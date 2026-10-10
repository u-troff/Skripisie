import json
import time
import config
from typing import Any, Dict, List, Optional, Tuple

from log_setup import get_logger
from providers import ImageSource, ProviderError, get_provider, log_completion, user_message

log = get_logger("vlm")

# ImageSource is re-exported so pipeline.py's existing import keeps working.
__all__ = ["ImageSource", "check_ambiguity", "verify_plan", "describe_frame",
           "inventory_frame", "check_progress", "check_side_look",
           "check_arrival", "failed", "KNOWN_MAX", "locate_target", "distance_cm_from", "VLM_DIST_MAX_CM", "check_condition", "survey_frame"]


# -- distance estimate (spec-supervisor-feedback-2026-10-05.md C2) ----------------
# One extra JSON field on the existing locate_target call: no extra model call. Only
# numbers in (0, VLM_DIST_MAX_CM] are accepted; anything else is None. The hop policy
# (mission._hop_for) uses a FRACTION of it, never the whole thing.
VLM_DIST_MAX_CM = config.get_float("VLM_DIST_MAX_CM", 400.0)
# VLM_DIST_MODE=bucket is the fallback if the numeric estimate fails the go/no-go test
# (tools/f9_distance_calib.py); values are bucket midpoints in cm.
_BUCKETS_CM = {"lt0.3": 15.0, "0.3-0.6": 45.0, "0.6-1": 80.0, "1-2": 150.0, "gt2": 250.0}

# Marker key on a result that never reached a usable answer. Without this a
# failed call returns {} and .get("ambiguous") is falsy, so a broken model reads
# as "the command was perfectly clear" — which is how a flaky VLM turns into a
# pipeline that silently skips clarification.
ERROR_KEY = "_error"
# (scale, yx_order) by model-name prefix. scale 0 = pixels. Anything not listed
# (openai, ...) is pixels in [x1, y1, x2, y2], which is what we ask for.
# scale -1 = pixels of the enlarged view the model actually sees (qwen via
# ollama upscales to >= 1024 image tokens); mission._derive_loc maps it back.
_BBOX_FORMATS = {
    "gemma4": (1000, True),      # normalised 0-1000, [y1, x1, y2, x2]
    "qwen2.5vl": (-1, False),
}


def bbox_format() -> Tuple[int, bool]:
    try:
        model = get_provider("vlm").model.lower()
    except ProviderError:
        return 0, False
    for prefix, fmt in _BBOX_FORMATS.items():
        if model.startswith(prefix):
            return fmt
    return 0, False



def failed(result: Optional[dict]) -> bool:
    return not isinstance(result, dict) or ERROR_KEY in result


def check_ambiguity(
    command: str,
    image: Optional[ImageSource] = None,
    history: Optional[List[Dict[str, str]]] = None,
    scene: str = "",
) -> dict:
    """One round of the clarification loop.

    history is the question/answer pairs already exchanged. Passing it is what
    turns the old single-shot check into a multi-turn conversation: the model
    sees what it already asked and either asks something new or declares the
    command resolved.

    scene is the digest built from the room video. Without it the model is being
    asked whether "the thing by the window" is ambiguous with no way to know how
    many windows there are, so its answer is close to arbitrary.
    """
    lines = [
        "You are checking whether a robot command is unambiguous enough to execute.",
        "The robot can only drive around and look at things. It has no arm.",
        f'Original command: "{command}"',
    ]
    if scene:
        lines.append("The room was filmed beforehand. Everything known to be in it:")
        lines.append(scene)
        lines.append(
            "Judge ambiguity against that list. If two or more things in the room match "
            "the description, it IS ambiguous — ask which one, naming the candidates. "
            "If nothing in the room matches, say so in the reason."
        )
    else:
        lines.append("Use the image of the robot's current view, if given, to decide.")

    if history:
        lines.append("Clarifications already exchanged:")
        for turn in history:
            lines.append("  Q: " + turn.get("question", ""))
            lines.append("  A: " + (turn.get("answer") or "(no answer yet)"))
        lines.append(
            "Do NOT repeat a question that was already answered. If you now have "
            "enough information to act, set ambiguous to false."
        )

    lines.append(
        "resolved_command must name the object only (e.g. \"go to the cardboard box\"). "
        "NEVER include where it appears in the image or camera view (left, right, centre, "
        "edge, foreground, partially visible, in view) — the robot turns, so view-relative "
        "positions are wrong a moment later."
    )
    lines.append(
        "Respond ONLY with JSON: "
        '{"ambiguous": true/false, "reason": "...", '
        '"clarifying_question": "..." or null, '
        '"resolved_command": "the full unambiguous command" or null}'
    )
    return _ask("check_ambiguity", "\n".join(lines), image)


def verify_plan(plan: dict, image: Optional[ImageSource] = None, scene: str = "") -> dict:
    lines = ["You are verifying a robot's mission plan before it departs.",
             "The robot can only drive and look. It cannot pick up or touch anything."]
    if scene:
        lines.append("What is in the room:")
        lines.append(scene)
    lines.append(f"Plan: {json.dumps(plan)}")
    lines.append(
        "Respond ONLY with JSON: "
        '{"verified": true/false, "concerns": "..." or null}'
    )
    return _ask("verify_plan", "\n".join(lines), image)


def describe_frame(image: ImageSource) -> str:
    """One sentence about what the rover can see, for the mid-mission check.

    Kept short on purpose: this runs per changed frame while the rover moves.
    Scene building uses inventory_frame instead.
    """
    prompt = (
        "Describe what a robot's camera is seeing, in one short sentence. "
        "Name visible objects, their rough positions, and anything blocking a path.\n"
        'Respond ONLY with JSON: {"description": "..."}'
    )
    return str(_ask("describe_frame", prompt, image).get("description") or "").strip()


def inventory_frame(image: ImageSource) -> dict:
    """Enumerate one keyframe of the room video.

    Deliberately not describe_frame: a one-sentence summary drops the mug in the
    corner, and a dropped object is one the planner can never be asked about.
    """
    prompt = (
        "You are cataloguing one frame from a video of a room. A robot will later be "
        "told to drive somewhere in this room and look at something.\n"
        "List EVERY distinct object you can see, even small ones — furniture, "
        "equipment, screens, cables, lights, signs, items on shelves and floor. "
        "List at most 12 objects, each object ONCE (never repeat an object or list "
        "several of the same kind separately). Stop when you run out of new objects. "
        "Do not summarise.\n"
        "For each object give specific attributes (colour, material, shape, size, "
        "any text or lights) and where it is relative to the frame and to nearby objects.\n"
        "Respond ONLY with JSON: "
        '{"place": "short name for this part of the room", '
        '"objects": [{"name": "...", "attributes": ["colour", "size"], '
        '"where": "where it is, relative to the room or other objects"}], '
        '"obstacles": ["anything on the floor that would block a small wheeled robot"]}'
    )
    result = _ask("inventory_frame", prompt, image, salvage=True)
    objects = result.get("objects")
    if isinstance(objects, list):   # a looping model repeats the same block; keep the first
        seen, unique = set(), []
        for obj in objects:
            key = json.dumps(obj, sort_keys=True, default=str).lower()
            if key not in seen:
                seen.add(key)
                unique.append(obj)
        result["objects"] = unique
    return result


# --------------------------------------------------------------------------
# Grounded line mission — checks taken while the rover drives, and at arrival.
#
# All three are OBSERVATIONS, never control signals. The IR sensor steers and
# the ultrasonic decides when to stop; at ~15s a frame the VLM is two orders of
# magnitude too slow to be in that loop. A failed call returns {"_error": ...}
# and callers MUST record that as "check failed", never as "target not
# visible" — the whole point of RQ1's evidence is the difference between the
# system saying "I could not see it" and the system hallucinating that it did.
# --------------------------------------------------------------------------
# The room catalogue handed to a check is capped. A 3B model given a long list
# starts reporting things *because they are on the list* — which would invert
# the evidence these checks exist to produce, turning a hallucination measure
# into a hallucination source. Short list, and every check is also asked for
# `unexpected`, so over-claiming at least shows up in the log instead of
# quietly passing as a match.
KNOWN_MAX = 20


def _known_block(known: Optional[List[str]]) -> str:
    """The room-video grounding block, shared by all three drive checks.

    The room was filmed before departure and catalogued by scene.py; this is
    that catalogue as plain names. It is what lets a check answer "I can see
    the desk and the bookshelf" rather than free-associating, and it is the
    only thing that makes `unexpected` meaningful — a name the rover reports
    that the room does not contain is the cleanest RQ2 hallucination signal in
    the run.

    Grounding only. Nothing derived from it steers: the IR sensor and the
    sonar own the driving, at two orders of magnitude more often than a frame
    can be read.
    """
    if not known:
        return ""
    names = json.dumps(list(known)[:KNOWN_MAX])
    return (
        "The room was filmed beforehand. Everything catalogued in it:\n"
        f"{names}\n"
        "List in \"seen\" only the catalogued things you can ACTUALLY see in this "
        "image right now. Do not list something merely because it is on the "
        "list. Anything clearly visible that is NOT on the list goes in "
        "\"unexpected\".\n"
    )


def check_progress(image: ImageSource, target: str,
                   known: Optional[List[str]] = None) -> dict:
    """Mid-route keyframe check. Observation only; never a control signal."""
    prompt = (
        "You are the camera check for a small rover driving along a floor line toward: "
        f'"{target}".\n'
        "Answer only from what is visible in this image.\n"
        + _known_block(known) +
        "Respond ONLY with JSON: "
        '{"target_visible": true/false, "path_clear": true/false, '
        '"seen": ["catalogued things visible now"], '
        '"unexpected": ["visible things not in the catalogue"], '
        '"description": "one short sentence"}'
    )
    return _ask("check_progress", prompt, image)


def check_arrival(image: ImageSource, target: str, expected: Optional[List[str]] = None,
                  known: Optional[List[str]] = None) -> dict:
    """Arrival check for the report.

    `expected` is the narrow list — the target plus whatever the command itself
    named. `known` is the wider room catalogue. Both are reported against, but
    only `expected` drives `missing`: report.py flags a non-empty `missing` as
    an uncertainty, and scoring the whole room inventory as "missing" would
    make that flag fire on every run and stop meaning anything.
    """
    prompt = (
        f'A rover has stopped where it should be able to see: "{target}".\n'
        f"Other things that may be nearby: {json.dumps(list(expected or []))}\n"
        "Answer only from what is visible in this image. Do not assume.\n"
        + _known_block(known) +
        "Respond ONLY with JSON: "
        '{"target_visible": true/false, "confidence": "high"|"medium"|"low", '
        '"seen": ["things from the lists that ARE visible"], '
        '"missing": ["things from the NEARBY list that are NOT visible"], '
        '"unexpected": ["visible things not in either list"], '
        '"description": "one or two sentences"}'
    )
    return _ask("check_arrival", prompt, image)


def check_side_look(image: ImageSource, target: str,
                    known: Optional[List[str]] = None) -> dict:
    """The one planned stop: what is off to the left of the route.

    The check that gains most from the catalogue — it is a deliberate "what is
    over there" observation with no target to anchor it, so without the room
    list the model has nothing to be right or wrong against.
    """
    prompt = (
        "This frame was taken with the rover's camera turned LEFT, off its direction of travel.\n"
        f'The rover is heading toward: "{target}".\n'
        + _known_block(known) +
        "Respond ONLY with JSON: "
        '{"objects": ["..."], "target_visible": true/false, '
        '"seen": ["catalogued things visible now"], '
        '"unexpected": ["visible things not in the catalogue"], '
        '"description": "one sentence"}'
    )
    return _ask("check_side_look", prompt, image)

# 0 = the model returns pixels; N>0 = it returns 0..N normalised (gemma4: 1000).

def survey_frame(image: ImageSource) -> dict:
    """L5: what is around the rover now. Unlike describe_frame (one sentence) this lists
    every object, so "what is near the X" gets an inventory instead of a summary."""
    prompt = (
        "A rover has arrived at its target and is looking around it. List EVERY distinct object "
        "you can see in this image, including small ones and anything on the floor. List each "
        "object ONCE, at most 10. For each give its colour and where it is in the frame. "
        "Do not summarise and do not guess.\n"
        'Respond ONLY with JSON: {"objects": ["colour name, position in frame"], '
        '"description": "one short sentence"}'
    )
    return _ask("survey_frame", prompt, image, salvage=True)


def check_condition(image: ImageSource, condition: str) -> dict:
    """L4: is this statement true of what the camera sees? Answers "unsure" rather than guess."""
    prompt = (
        "A rover's camera took this image. Decide whether this statement is true of what is visible: "
        f'"{condition}"\n'
        'Answer only from what is visible. If you cannot tell, answer "unsure". Do not assume.\n'
        'Respond ONLY with JSON: {"answer": "yes"|"no"|"unsure", "reason": "one short sentence"}'
    )
    return _ask("check_condition", prompt, image)



def locate_target(image: ImageSource, target: str) -> dict:
    """Bounding-box localisation for the free-roam approach loop
    (spec-free-roam-approach.md §3B). Distinct from check_progress/
    check_arrival: those ask yes/no against a room catalogue, this asks
    WHERE, in pixels, so mission.py can derive x_center/fill/bottom itself
    rather than trusting the model's own notion of "left" or "close"."""

    scale,yx = bbox_format()

    if scale > 0:
        order = "[y1, x1, y2, x2]" if yx else "[x1, y1, x2, y2]"
        coords = (f"give its bounding box as {order}, each coordinate normalised to 0-{scale} "
                  "(0,0 = top-left of the image).\n")
    else:
        order = "[x1, y1, x2, y2]"
        coords = "give its bounding box in pixel coordinates.\n"

    dist_field,dist_note = _distance_prompt()
        
    prompt = (
        f'Find "{target}" in this image from a small floor robot\'s camera.\n'
        "If it is visible, " + coords + dist_note +
        "Respond ONLY with JSON: "
        '{"visible": true/false, "bbox_2d": ' + order +' or null, '
        '"confidence": "high"|"medium"|"low", ' + dist_field + ', "description": "one short sentence"}'
    )
    return _ask("locate_target", prompt, image)

#this funciton calls the provider to give the prompt to the LLM

def _salvage_truncated_json(text: str) -> Optional[dict]:
    """Recover a reply cut off by num_predict (typically a repetition loop).

    Walks the text tracking strings and the open-bracket stack; every point just
    after a closing } or ] is a candidate cut. Tries the latest first: truncate
    there, close whatever is still open, and keep the first cut that parses.
    Returns None if nothing usable remains.
    """
    stack: List[str] = []
    cuts: List[Tuple[int, str]] = []   # (end index, closers needed from that point)
    in_str = escaped = False
    for i, ch in enumerate(text):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack:
                break
            stack.pop()
            if stack:   # an empty stack means the JSON was complete — not our case
                cuts.append((i + 1, "".join(reversed(stack))))
    for end, closers in reversed(cuts[-20:]):
        try:
            parsed = json.loads(text[:end] + closers)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _ask(stage: str, prompt: str, image: Optional[ImageSource], salvage: bool = False) -> dict:
    started = time.perf_counter()
    try:
        provider = get_provider("vlm")
    except ProviderError as exc:
        log.error("[%s] no provider: %s", stage, exc)
        return {ERROR_KEY: f"no provider: {exc}"}

    image_note = f"{len(image)} bytes" if isinstance(image, bytes) else ("path" if image else "none")
    log.info("[%s] -> %s/%s (image=%s)", stage, provider.name, provider.model, image_note)
    log.debug("[%s] prompt:\n%s", stage, prompt)

    try:
        completion = provider.complete([user_message(prompt)], image=image, json_mode=True)
    except ProviderError as exc:
        log.error("[%s] failed after %.1fs: %s", stage, time.perf_counter() - started, exc)
        return {ERROR_KEY: str(exc)}

    log.info("[%s] <- %.1fs", stage, completion.latency_s)
    log.debug("[%s] raw response:\n%s", stage, completion.text)
    log_completion(log, "vlm", stage, completion)

    try:
        parsed: Optional[Dict[str, Any]] = json.loads(completion.text)
    except (json.JSONDecodeError, TypeError):
        log.warning("[%s] unparseable JSON: %r", stage, completion.text[:300])
        parsed = _salvage_truncated_json(completion.text) if salvage and isinstance(completion.text, str) else None
        if parsed is None:
            return {ERROR_KEY: "unparseable JSON from model"}
        log.warning("[%s] salvaged truncated JSON (%d chars)", stage, len(completion.text))

    if not isinstance(parsed, dict):
        log.warning("[%s] JSON was not an object: %r", stage, completion.text[:200])
        return {ERROR_KEY: "model returned JSON that was not an object"}

    log.info("[%s] parsed: %s", stage, json.dumps(parsed)[:300])
    return parsed

def _distance_prompt() -> Tuple[str,str]:
    """(json field, one instruction line) for the distance estimate."""
    if config.get("VLM_DIST_MODE", "numeric").strip().lower() == "bucket":
        return ('"distance_bucket": "lt0.3"|"0.3-0.6"|"0.6-1"|"1-2"|"gt2" or null',
                "Also give distance_bucket: your best estimate of the horizontal distance from the "
                "camera to the point where the object touches the floor, in metres (lt0.3 = under "
                "0.3, gt2 = over 2), or null if you cannot tell.\n")
    return ('"distance_m": number or null',
            "Also give distance_m: your best estimate, in metres, of the horizontal distance from "
            "the camera to the point where the object touches the floor, or null if you cannot tell.\n")

def distance_cm_from(raw) -> Optional[float]:
    """The VLM's distance estimate in cm, or None. Never raises; unknown keys are ignored."""
    if not isinstance(raw, dict):
        return None
    if config.get("VLM_DIST_MODE", "numeric").strip().lower() == "bucket":
        return _BUCKETS_CM.get(str(raw.get("distance_bucket") or "").strip().lower().replace(" ", ""))
    value = raw.get("distance_m")
    if isinstance(value, bool):
        return None
    try:
        cm = float(value) * 100.0
    except (TypeError, ValueError):
        return None
    return round(cm, 1) if 0 < cm <= VLM_DIST_MAX_CM else None   # NaN fails the comparison
