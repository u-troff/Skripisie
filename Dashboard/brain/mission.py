"""Execution + perception loop.

Concurrency lives here and in main.py, nowhere else. Two coroutines share a
MissionSession: run_mission advances steps, ingest_frame runs the perception
funnel. They never both write active_plan — perception stages a swap and the
step loop applies it between steps.
"""

import asyncio
import base64
import json
import re
import time
from pathlib import Path
from typing import Awaitable, Callable, List, Optional

import config
import confirmation
import frames
import report as report_mod
import revision as revision_rules
import scene as scene_mod
import tts
from log_setup import get_logger
from mission_session import TERMINAL, MissionPhase, MissionSession, RevisionRecord
from planner import revise_plan
from providers import usage
from rover import RoverController
from stt import transcribe_audio
from vlm import KNOWN_MAX, check_arrival, check_progress, check_side_look, describe_frame
from vlm import failed as vlm_failed

log = get_logger("mission")

Emit = Callable[[dict], Awaitable[None]]

CHANGE_THRESHOLD = config.get_float("PERCEPTION_CHANGE_THRESHOLD", 0.06)
MIN_INTERVAL_S = config.get_float("PERCEPTION_MIN_INTERVAL_S", 6.0)
MIN_SHARPNESS = config.get_float("PERCEPTION_MIN_SHARPNESS", 25.0)

# -- grounded line mission (spec-grounded-line-mission.md §E) ---------------
# There is no distance or speed anywhere in a plan step, and the chassis has no
# encoders, so "how far along the line are we" is wall-clock time times a
# measured constant. LINE_SPEED_CMPS comes from test T2 — until it is measured
# every est_distance_cm in a run log is a placeholder, not a measurement.
LINE_LENGTH_CM = config.get_float("LINE_LENGTH_CM", 300.0)
LINE_SPEED_CMPS = config.get_float("LINE_SPEED_CMPS", 17.0)
KEYFRAME_SPACING_CM = config.get_float("KEYFRAME_SPACING_CM", 100.0)
LOOK_LEFT_AT_FRACTION = config.get_float("LOOK_LEFT_AT_FRACTION", 0.5)
LOOK_LEFT_PAN = config.get_int("LOOK_LEFT_PAN", 1100)
# Where the gimbal points for successive progress checks. The IR array is on the
# chassis, not the gimbal, so panning costs nothing and disturbs nothing — that
# is exactly what §0's "keeps the camera free for the VLM" bought. Set this to
# "centre" alone to restore the old forward-only behaviour.
PAN_CYCLE = [d.strip().lower() for d in
             config.get("KEYFRAME_PAN_CYCLE", "centre,left,right").split(",")
             if d.strip()] or ["centre"]
# How long to let a VLM check that is still running when the drive ends finish
# before giving up on it. At ~15s a frame on the local host, a check launched
# near the crossbar routinely outlives the step it belongs to.
CHECK_DRAIN_S = config.get_float("CHECK_DRAIN_S", 25.0)

LOGS_DIR = Path(__file__).parent / "logs"

# Used only when no room scene is loaded, to turn the command into a rough
# "what else should be here" list for the arrival check.
_STOPWORDS = {
    "the", "and", "then", "that", "this", "there", "here", "with", "from",
    "into", "onto", "your", "you", "its", "for", "are", "was", "has", "have",
    "line", "follow", "following", "drive", "driving", "move", "turn", "look",
    "tell", "see", "check", "find", "get", "got", "please", "rover", "robot",
    "along", "toward", "towards", "until", "when", "what", "where", "whether",
    "can", "will", "would", "should", "not", "any", "all", "out",
}


async def _speak(mission: MissionSession, text: str) -> dict:
    """Same event shape as pipeline._speak, including the Piper audio the Pi
    client plays back. Async because this runs inside the step loop, which
    shares an event loop with ingest_frame — synthesis is sub-second but
    still blocking, so it goes to a worker thread rather than stalling
    perception mid-mission."""
    event = {"type": "speak", "text": text, "session_id": mission.session_id,
             "phase": mission.phase.value}
    try:
        audio = await asyncio.to_thread(tts.synthesize_speech, text)
        event["audio"] = base64.b64encode(audio).decode("ascii")
    except Exception:
        # Degrade to text-only rather than killing the mission — same
        # soft-fail stance pipeline._speak takes.
        log.warning("[mission %s] TTS synthesis failed, falling back to text-only",
                    mission.session_id, exc_info=True)
    return event


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
        await emit(await _speak(mission, "Adjusting my route. " + verdict.reason + "."))
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
        await emit(await _speak(mission, "I cannot continue. " + verdict.reason + "."))
        return

    mission.phase = MissionPhase.AWAITING_REVISION_CONFIRMATION
    await emit({"type": "awaiting_revision", "session_id": mission.session_id,
                "revision": verdict.to_dict(), "plan": proposed})
    await emit(await _speak(mission, revision_rules.summarise(verdict, proposed)))


# --------------------------------------------------------------------------
# Grounded line mission — keyframe checks while driving, and the arrival check.
#
# Almost everything here is OBSERVATION. The IR line sensor steers and the
# ultrasonic remains the only authority on obstacles; a check that fails is
# recorded as a failed check, never as "not visible". The one deliberate
# exception: a confirmed sighting of the step's own named target (see the end
# of _record_check) halts the drive and counts as arrival — the follower's own
# crossbar detection is the *other* way a drive ends in arrival, not the only
# one any more.
# --------------------------------------------------------------------------

def _frames_dir(session_id: str) -> Path:
    path = LOGS_DIR / "frames" / session_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_frame(mission: MissionSession, name: str, raw: bytes) -> Optional[str]:
    """Every frame that goes to the VLM is kept. A verdict with no frame behind
    it cannot be checked against ground truth after the run, which is most of
    what T9 needs."""
    try:
        path = _frames_dir(mission.session_id) / name
        path.write_bytes(raw)
        return str(path.relative_to(Path(__file__).parent))
    except OSError:
        log.warning("[mission %s] could not save frame %s", mission.session_id, name,
                    exc_info=True)
        return None


def _scene_object_names(mission: MissionSession) -> List[str]:
    if not mission.scene_id:
        return []
    room = scene_mod.store.get(mission.scene_id)
    if room is None:
        return []
    names = []
    for frame in room.frames:
        for obj in frame.objects:
            name = str(obj.get("name") or "").strip()
            if name:
                names.append(name)
    return names


def _room_vocabulary(mission: MissionSession) -> List[str]:
    """The room video's catalogue, as the grounding list for the drive checks.

    This is the room video reaching the *hardware* stage. Until now the scene
    digest only shaped the pre-departure prompts (clarification, planning,
    verification) and then went unused the moment the rover started moving — a
    check mid-drive had nothing to be right or wrong against. Now every check
    answers against the same catalogue the plan was built from, which is what
    makes "it saw something that is not in this room" a measurable event rather
    than a judgement call.

    Grounding only. Nothing derived from it steers: the IR sensor and the sonar
    own the driving, at two orders of magnitude more often than a frame can be
    read. [] when no room video was uploaded, which is a weaker but valid run.
    """
    if not mission.scene_id:
        return []
    room = scene_mod.store.get(mission.scene_id)
    return room.vocabulary(KNOWN_MAX) if room is not None else []


def _expected_things(mission: MissionSession, target: str) -> List[str]:
    """What else the arrival frame should contain, besides the target.

    Deliberately narrow: only room objects the command itself mentions, not the
    whole room inventory. Handing the VLM every object in the room would make
    `missing` non-empty on every single run, and report.py flags a non-empty
    `missing` as an uncertainty — the flag would stop meaning anything.
    """
    text = ((mission.command or "") + " " + (mission.original_command or "")).lower()
    expected: List[str] = []
    seen = set()

    def add(item: str) -> None:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            expected.append(item.strip())

    if target:
        add(target)

    names = _scene_object_names(mission)
    if names:
        for name in names:
            if name.lower() in text:
                add(name)
    else:
        for word in re.findall(r"[A-Za-z]{3,}", text):
            if word.lower() not in _STOPWORDS:
                add(word)
    return expected[:8]


async def _emit_check(mission: MissionSession, record: dict, emit: Emit) -> None:
    await emit({"type": "check", "session_id": mission.session_id, "check": record})


async def _record_check(mission: MissionSession, rover, kind: str, frame: bytes,
                        t_rel_s: float, target: str, emit: Emit,
                        filename: str, known: Optional[List[str]] = None,
                        aimed: str = "centre") -> None:
    """Run one VLM check in a worker thread and file the result.

    Runs as its own task so the drive is never waiting on inference — at ~15s a
    frame the check would otherwise be the slowest thing in the loop by two
    orders of magnitude.

    `known` is the room-video catalogue (_room_vocabulary). It costs no extra
    call — it is a block in the prompt that already runs — and it is what turns
    a free-form description into a claim that can be checked against a room we
    filmed.
    """
    frame_path = _save_frame(mission, filename, frame)
    call = check_progress if kind == "progress" else check_side_look
    started = time.perf_counter()
    result = await asyncio.to_thread(call, frame, target, known or [])
    latency = time.perf_counter() - started

    record = {
        "kind": kind,
        "target": target,
        # The physical direction the camera was pointing, never the PWM
        # number — spec §8.
        "aimed": aimed,
        "known_count": len(known or []),
        "t_rel_s": round(t_rel_s, 2),
        "est_distance_cm": round(t_rel_s * LINE_SPEED_CMPS, 1),
        "latency_s": round(latency, 2),
        "frame_path": frame_path,
        "result": result,
    }
    mission.checks.append(record)
    if kind == "look_left":
        mission.look_left = record
    mission.touch()
    log.info("[mission %s] %s check at %.1fs (%.1fs latency): %s",
             mission.session_id, kind, t_rel_s, latency, json.dumps(result)[:200])
    await _emit_check(mission, record, emit)

    # Policy for this slice: warn, never halt. The sonar is the only authority
    # on distance and stopping, and a VLM whose answer is seconds stale must
    # not be allowed to veto a sensor that is current.
    #
    # Centre frames ONLY. `path_clear` on a side-looking frame is a statement
    # about a wall to the left, not about the route ahead — counting those
    # would fire the warning on a perfectly clear line, and a warning that
    # fires every run is worse than no warning at all.
    if kind == "progress" and aimed == "centre" and not vlm_failed(result):
        if result.get("path_clear") is False:
            mission.path_unclear_streak += 1
            if mission.path_unclear_streak >= 2:
                log.warning("[mission %s] two consecutive checks report the path is "
                            "not clear — not halting (sonar owns stopping)",
                            mission.session_id)
                await emit({"type": "warning", "session_id": mission.session_id,
                            "message": "Two consecutive camera checks report the path "
                                       "ahead is not clear.",
                            "check": record})
        else:
            mission.path_unclear_streak = 0

    # Narrow exception to "never halt", above: a confirmed sighting of the
    # step's own named target is a deliberate stop condition, not a path/
    # obstacle judgement — those stay sonar/crossbar-only. Any aimed direction
    # counts (a sweep's left/right frame is as valid a sighting as centre), and
    # the look-left check counts too (check_side_look also reports
    # target_visible). First confirmation wins — the guard below stops a
    # second in-flight check in the same sweep from firing twice.
    if kind in ("progress", "look_left") and target and not vlm_failed(result):
        if result.get("target_visible") is True and mission.target_confirmed is None:
            mission.target_confirmed = record
            log.warning("[mission %s] target %r confirmed at %.1fs (aimed=%s, ~%.0fcm) "
                        "— stopping", mission.session_id, target, t_rel_s, aimed,
                        record["est_distance_cm"])
            confirm = getattr(rover, "confirm_target", None)
            if confirm is not None:
                confirm(target)
            await emit({"type": "target_confirmed", "session_id": mission.session_id,
                        "check": record})

        else:
            mission.path_unclear_streak = 0


async def _skip(mission: MissionSession, reason: str, t_rel_s: float, emit: Emit) -> None:
    record = {"kind": "skipped", "reason": reason, "t_rel_s": round(t_rel_s, 2),
              "est_distance_cm": round(t_rel_s * LINE_SPEED_CMPS, 1)}
    mission.checks.append(record)
    log.info("[mission %s] check skipped at %.1fs (%s)", mission.session_id,
             t_rel_s, reason)
    await _emit_check(mission, record, emit)


async def _bend(mission: MissionSession, bend: dict, t_rel_s: float,
                emit: Emit) -> None:
    """One 90-degree bend the follower took, from line_follow_corner.py's
    ~/corner topic.

    Filed alongside the VLM checks on purpose: it is the only thing in a run
    log that says where the rover actually *went* rather than what it thought
    it saw, it costs no inference, and it comes from the same IR sensor that
    did the steering — so it cannot be hallucinated.
    """
    record = {"kind": "bend", "side": bend.get("side"),
              "t_rel_s": round(t_rel_s, 2),
              "est_distance_cm": round(t_rel_s * LINE_SPEED_CMPS, 1)}
    mission.checks.append(record)
    log.info("[mission %s] bend taken: %s at %.1fs", mission.session_id,
             record["side"], t_rel_s)
    await _emit_check(mission, record, emit)


async def _look_left(mission: MissionSession, rover, target: str,
                     started: float, emit: Emit,
                     known: Optional[List[str]] = None) -> None:
    """The one planned stop. Pause, pan left, grab a frame, re-centre, resume —
    and only then ask the model about it, so inference never holds up motion."""
    t_rel = time.time() - started
    parked = await asyncio.to_thread(rover.pause)
    if not parked:
        log.warning("[mission %s] look-left skipped: follower did not park",
                    mission.session_id)
        await _skip(mission, "pause_failed", t_rel, emit)
        return

    frame = await asyncio.to_thread(rover.look, LOOK_LEFT_PAN)
    await asyncio.to_thread(rover.resume)

    if frame is None:
        await _skip(mission, "no_frame", t_rel, emit)
        return

    task = asyncio.ensure_future(
        _record_check(mission, rover, "look_left", frame, t_rel, target, emit,
                      "left_01.jpg", known, "left")
    )
    mission.check_tasks.append(task)



async def keyframe_monitor(mission: MissionSession, rover, step: dict, emit: Emit) -> None:
    """Sample the camera while a follow_line step runs.

    Single-flight: if a check is still in the VLM when the next tick comes due,
    that tick is recorded as skipped rather than queued. On the local host this
    is expected to skip most ticks and complete 1-3 checks per run — that ratio
    is a measured RQ1 result, not a bug to tune away.

    Cancelled by run_mission the moment the step returns; in-flight checks are
    drained separately so a slow one still reaches the report.
    """
    target = str(step.get("target") or "").strip()
    # Built once per drive, not per check: the scene is immutable for the run,
    # and rebuilding it 20 times would be pure waste.
    known = _room_vocabulary(mission)
    started = time.time()
    interval_s = max(1.0, KEYFRAME_SPACING_CM / LINE_SPEED_CMPS) if LINE_SPEED_CMPS > 0 else 1.0
    look_left_at_s = (LOOK_LEFT_AT_FRACTION * LINE_LENGTH_CM / LINE_SPEED_CMPS
                      if LINE_SPEED_CMPS > 0 else 0.0)
    can_look = all(hasattr(rover, name) for name in ("pause", "resume", "look"))
    look_done = not can_look
    if not can_look:
        log.info("[mission %s] rover has no pause/look/resume — no look-left this run",
                 mission.session_id)

    # Only a rover running line_follow_corner.py has these; against the stock
    # node (or sim/virtual) both stay None and the loop behaves exactly as it
    # did on a straight track.
    corner_guard = getattr(rover, "corner_guard_active", None)
    drain_corners = getattr(rover, "drain_corners", None)
    deferred_logged = False

    # Panning while driving, not stopping to do it: the IR array is on the
    # chassis, so where the camera points has no bearing on steering. A rover
    # that only ever looks straight ahead can only ever report what is on the
    # tape, which is the least interesting thing in the room.
    aim = getattr(rover, "aim", None)
    pan_cycle = PAN_CYCLE if aim is not None else ["centre"]

    log.info("[mission %s] keyframe monitor: every %.1fs, look-left at %.1fs, target=%r, "
             "room catalogue=%d item(s)",
             mission.session_id, interval_s, look_left_at_s, target, len(known))
    if not known:
        # Not an error — a run with no room video is a valid (weaker) run. But
        # it means every `unexpected` will be empty, so the report must not read
        # that as "the rover hallucinated nothing".
        log.info("[mission %s] no room video loaded — checks run ungrounded",
                 mission.session_id)

    in_flight: List[asyncio.Future] = []
    index = 0
    next_tick = started + interval_s


    try:
        while True:
            await asyncio.sleep(0.2)
            now = time.time()

            if drain_corners is not None:
                for bend in drain_corners():
                    await _bend(mission, bend, float(bend.get("at") or now) - started, emit)

            due_for_look = not look_done and (now - started) >= look_left_at_s
            if due_for_look and corner_guard is not None and corner_guard():
                # Spec §7: a STOP landing inside the follower's pivot cancels the
                # turn and the rover resumes straight, off the tape. Defer the one
                # planned stop rather than skip it — keyframe checks carry on
                # meanwhile, so nothing else is lost by waiting the bend out.
                if not deferred_logged:
                    log.info("[mission %s] look-left deferred: a bend is in progress",
                             mission.session_id)
                    deferred_logged = True
                due_for_look = False

            if due_for_look:
                look_done = True
                await _look_left(mission, rover, target, started, emit, known)
                # Don't fire a keyframe the instant the rover starts moving again.
                next_tick = time.time() + interval_s
                continue

            if now < next_tick:
                continue
            next_tick = now + interval_s

            if in_flight and any(not t.done() for t in in_flight):
                await _skip(mission, "vlm_busy", now - started, emit)
                continue

            # Full centre/left/right sweep at every checkpoint now, not one
            # direction cycled across ticks — the single-flight rule above
            # applies to the sweep as a whole: the next sweep waits for every
            # check from this one to finish before it will dispatch.
            in_flight = []
            for direction in pan_cycle:
                # Aim first, then grab: the settle is inside aim(). At 15.3
                # cm/s a 0.5 s settle is ~8 cm of travel, so the frame is
                # taken from essentially where the check is timestamped.
                aimed = direction
                if aim is not None:
                    aimed = await asyncio.to_thread(aim, direction) or direction

                frame = await asyncio.to_thread(rover.get_frame)
                if frame is None:
                    await _skip(mission, "no_frame", time.time() - started, emit)
                    continue

                index += 1
                task = asyncio.ensure_future(
                    _record_check(mission, rover, "progress", frame,
                                  time.time() - started, target, emit,
                                  "kf_%02d_%s.jpg" % (index, aimed), known, aimed)
                )
                mission.check_tasks.append(task)
                in_flight.append(task)

    except asyncio.CancelledError:
        # The drive ended (arrived, blocked or halted) before the look-left ran.
        # Record why: an absent look_left with no explanation reads in the run
        # log as if the code forgot. The two causes are not the same fault —
        # a bend deferral is the §7 guard doing its job on a route with corners,
        # while "never came due" just means the drive was shorter than
        # LOOK_LEFT_AT_FRACTION predicted, which is a calibration note.
        if not look_done:
            reason = ("look_left_deferred_by_bend" if deferred_logged
                      else "look_left_not_due_before_end_of_drive")
            mission.checks.append({
                "kind": "skipped",
                "reason": reason,
                "t_rel_s": round(time.time() - started, 2),
            })
            log.warning("[mission %s] look-left never happened (%s)",
                        mission.session_id, reason)
        raise


async def _drain_checks(mission: MissionSession) -> None:
    pending = [task for task in mission.check_tasks if not task.done()]
    mission.check_tasks = []
    if not pending:
        return
    log.info("[mission %s] waiting up to %.0fs for %d in-flight check(s)",
             mission.session_id, CHECK_DRAIN_S, len(pending))
    _, unfinished = await asyncio.wait(pending, timeout=CHECK_DRAIN_S)
    for task in unfinished:
        task.cancel()
    if unfinished:
        log.warning("[mission %s] gave up on %d check(s) still in the VLM",
                    mission.session_id, len(unfinished))


async def _arrival_check(mission: MissionSession, rover, target: str, emit: Emit) -> None:
    t_rel = time.time() - mission.started_at
    # Centre first, always. The pan cycle leaves the gimbal wherever the last
    # progress check pointed it, and an arrival frame taken while still aimed
    # left would be scored against the forward target — a false "not visible"
    # that looks exactly like an honest one. Idempotent: the `observe` step
    # already centres, and doing it twice costs nothing.
    if hasattr(rover, "center_gimbal"):
        await asyncio.to_thread(rover.center_gimbal)
        await asyncio.sleep(0.5)
    frame = await asyncio.to_thread(rover.get_frame)
    if frame is None:
        mission.arrival = {"kind": "arrival", "target": target, "expected": [],
                           "t_rel_s": round(t_rel, 2), "latency_s": None,
                           "frame_path": None,
                           "result": {"_error": "no frame from the camera"}}
        await _emit_check(mission, mission.arrival, emit)
        return

    expected = _expected_things(mission, target)
    known = _room_vocabulary(mission)
    frame_path = _save_frame(mission, "arrival.jpg", frame)
    started = time.perf_counter()
    result = await asyncio.to_thread(check_arrival, frame, target, expected, known)
    record = {
        "kind": "arrival",
        "target": target,
        "expected": expected,
        "known": known,
        "t_rel_s": round(t_rel, 2),
        "latency_s": round(time.perf_counter() - started, 2),
        "frame_path": frame_path,
        "result": result,
    }
    mission.arrival = record
    mission.touch()
    log.info("[mission %s] arrival check: %s", mission.session_id,
             json.dumps(result)[:300])
    await _emit_check(mission, record, emit)


async def _maybe_arrival_check(mission: MissionSession, rover, step: dict,
                               result: dict, emit: Emit) -> None:
    """One arrival check per run: after the observe step if the plan has one,
    otherwise straight after a follow_line step that actually reached the
    crossbar. A follow_line that timed out or was blocked has not arrived, so
    there is nothing to check for."""
    if mission.arrival is not None or not hasattr(rover, "get_frame"):
        return

    action = str(step.get("action") or "").strip().lower()
    if action == "observe":
        pass
    elif action == "follow_line":
        detail = result.get("detail")
        arrived = isinstance(detail, dict) and detail.get("arrived")
        has_observe = any(str(s.get("action") or "").strip().lower() == "observe"
                          for s in mission.steps())
        if not arrived or has_observe:
            return
    else:
        return

    await _arrival_check(mission, rover, str(step.get("target") or ""), emit)


def _grounding(mission: MissionSession, rover: RoverController) -> List[dict]:
    """Every confirmed-plan step with a target, resolved through the room's
    matcher — independent of whether the mission actually reached that step,
    so a plan naming a target the room doesn't have shows up even if the
    mission halted before getting there. Only meaningful for a rover with a
    room.resolve (VirtualRover); [] otherwise."""
    room = getattr(rover, "room", None)
    if room is None or not hasattr(room, "resolve"):
        return []
    grounding = []
    for step in mission.confirmed_plan.get("steps") or []:
        target = step.get("target")
        if not target:
            continue
        landmark, how = room.resolve(target)
        grounding.append({
            "step_id": step.get("id"),
            "action": step.get("action"),
            "target": target,
            "resolved": landmark is not None,
            "matched": landmark.name if landmark is not None else None,
            "how": how,
        })
    return grounding


def _collect_terminal_fields(mission: MissionSession, rover: RoverController) -> None:
    """rover_summary, grounding and usage all need to be in place before
    report.build_report runs — including on the abort path, where the report
    is built inside the except block, before `finally` would otherwise set
    them. Called from both places; cheap and idempotent either way."""
    if hasattr(rover, "summary"):
        mission.rover_summary = rover.summary()
    mission.grounding = _grounding(mission, rover)
    mission.usage = usage.snapshot()


def _write_run_log(mission: MissionSession, report: dict) -> Optional[str]:
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        path = LOGS_DIR / ("mission_%s.json" % mission.session_id)
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    except OSError:
        log.warning("[mission %s] could not write the run log", mission.session_id,
                    exc_info=True)
        return None
    log.info("[mission %s] run log -> %s", mission.session_id, path)
    return str(path)


async def _finish_report(mission: MissionSession, emit: Emit) -> None:
    try:
        report = await asyncio.to_thread(report_mod.build_report, mission)
    except Exception:
        log.exception("[mission %s] report generation failed", mission.session_id)
        return
    await emit({"type": "report", "session_id": mission.session_id, "report": report})
    summary = str(report.get("spoken_summary") or "").strip()
    if summary:
        await emit(await _speak(mission, summary))
    await asyncio.to_thread(_write_run_log, mission, report)


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
            action = str(step.get("action") or "").strip().lower()
            await emit({"type": "step_started", "session_id": mission.session_id,
                        "index": mission.cursor, "step": step})

            # The keyframe monitor only exists for the driving step, and only
            # for a rover that can hand over a frame. On sim/virtual there is
            # no get_frame, so the whole check path stays switched off and
            # mission.py behaves exactly as it did before.
            monitor = None
            if action == "follow_line" and hasattr(rover, "get_frame"):
                monitor = asyncio.ensure_future(keyframe_monitor(mission, rover, step, emit))

            step_started_at = time.time()
            try:
                result = await asyncio.to_thread(rover.execute_step, step)
            finally:
                if monitor is not None:
                    monitor.cancel()
                    await asyncio.gather(monitor, return_exceptions=True)
                    await _drain_checks(mission)
                    # The pan cycle does not re-centre after each check (that
                    # would double every settle for nothing), so put it back
                    # once the drive is over.
                    if hasattr(rover, "center_gimbal"):
                        await asyncio.to_thread(rover.center_gimbal)
                    if hasattr(rover, "telemetry_snapshot"):
                        mission.rover_telemetry = rover.telemetry_snapshot()

            mission.results.append({"step": step,
                                    "elapsed_s": round(time.time() - step_started_at, 2),
                                    **result})
            mission.touch()
            await emit({"type": "step_done", "session_id": mission.session_id,
                        "index": mission.cursor, "step": step, "result": result})

            if hasattr(rover, "state_snapshot"):
                await emit({"type": "rover_state", "session_id": mission.session_id,
                            "state": rover.state_snapshot()})

            if result.get("status") == "ok":
                await _maybe_arrival_check(mission, rover, step, result, emit)

            if result.get("status") == "blocked":
                mission.phase = MissionPhase.HALTED
                await emit(await _speak(mission, "I am blocked and have stopped."))
                break
            if result.get("status") == "halted":
                break

            mission.cursor += 1

        if mission.phase is MissionPhase.EXECUTING and mission.cursor >= len(mission.steps()):
            mission.phase = MissionPhase.COMPLETED
    except asyncio.CancelledError:
        rover.halt()
        mission.phase = MissionPhase.ABORTED
        # No planner call and no await on the abort path — the operator has
        # already asked the rover to stop, and the report must not be the thing
        # that delays it. The deterministic half of the report is still worth
        # keeping, so it is written straight to disk and nothing is spoken.
        _collect_terminal_fields(mission, rover)
        _write_run_log(mission, report_mod.build_report(mission, spoken=False))
        raise
    finally:
        mission.touch()
        log.info("[mission %s] end — %s", mission.session_id, mission.phase.value)
        # Always, on every non-aborted outcome: completed, blocked or halted.
        # A run that ends badly is exactly the run whose report matters most.
        if mission.phase is not MissionPhase.ABORTED:
            _collect_terminal_fields(mission, rover)
            await _finish_report(mission, emit)
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
        await emit(await _speak(mission, "Accepted. Continuing."))
        return

    # Anything that is not a clear yes stops the rover. Unclear must never
    # read as consent for a change the human did not confirm.
    mission.pending_material = None
    mission.phase = MissionPhase.HALTED
    await emit({"type": "halted", "session_id": mission.session_id,
                "reason": "revision_rejected" if verdict == confirmation.REJECT else "unclear"})
    await emit(await _speak(mission, "Stopping."))


async def abort(mission: MissionSession, rover: RoverController, emit: Emit) -> None:
    """No inference, no model, no network."""
    rover.halt()
    mission.phase = MissionPhase.ABORTED
    await emit({"type": "aborted", "session_id": mission.session_id})
