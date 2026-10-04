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
from typing import Awaitable, Callable, List, Optional,Dict,Tuple

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
from vlm import BBOX_SCALE, KNOWN_MAX, check_arrival, check_progress, check_side_look, describe_frame, locate_target
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

# Fixed sweep direction — the spec gives SEARCH_STEPS x SEARCH_PIVOT_S a
# direction to sweep, not a config knob, so this is a constant, not .env.
SEARCH_DIR = 1  # +1 = right, matching rover_pi.pivot()'s convention
APPROACH_MAX_CYCLES = config.get_int("APPROACH_MAX_CYCLES", 20)
APPROACH_MAX_S = config.get_float("APPROACH_MAX_S", 240.0)
HOP_CM = config.get_float("HOP_CM", 30.0)
HOP_NEAR_CM = config.get_float("HOP_NEAR_CM", 15.0)
HOP_BACK_S = config.get_float("HOP_BACK_S", 0.6)
NEAR_FILL = config.get_float("NEAR_FILL", 0.08)
ARRIVE_FILL = config.get_float("ARRIVE_FILL", 0.20)
ARRIVE_BOTTOM = config.get_float("ARRIVE_BOTTOM", 0.90)
ARRIVE_MM = config.get_float("ARRIVE_MM", 250.0)
# Vision (fill/bottom) is demoted to a fallback when sonar is readable and
# clearly disagrees — sonar is a physical measurement, vision's "looks close"
# is scene/tilt-dependent and was observed firing at 1.7m away (2026-10-03).
# None/unreadable sonar still lets vision arrive on its own, for a target
# sonar genuinely can't range (off-axis, below/above the beam).
ARRIVE_SONAR_SANITY_MM = config.get_float("ARRIVE_SONAR_SANITY_MM", 800.0)
CENTER_TOL = config.get_float("CENTER_TOL", 0.12)
CAMERA_HFOV_DEG = config.get_float("CAMERA_HFOV_DEG", 60.0)
FREE_TURN_DEG_PER_S = config.get_float("FREE_TURN_DEG_PER_S", 90.0)
# Seconds of pivot per unit of x_center offset. e ranges -0.5..+0.5 as a
# fraction of frame width, which maps 1:1 onto the full HFOV, so
# angle_needed_deg = e * HFOV_DEG, and seconds = angle_needed_deg / DEG_PER_S.
PIVOT_S_PER_UNIT = CAMERA_HFOV_DEG / FREE_TURN_DEG_PER_S if FREE_TURN_DEG_PER_S > 0 else 0.0

# -- gimbal-first approach search (amendment 2026-10-02; rung F8) ----------
APPROACH_TILT_OFFSET = config.get_int("APPROACH_TILT_OFFSET", 0)
GIMBAL_DEG_PER_UNIT = config.get_float("GIMBAL_DEG_PER_UNIT", 0.0)
PAN_SWEEP_UNITS = config.get_int("PAN_SWEEP_UNITS", 400)
SWEEP_OVERLAP_FRAC = config.get_float("SWEEP_OVERLAP_FRAC", 0.15)
SWEEP_MARGIN_DEG = config.get_float("SWEEP_MARGIN_DEG", 20.0)
# NOT in the spec doc's .env block — it only specifies chassis-pivot timing
# in prose ("ONE big pivot" between sweeps) without naming the knob. Added
# here so the pivot has a size; 110° keeps 3 sweeps x 2 advances inside the
# doc's own "≤232° worst case" budget. Flag this choice when you read the
# spec doc again — it's a gap I filled, not something written there.
SWEEP_ADVANCE_DEG = config.get_float("SWEEP_ADVANCE_DEG", 110.0)
SEARCH_MAX_SWEEPS = config.get_int("SEARCH_MAX_SWEEPS", 3)
SEARCH_MAX_VLM_CALLS = config.get_int("SEARCH_MAX_VLM_CALLS", 18)
APPROACH_SETTLE_S = config.get_float("APPROACH_SETTLE_S", 0.8)
APPROACH_MIN_SHARPNESS = config.get_float("APPROACH_MIN_SHARPNESS", 25.0)
APPROACH_FRAME_RETRIES = config.get_int("APPROACH_FRAME_RETRIES", 2)
MAX_FILL = config.get_float("MAX_FILL", 0.60)
MAX_BOX_SIDE_FRAC = config.get_float("MAX_BOX_SIDE_FRAC", 0.95)
MIN_FILL = config.get_float("MIN_FILL", 0.0008)
MIN_HOPS_BEFORE_ARRIVAL = config.get_int("MIN_HOPS_BEFORE_ARRIVAL", 1)
FILL_JUMP_MAX = config.get_float("FILL_JUMP_MAX", 3.0)
MIN_PIVOT_DEG = config.get_float("MIN_PIVOT_DEG", 6.0)
MIN_PIVOT_S = config.get_float("MIN_PIVOT_S", 0.12)
APPROACH_ARRIVAL_CROSSCHECK = config.get_int("APPROACH_ARRIVAL_CROSSCHECK", 1)
APPROACH_RESIDUAL_FLAG_E = config.get_float("APPROACH_RESIDUAL_FLAG_E", 0.30)


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
        return path.relative_to(Path(__file__).parent).as_posix()
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


async def _arrival_check(mission: MissionSession, rover, target: str, emit: Emit,keep_tilt:Optional[int]=None) -> None:
    t_rel = time.time() - mission.started_at
    # Centre first, always. The pan cycle leaves the gimbal wherever the last
    # progress check pointed it, and an arrival frame taken while still aimed
    # left would be scored against the forward target — a false "not visible"
    # that looks exactly like an honest one. Idempotent: the `observe` step
    # already centres, and doing it twice costs nothing.
    if keep_tilt is not None and hasattr(rover, "set_gimbal"):
        await asyncio.to_thread(rover.set_gimbal, 0, keep_tilt)
    elif hasattr(rover, "center_gimbal"):
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

# --------------------------------------------------------------------------
# Free-roam approach controller (spec-free-roam-approach.md §2). Only reached
# when NAV_MODE=free and the rover has pivot/hop/get_frame — see
# run_mission's use_free_approach gate. Returns the same {"status", "detail"}
# shape execute_step does, so nothing downstream needs a special case.
#
# Control split (§0): sonar is the fast reactive layer, inside hop()'s own
# watchdog, at 20Hz, during every movement. The VLM only ever runs while the
# rover is stationary, between movements — this loop never calls locate_target
# while a Twist is being published.
# --------------------------------------------------------------------------
def _arrived(loc: dict, sonar_mm: Optional[int], e: float) -> bool:
    if loc.get("fill") is not None and loc["fill"] >= ARRIVE_FILL:
        return True
    if loc.get("bottom") is not None and loc["bottom"] >= ARRIVE_BOTTOM:
        return True
    if sonar_mm is not None and sonar_mm <= ARRIVE_MM and abs(e) <= CENTER_TOL:
        return True
    return False


def _derive_loc(raw: dict, frame_w: Optional[int], frame_h: Optional[int]) -> dict:
    """bbox_2d (pixels) -> x_center/fill/bottom/w_frac/h_frac (frame fractions).

    vlm_said_visible is carried through separately from `visible` so the trace
    can tell "the model said no" from "we rejected what the model said".
    """
    vlm_said_visible = bool(raw.get("visible"))
    bbox = raw.get("bbox_2d")
    if (not vlm_said_visible or not frame_w or not frame_h
            or not isinstance(bbox, list) or len(bbox) != 4):
        return {"visible": False, "bbox_2d": None, "vlm_said_visible": vlm_said_visible}
    x1, y1, x2, y2 = bbox
    if BBOX_SCALE > 0:
        try:
            x1, x2 = x1 / BBOX_SCALE * frame_w, x2 / BBOX_SCALE * frame_w
            y1, y2 = y1 / BBOX_SCALE * frame_h, y2 / BBOX_SCALE * frame_h
        except TypeError:
            return {"visible": False, "bbox_2d": None, "vlm_said_visible": vlm_said_visible}
        bbox = [round(x1), round(y1), round(x2), round(y2)]
    if not (x1 < x2 and y1 < y2 and x1 >= 0 and y1 >= 0
            and x2 <= frame_w and y2 <= frame_h):
        return {"visible": False, "bbox_2d": None, "vlm_said_visible": vlm_said_visible}
    w_frac = (x2 - x1) / frame_w
    h_frac = (y2 - y1) / frame_h
    return {
        "visible": True, "bbox_2d": bbox, "vlm_said_visible": vlm_said_visible,
        "x_center": round(((x1 + x2) / 2) / frame_w, 3),
        "fill": round(w_frac * h_frac, 3),
        "bottom": round(y2 / frame_h, 3),
        "w_frac": round(w_frac, 3), "h_frac": round(h_frac, 3),
    }


def _sweep_offsets() -> List[int]:
    """Pan offsets for one sweep, centre-outward: [0, -u1, +u1, -u2, +u2, ...].

    Step size is a fraction of the half-FOV so consecutive looks overlap by
    SWEEP_OVERLAP_FRAC; SWEEP_MARGIN_DEG is held back from PAN_SWEEP_UNITS so
    the rover's own mount never dominates the extreme frame. [0] (centre-only)
    if the pan constant is unmeasured — never fabricate a bearing from zero.
    """
    if GIMBAL_DEG_PER_UNIT <= 0:
        log.warning("[mission] gimbal_deg_per_unit_unmeasured — search will not pan")
        return [0]

    half_fov_deg = CAMERA_HFOV_DEG / 2.0
    step_deg = half_fov_deg * (1.0 - SWEEP_OVERLAP_FRAC)
    step_units = max(1, int(round(step_deg / GIMBAL_DEG_PER_UNIT)))
    margin_units = int(round(SWEEP_MARGIN_DEG / GIMBAL_DEG_PER_UNIT))
    max_units = max(0, PAN_SWEEP_UNITS - margin_units)

    offsets = [0]
    unit = step_units
    while unit <= max_units:
        offsets.append(-unit)
        offsets.append(unit)
        unit += step_units
    if len(offsets) == 1:
        log.warning("[mission] sweep_offsets_degenerate: pan range %d units minus margin "
                    "leaves %d, less than one step (%d) — search will not pan; lower "
                    "SWEEP_MARGIN_DEG or raise PAN_SWEEP_UNITS",
                    PAN_SWEEP_UNITS, max_units, step_units)
    return offsets


async def _grab_sharp(mission: MissionSession, rover, filename: str) -> dict:
    """Settle, grab, analyse sharpness; retry up to APPROACH_FRAME_RETRIES.

    The VLM is never called on a blurry frame. On final failure the frame (if
    any) is still saved — caller records it as kind="skipped" so
    report.timings.checks_skipped counts it, matching the existing _skip
    convention.
    """
    attempts = 0
    frame = None
    stats = None
    for attempt in range(APPROACH_FRAME_RETRIES + 1):
        attempts = attempt + 1
        await asyncio.sleep(APPROACH_SETTLE_S)
        frame = await asyncio.to_thread(rover.get_frame)
        if frame is None:
            continue
        stats = await asyncio.to_thread(frames.analyse, frame)
        if stats is not None and stats.sharpness >= APPROACH_MIN_SHARPNESS:
            path = _save_frame(mission, filename, frame)
            return {"frame": frame, "sharpness": stats.sharpness, "path": path,
                    "width": stats.width, "height": stats.height,
                    "attempts": attempts, "ok": True}

    path = _save_frame(mission, filename, frame) if frame else None
    return {"frame": None, "sharpness": stats.sharpness if stats else None,
            "path": path, "width": None, "height": None,
            "attempts": attempts, "ok": False}


def _plausible(loc: dict, prev_fill: Optional[float], hops_done: int) -> Optional[str]:
    """Pure, offline-runnable rejection gate (run it over tools/f3_frames/).
    Returns a reject reason, or None if the detection stands. hops_done is
    kept in the signature per spec even though no current rule reads it."""
    if not loc.get("visible"):
        return None

    fill = loc.get("fill")
    w_frac = loc.get("w_frac")
    h_frac = loc.get("h_frac")
    x_center = loc.get("x_center")

    if fill is not None and fill > MAX_FILL:
        return "degenerate_fill"
    if (w_frac is not None and w_frac > MAX_BOX_SIDE_FRAC) or \
       (h_frac is not None and h_frac > MAX_BOX_SIDE_FRAC):
        return "degenerate_full_frame"
    if fill is not None and fill < MIN_FILL:
        return "degenerate_tiny"
    if (prev_fill is not None and fill is not None
            and prev_fill < NEAR_FILL and fill > prev_fill * FILL_JUMP_MAX):
        return "fill_jump"
    if (x_center is not None and fill is not None
            and abs(x_center - 0.5) <= 0.003 and fill > 0.3):
        return "degenerate_centre_box"
    return None



async def approach_controller(mission: MissionSession, rover, step: dict, emit: Emit) -> dict:
    target = str(step.get("target") or "").strip()
    started = time.time()
    cycle = 0
    low_pending: Dict[int, dict] = {}  # keyed by pan offset, per spec §B

    has_gimbal = hasattr(rover, "set_gimbal")

    def set_gimbal(pan: Optional[int] = None, tilt: Optional[int] = None) -> None:
        if has_gimbal:
            rover.set_gimbal(pan=pan, tilt=tilt)

    async def grab_and_locate(filename: str) -> dict:
        grabbed = await _grab_sharp(mission, rover, filename)
        if not grabbed["ok"]:
            return {"visible": False, "bbox_2d": None, "vlm_said_visible": False,
                    "_skip": True, "sharpness": grabbed["sharpness"],
                    "attempts": grabbed["attempts"], "_latency": None,
                    "_path": grabbed["path"]}
        t0 = time.perf_counter()
        raw = await asyncio.to_thread(locate_target, grabbed["frame"], target)
        latency = round(time.perf_counter() - t0, 2)
        loc = _derive_loc(raw, grabbed["width"], grabbed["height"])
        loc["confidence"] = raw.get("confidence")
        loc["sharpness"] = grabbed["sharpness"]
        loc["attempts"] = grabbed["attempts"]
        loc["_latency"] = latency
        loc["_path"] = grabbed["path"]
        return loc

    async def record(phase: str, action_desc: str, loc: dict, sonar_mm: Optional[int],
                     extra: Optional[dict] = None) -> dict:
        entry = {
            "kind": "approach_cycle", "cycle": cycle, "phase": phase,
            "t_rel_s": round(time.time() - started, 2),
            "bbox": loc.get("bbox_2d"), "x_center": loc.get("x_center"),
            "fill": loc.get("fill"), "bottom": loc.get("bottom"),
            "confidence": loc.get("confidence"), "sharpness": loc.get("sharpness"),
            "frame_attempts": loc.get("attempts"),
            "vlm_said_visible": loc.get("vlm_said_visible"),
            "accepted_as_visible": loc.get("visible"),
            "sonar_mm": sonar_mm, "action": action_desc,
            "vlm_latency_s": loc.get("_latency"), "frame_path": loc.get("_path"),
        }
        if extra:
            entry.update(extra)
        mission.checks.append(entry)
        log.info("[mission %s] approach cycle %d (%s): %s", mission.session_id,
                 cycle, phase, action_desc)
        await emit({"type": "approach_cycle", "session_id": mission.session_id, "check": entry})
        return entry

    async def sonar_now() -> Optional[int]:
        if not hasattr(rover, "telemetry_snapshot"):
            return None
        telemetry = await asyncio.to_thread(rover.telemetry_snapshot)
        return telemetry.get("sonar_mm")

    def gate_for(loc: dict, pan_offset: int, prev_fill: Optional[float],
                hops_done: int) -> Tuple[Optional[dict], str]:
        """_plausible, then the pan-keyed low-confidence second-look gate.
        Returns (accepted_loc_or_None, gate_label)."""
        if not loc.get("visible"):
            low_pending.pop(pan_offset, None)
            return None, "not_visible"

        reject = _plausible(loc, prev_fill, hops_done)
        if reject is not None:
            low_pending.pop(pan_offset, None)
            return None, reject

        if loc.get("confidence") == "low":
            pending = low_pending.get(pan_offset)
            if pending is None:
                low_pending[pan_offset] = loc
                return None, "low_pending"
            if abs((loc.get("x_center") or 0) - (pending.get("x_center") or 0)) <= 0.15:
                low_pending.pop(pan_offset, None)
                return loc, "low_confirmed"
            low_pending[pan_offset] = loc
            return None, "low_pending"

        low_pending.pop(pan_offset, None)
        return loc, "confirmed"

    set_gimbal(pan=0, tilt=APPROACH_TILT_OFFSET)

    # -- SEARCH: chassis stationary, gimbal does the looking -----------------
    bearing_deg = None
    found_loc = None
    vlm_calls = 0
    search_offsets = _sweep_offsets()

    for sweep in range(SEARCH_MAX_SWEEPS):
        for pan_offset in search_offsets:
            if vlm_calls >= SEARCH_MAX_VLM_CALLS:
                break
            accepted = None
            pan_deg = pan_offset * GIMBAL_DEG_PER_UNIT if GIMBAL_DEG_PER_UNIT > 0 else None
            while True:
                cycle += 1
                set_gimbal(pan=pan_offset)
                loc = await grab_and_locate("ap_s%d_p%d.jpg" % (sweep, pan_offset))
                if loc.get("_skip"):
                    await record("search", "skipped_blurry", loc, None,
                                 {"sweep": sweep, "pan_offset": pan_offset})
                    mission.checks[-1]["kind"] = "skipped"
                    mission.checks[-1]["reason"] = "blurry"
                    break
                vlm_calls += 1
                accepted, gate = gate_for(loc, pan_offset, None, 0)
                reject_reason = gate if gate not in ("confirmed", "low_confirmed",
                                                     "low_pending", "not_visible") else None
                await record("search", "look pan=%d gate=%s" % (pan_offset, gate), loc, None,
                             {"sweep": sweep, "pan_offset": pan_offset, "pan_deg": pan_deg,
                              "gate": gate, "reject_reason": reject_reason})
                if gate != "low_pending" or vlm_calls >= SEARCH_MAX_VLM_CALLS:
                    break
            if accepted is not None:
                bearing_deg = (pan_deg or 0.0) + (accepted["x_center"] - 0.5) * CAMERA_HFOV_DEG
                found_loc = accepted
                break
        if found_loc is not None or vlm_calls >= SEARCH_MAX_VLM_CALLS:
            break
        set_gimbal(pan=0)
        if SWEEP_ADVANCE_DEG > 0 and FREE_TURN_DEG_PER_S > 0:
            pivot_s = SWEEP_ADVANCE_DEG / FREE_TURN_DEG_PER_S
            await asyncio.to_thread(rover.pivot, SEARCH_DIR, pivot_s)
            cycle += 1
            await record("search", "sweep_advance_pivot", {"visible": False}, None,
                         {"sweep": sweep, "pivot_requested_deg": SWEEP_ADVANCE_DEG,
                          "pivot_cmd_s": pivot_s, "pivot_dir": SEARCH_DIR})

    if found_loc is None:
        return {"status": "blocked",
                "detail": {"reason": "target_not_found", "cycles": cycle}}

    # -- LOCK: re-centre gimbal, ONE pivot toward the measured bearing -------
    set_gimbal(pan=0)
    pivot_dir = 0
    pivot_s = 0.0
    if abs(bearing_deg) >= MIN_PIVOT_DEG and FREE_TURN_DEG_PER_S > 0:
        pivot_dir = 1 if bearing_deg > 0 else -1
        pivot_s = max(MIN_PIVOT_S, abs(bearing_deg) / FREE_TURN_DEG_PER_S)
        await asyncio.to_thread(rover.pivot, pivot_dir, pivot_s)
    cycle += 1

    await asyncio.sleep(APPROACH_SETTLE_S)
    verify = await grab_and_locate("ap_lock_verify.jpg")
    if verify.get("_skip"):
        accepted, gate = None, "skipped_blurry"
    else:
        accepted, gate = gate_for(verify, 0, None, 0)
    residual_e = (accepted["x_center"] - 0.5) if accepted and accepted.get("x_center") is not None else None

    await record("lock", "verify pivot_dir=%s pivot_s=%.2f gate=%s" % (pivot_dir, pivot_s, gate),
                 verify, None,
                 {"bearing_deg": round(bearing_deg, 2),
                  "pivot_requested_deg": round(bearing_deg, 2),
                  "pivot_cmd_s": round(pivot_s, 2) if pivot_dir else None,
                  "pivot_dir": pivot_dir or None,
                  "residual_e": round(residual_e, 3) if residual_e is not None else None,
                  "gate": gate})

    if residual_e is not None and abs(residual_e) > APPROACH_RESIDUAL_FLAG_E:
        log.warning("[mission %s] bearing_residual_high: residual_e=%.3f",
                    mission.session_id, residual_e)

    loc = dict(verify) if verify.get("visible") else {"visible": False, "bbox_2d": None}
    loc["gate_trusted"] = accepted is not None

    # -- APPROACH --------------------------------------------------------------
    deadline = started + APPROACH_MAX_S
    hops_done = 0
    prev_fill: Optional[float] = loc.get("fill") if loc.get("gate_trusted") else None
    lost = 0

    def _merge(raw: dict, gate_accepted: Optional[dict]) -> dict:
        """raw is the geometric detection from _derive_loc (has x_center/fill
        whenever vlm_said_visible+valid bbox, regardless of gate). Centring
        uses this always. Only fill-sensitive decisions (hop size, arrival,
        prev_fill bookkeeping) key off gate_trusted — a low_pending/fill_jump
        box still has a real bearing worth steering toward, it just hasn't
        earned trust for *distance* yet."""
        merged = dict(raw) if raw.get("visible") else {"visible": False, "bbox_2d": None}
        merged["gate_trusted"] = gate_accepted is not None
        return merged

    while cycle < APPROACH_MAX_CYCLES and time.time() < deadline:
        cycle += 1

        if not loc.get("visible"):
            lost += 1
            if lost >= 2:
                loc2 = await grab_and_locate("ap_%02d_relocate.jpg" % cycle)
                accepted, gate = (None, "skipped_blurry") if loc2.get("_skip") \
                    else gate_for(loc2, 0, prev_fill, hops_done)
                await record("approach", "relocate_look gate=%s" % gate, loc2, None, {"gate": gate})
                if not loc2.get("visible"):
                    return {"status": "blocked",
                            "detail": {"reason": "target_not_found", "cycles": cycle}}
                loc = _merge(loc2, accepted)
                lost = 0
                continue
            back_cm = HOP_BACK_S * getattr(rover, "free_speed_cmps", 15.0)
            hop_result = await asyncio.to_thread(rover.hop, back_cm, True)
            loc2 = await grab_and_locate("ap_%02d_lostlook.jpg" % cycle)
            accepted, gate = (None, "skipped_blurry") if loc2.get("_skip") \
                else gate_for(loc2, 0, prev_fill, hops_done)
            await record("approach", "hop_back %.1fs gate=%s" % (HOP_BACK_S, gate), loc2, None,
                         {"gate": gate, "hop_moved_s": hop_result.get("moved_s")})
            loc = _merge(loc2, accepted)
            continue
        lost = 0

        e = loc["x_center"] - 0.5
        sonar_mm = await sonar_now()
        trusted = loc.get("gate_trusted")

        sonar_hit = sonar_mm is not None and sonar_mm <= ARRIVE_MM and abs(e) <= CENTER_TOL
        vision_hit = trusted and (
            (loc.get("fill") is not None and loc["fill"] >= ARRIVE_FILL)
            or (loc.get("bottom") is not None and loc["bottom"] >= ARRIVE_BOTTOM)
        )
        arrived = False
        arrival_reason = None
        arrival_block = None
        if sonar_hit:
            arrived, arrival_reason = True, "sonar"
            if hops_done == 0:
                arrival_block = "arrival_without_motion"
        elif vision_hit and hops_done >= MIN_HOPS_BEFORE_ARRIVAL:
            sonar_contradicts = sonar_mm is not None and sonar_mm > ARRIVE_SONAR_SANITY_MM
            non_shrinking = prev_fill is None or (loc.get("fill") or 0) >= prev_fill
            if non_shrinking and not sonar_contradicts:
                arrived = True
                arrival_reason = "fill" if (loc.get("fill") or 0) >= ARRIVE_FILL else "bottom"
            elif sonar_contradicts:
                arrival_block = "vision_arrival_sonar_contradicted"

        if arrived:
            if APPROACH_ARRIVAL_CROSSCHECK:
                await _arrival_check(mission, rover, target, emit, keep_tilt=APPROACH_TILT_OFFSET)
                cross = (mission.arrival or {}).get("result") or {}
                if "_error" not in cross and cross.get("target_visible") is False:
                    back_cm = HOP_BACK_S * getattr(rover, "free_speed_cmps", 15.0)
                    await asyncio.to_thread(rover.hop, back_cm, True)
                    recheck = await grab_and_locate("ap_%02d_arrival_recheck.jpg" % cycle)
                    accepted, gate = (None, "skipped_blurry") if recheck.get("_skip") \
                        else gate_for(recheck, 0, prev_fill, hops_done)
                    await record("approach", "arrival_recheck gate=%s" % gate, recheck, sonar_mm,
                                 {"gate": gate, "arrival_block": "arrival_contradicted"})
                    if not recheck.get("visible"):
                        return {"status": "blocked",
                                "detail": {"reason": "arrival_contradicted", "cycles": cycle,
                                           "arrival_cross_check": "contradicted"}}
                    loc = _merge(recheck, accepted)
                    continue

            await record("approach", "arrived (%s)" % arrival_reason, loc, sonar_mm,
                         {"hops_done": hops_done, "fill_prev": prev_fill,
                          "arrival_block": arrival_block})
            return {"status": "ok",
                    "detail": {"reason": arrival_reason, "cycles": cycle,
                               "arrival_cross_check": "confirmed" if APPROACH_ARRIVAL_CROSSCHECK else "skipped",
                               "gimbal_only_lock": True}}

        action_desc = ""
        pivot_dir2 = None
        pivot_s2 = None
        if abs(e) > CENTER_TOL:
            pivot_dir2 = 1 if e > 0 else -1
            pivot_s2 = PIVOT_S_PER_UNIT * abs(e)
            await asyncio.to_thread(rover.pivot, pivot_dir2, pivot_s2)
            action_desc = "pivot %s %.2fs " % ("R" if pivot_dir2 > 0 else "L", pivot_s2)

        # Untrusted fill -> don't guess near/far, just take the cautious hop.
        hop_cm = HOP_NEAR_CM if (not trusted or loc.get("fill", 0) > NEAR_FILL) else HOP_CM
        hop_result = await asyncio.to_thread(rover.hop, hop_cm)
        action_desc += "hop %d" % hop_cm
        hops_done += 1
        if trusted:
            prev_fill = loc.get("fill")

        if hop_result.get("status") == "sonar_stop":
            sonar_mm = hop_result.get("sonar_mm")
            if trusted and loc.get("fill", 0) > NEAR_FILL and abs(e) <= CENTER_TOL:
                await record("approach", action_desc + " sonar_stop", loc, sonar_mm,
                             {"pivot_cmd_s": pivot_s2, "pivot_dir": pivot_dir2,
                              "hop_moved_s": hop_result.get("moved_s"), "hops_done": hops_done})
                await _arrival_check(mission, rover, target, emit, keep_tilt=APPROACH_TILT_OFFSET)
                return {"status": "ok",
                        "detail": {"reason": "sonar", "cycles": cycle,
                                   "arrival_cross_check": "confirmed", "gimbal_only_lock": True}}
            await record("approach", action_desc + " sonar_stop_blocked", loc, sonar_mm,
                         {"pivot_cmd_s": pivot_s2, "pivot_dir": pivot_dir2,
                          "hop_moved_s": hop_result.get("moved_s")})
            return {"status": "blocked",
                    "detail": {"reason": "obstacle", "sonar_mm": sonar_mm, "cycles": cycle}}

        loc2 = await grab_and_locate("ap_%02d_check.jpg" % cycle)
        accepted, gate = (None, "skipped_blurry") if loc2.get("_skip") \
            else gate_for(loc2, 0, prev_fill, hops_done)
        reject_reason = gate if gate not in ("confirmed", "low_confirmed",
                                             "low_pending", "not_visible") else None
        await record("approach", action_desc + " gate=%s" % gate, loc2, None,
                     {"pivot_cmd_s": pivot_s2, "pivot_dir": pivot_dir2,
                      "hop_moved_s": hop_result.get("moved_s"), "gate": gate,
                      "hops_done": hops_done, "fill_prev": prev_fill,
                      "reject_reason": reject_reason, "arrival_block": arrival_block})
        loc = _merge(loc2, accepted)

    return {"status": "blocked", "detail": {"reason": "cycle_budget_exhausted", "cycles": cycle}}



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
            use_free_approach = (
                action == "approach"
                and config.get("NAV_MODE", "line").strip().lower() == "free"
                and all(hasattr(rover, name) for name in ("pivot", "hop", "get_frame", "set_gimbal"))
            )

            try:
                if use_free_approach:
                    result = await approach_controller(mission, rover, step, emit)
                else:
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
