"""Execution + perception loop.

Concurrency lives here and in main.py, nowhere else. Two coroutines share a
MissionSession: run_mission advances steps, ingest_frame runs the perception
funnel. They never both write active_plan — perception stages a swap and the
step loop applies it between steps.
"""

import asyncio
import time
from typing import Awaitable, Callable, Optional

import config
import confirmation
import frames
import revision as revision_rules
from log_setup import get_logger
from mission_session import TERMINAL, MissionPhase, MissionSession, RevisionRecord
from planner import revise_plan
from rover import RoverController
from stt import transcribe_audio
from vlm import describe_frame

log = get_logger("mission")

Emit = Callable[[dict], Awaitable[None]]

CHANGE_THRESHOLD = config.get_float("PERCEPTION_CHANGE_THRESHOLD", 0.06)
MIN_INTERVAL_S = config.get_float("PERCEPTION_MIN_INTERVAL_S", 6.0)
MIN_SHARPNESS = config.get_float("PERCEPTION_MIN_SHARPNESS", 25.0)


def _speak(mission: MissionSession, text: str) -> dict:
    return {"type": "speak", "text": text, "session_id": mission.session_id,
            "phase": mission.phase.value}


# --------------------------------------------------------------------------
# Perception — three tiers, each gating the next.
# --------------------------------------------------------------------------
async def ingest_frame(mission: MissionSession, raw: bytes, emit: Emit) -> None:
    mission.frames_seen += 1
    if mission.phase in TERMINAL or mission.analysing:
        return

    # Tier 0: free. Decode + 32x32 diff, on the event loop's thread pool so a
    # burst of frames can't stall the step loop.
    stats = await asyncio.to_thread(frames.analyse, raw)
    if stats is None or stats.sharpness < MIN_SHARPNESS:
        return

    now = time.time()
    if now - mission.last_analysis_at < MIN_INTERVAL_S:
        mission.last_stats = mission.last_stats or stats
        return
    if mission.last_stats is not None:
        if frames.distance(stats, mission.last_stats) < CHANGE_THRESHOLD:
            return

    mission.analysing = True
    mission.last_stats = stats
    mission.last_analysis_at = now
    try:
        await _analyse(mission, raw, emit)
    finally:
        mission.analysing = False


async def _analyse(mission: MissionSession, raw: bytes, emit: Emit) -> None:
    # Tier 1: one caption per changed frame.
    caption = await asyncio.to_thread(describe_frame, raw)
    if not caption:
        return
    mission.frames_analysed += 1
    mission.note(caption)
    mission.touch()
    await emit({"type": "observation", "session_id": mission.session_id,
                "text": caption, "digest": list(mission.digest)})

    if mission.phase is not MissionPhase.EXECUTING:
        return

    # Tier 2: ask the planner whether the remaining steps still hold.
    proposal = await asyncio.to_thread(
        revise_plan, mission.confirmed_plan, mission.remaining(),
        list(mission.digest), mission.command,
    )
    if not proposal.get("change"):
        return

    proposed = {"steps": proposal.get("steps") or [], "notes": proposal.get("reason")}
    verdict = revision_rules.classify(mission.confirmed_plan, proposed)

    if verdict.kind == revision_rules.NO_CHANGE:
        return

    if verdict.kind == revision_rules.REROUTE:
        mission.pending_swap = proposed
        mission.revision_log.append(
            RevisionRecord(time.time(), verdict.kind, verdict.reason, True, proposed)
        )
        await emit({"type": "revision", "session_id": mission.session_id,
                    "revision": verdict.to_dict(), "plan": proposed, "applied": True})
        await emit(_speak(mission, "Adjusting my route. " + verdict.reason + "."))
        return

    # MATERIAL or BLOCKED: the human decides, and the rover stops asking the
    # models anything until they do.
    mission.pending_material = proposed
    mission.revision_log.append(
        RevisionRecord(time.time(), verdict.kind, verdict.reason, False, proposed)
    )
    if verdict.kind == revision_rules.BLOCKED:
        mission.phase = MissionPhase.HALTED
        await emit({"type": "halted", "session_id": mission.session_id,
                    "revision": verdict.to_dict()})
        await emit(_speak(mission, "I cannot continue. " + verdict.reason + "."))
        return

    mission.phase = MissionPhase.AWAITING_REVISION_CONFIRMATION
    await emit({"type": "awaiting_revision", "session_id": mission.session_id,
                "revision": verdict.to_dict(), "plan": proposed})
    await emit(_speak(mission, revision_rules.summarise(verdict, proposed)))


# --------------------------------------------------------------------------
# Execution — one step at a time, revisions applied only between steps.
# --------------------------------------------------------------------------
async def run_mission(mission: MissionSession, rover: RoverController, emit: Emit) -> None:
    log.info("=" * 68)
    log.info("[mission %s] start — %d step(s)", mission.session_id, len(mission.steps()))
    await emit({"type": "mission_started", "session_id": mission.session_id,
                "plan": mission.confirmed_plan})

    try:
        while mission.phase not in TERMINAL and mission.cursor < len(mission.steps()):
            if mission.phase is MissionPhase.AWAITING_REVISION_CONFIRMATION:
                await asyncio.sleep(0.2)
                continue

            if mission.pending_swap is not None:
                mission.active_plan = mission.pending_swap
                mission.pending_swap = None
                mission.cursor = 0
                await emit({"type": "plan_revised", "session_id": mission.session_id,
                            "plan": mission.active_plan})

            step = mission.steps()[mission.cursor]
            await emit({"type": "step_started", "session_id": mission.session_id,
                        "index": mission.cursor, "step": step})

            result = await asyncio.to_thread(rover.execute_step, step)
            mission.results.append({"step": step, **result})
            mission.touch()
            await emit({"type": "step_done", "session_id": mission.session_id,
                        "index": mission.cursor, "step": step, "result": result})

            if result.get("status") == "blocked":
                mission.phase = MissionPhase.HALTED
                await emit(_speak(mission, "I am blocked and have stopped."))
                break
            if result.get("status") == "halted":
                break

            mission.cursor += 1

        if mission.phase is MissionPhase.EXECUTING and mission.cursor >= len(mission.steps()):
            mission.phase = MissionPhase.COMPLETED
            await emit(_speak(mission, "Done."))
    except asyncio.CancelledError:
        rover.halt()
        mission.phase = MissionPhase.ABORTED
        raise
    finally:
        mission.touch()
        log.info("[mission %s] end — %s", mission.session_id, mission.phase.value)
        await emit({"type": "mission_ended", "session_id": mission.session_id,
                    "phase": mission.phase.value, "snapshot": mission.snapshot()})


async def handle_revision_confirmation(mission: MissionSession, audio, emit: Emit) -> None:
    text = await asyncio.to_thread(transcribe_audio, audio)
    verdict = await asyncio.to_thread(confirmation.classify, text)
    log.info("[mission %s] revision reply %r -> %s", mission.session_id, text, verdict)

    if verdict == confirmation.CONFIRM and mission.pending_material is not None:
        mission.active_plan = mission.pending_material
        mission.pending_material = None
        mission.cursor = 0
        mission.phase = MissionPhase.EXECUTING
        if mission.revision_log:
            mission.revision_log[-1].applied = True
        await emit({"type": "plan_revised", "session_id": mission.session_id,
                    "plan": mission.active_plan})
        await emit(_speak(mission, "Accepted. Continuing."))
        return

    # Anything that is not a clear yes stops the rover. Unclear must never
    # read as consent for a change the human did not confirm.
    mission.pending_material = None
    mission.phase = MissionPhase.HALTED
    await emit({"type": "halted", "session_id": mission.session_id,
                "reason": "revision_rejected" if verdict == confirmation.REJECT else "unclear"})
    await emit(_speak(mission, "Stopping."))


async def abort(mission: MissionSession, rover: RoverController, emit: Emit) -> None:
    """No inference, no model, no network."""
    rover.halt()
    mission.phase = MissionPhase.ABORTED
    await emit({"type": "aborted", "session_id": mission.session_id})
