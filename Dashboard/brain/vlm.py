import json
import time
from typing import Any, Dict, List, Optional

from log_setup import get_logger
from providers import ImageSource, ProviderError, get_provider, log_completion, user_message

log = get_logger("vlm")

# ImageSource is re-exported so pipeline.py's existing import keeps working.
__all__ = ["ImageSource", "check_ambiguity", "verify_plan", "describe_frame",
           "inventory_frame", "failed"]

# Marker key on a result that never reached a usable answer. Without this a
# failed call returns {} and .get("ambiguous") is falsy, so a broken model reads
# as "the command was perfectly clear" — which is how a flaky VLM turns into a
# pipeline that silently skips clarification.
ERROR_KEY = "_error"


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
        "List EVERY distinct object you can see, even small ones. Do not summarise.\n"
        "Respond ONLY with JSON: "
        '{"place": "short name for this part of the room", '
        '"objects": [{"name": "...", "attributes": ["colour", "size"], '
        '"where": "where it is, relative to the room or other objects"}], '
        '"obstacles": ["anything on the floor that would block a small wheeled robot"]}'
    )
    return _ask("inventory_frame", prompt, image)


def _ask(stage: str, prompt: str, image: Optional[ImageSource]) -> dict:
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
        parsed: Dict[str, Any] = json.loads(completion.text)
    except (json.JSONDecodeError, TypeError):
        log.warning("[%s] unparseable JSON: %r", stage, completion.text[:300])
        return {ERROR_KEY: "unparseable JSON from model"}

    if not isinstance(parsed, dict):
        log.warning("[%s] JSON was not an object: %r", stage, completion.text[:200])
        return {ERROR_KEY: "model returned JSON that was not an object"}

    log.info("[%s] parsed: %s", stage, json.dumps(parsed)[:300])
    return parsed
