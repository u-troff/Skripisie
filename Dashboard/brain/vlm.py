import json
import time
from typing import Any, Dict, List, Optional

from log_setup import get_logger
from providers import ImageSource, ProviderError, get_provider, log_completion, user_message

log = get_logger("vlm")

# ImageSource is re-exported so pipeline.py's existing import keeps working.
__all__ = ["ImageSource", "check_ambiguity", "verify_plan"]


def check_ambiguity(
    command: str,
    image: Optional[ImageSource] = None,
    history: Optional[List[Dict[str, str]]] = None,
) -> dict:
    """One round of the clarification loop.

    history is the question/answer pairs already exchanged. Passing it is what
    turns the old single-shot check into a multi-turn conversation: the model
    sees what it already asked and either asks something new or declares the
    command resolved.
    """
    lines = [
        "You are checking whether a robot command is unambiguous enough to execute.",
        "Use the image of the robot's current view, if given, to decide.",
        f'Original command: "{command}"',
    ]
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


def verify_plan(plan: dict, image: Optional[ImageSource] = None) -> dict:
    prompt = (
        "You are verifying a robot's mission plan before it departs.\n"
        f"Plan: {json.dumps(plan)}\n"
        "Respond ONLY with JSON: "
        '{"verified": true/false, "concerns": "..." or null}'
    )
    return _ask("verify_plan", prompt, image)

def describe_frame(image:ImageSource)->str:
    """One sentence about what the rover can see. This is the only per-frame
    model call, so it stays short — the digest is what gets reasoned over."""
    prompt = (
        "Describe what a robot's camera is seeing, in one short sentence. "
        "Name visible objects, their rough positions, and anything blocking a path.\n"
        'Respond ONLY with JSON: {"description": "..."}'
    )
    return str(_ask("describe_frame", prompt, image).get("description") or "").strip()

def _ask(stage: str, prompt: str, image: Optional[ImageSource]) -> dict:
    started = time.perf_counter()
    try:
        provider = get_provider("vlm")
    except ProviderError as exc:
        log.error("[%s] no provider: %s", stage, exc)
        return {}

    image_note = f"{len(image)} bytes" if isinstance(image, bytes) else ("path" if image else "none")
    log.info("[%s] -> %s/%s (image=%s)", stage, provider.name, provider.model, image_note)
    log.debug("[%s] prompt:\n%s", stage, prompt)

    try:
        completion = provider.complete([user_message(prompt)], image=image, json_mode=True)
    except ProviderError as exc:
        # Don't take the whole request down because one model call failed.
        # Callers use .get() with defaults, so {} degrades gracefully.
        log.error("[%s] failed after %.1fs: %s", stage, time.perf_counter() - started, exc)
        return {}

    log.info("[%s] <- %.1fs", stage, completion.latency_s)
    log.debug("[%s] raw response:\n%s", stage, completion.text)
    log_completion(log, "vlm", stage, completion)

    try:
        parsed: Dict[str, Any] = json.loads(completion.text)
    except (json.JSONDecodeError, TypeError):
        log.warning("[%s] unparseable JSON, returning {}: %r", stage, completion.text[:300])
        return {}

    log.info("[%s] parsed: %s", stage, json.dumps(parsed)[:300])
    return parsed
