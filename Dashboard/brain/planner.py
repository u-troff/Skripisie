import json
import time

from log_setup import get_logger
from providers import ProviderError, get_provider, log_completion, user_message

log = get_logger("planner")


# The rover drives and looks. Nothing else. A plan step it cannot perform is a
# failed plan, not a partial success, so the vocabulary is stated up front
# rather than filtered afterwards.
ACTIONS = {
    "move": "drive forward or backward",
    "turn": "rotate on the spot",
    "approach": "drive toward a visible target until close to it",
    "scan": "sweep the camera around without moving the base",
    "observe": "hold still and look at a named target",
    "stop": "halt",
    "report": "say what was found",
}

_VOCABULARY = (
    "The rover can ONLY perform these actions:\n"
    + "\n".join(f'  "{verb}" - {what}' for verb, what in ACTIONS.items())
    + "\nIt has no arm. It cannot pick up, carry, open, push, or touch anything.\n"
    'Every step\'s "action" must be exactly one of those verbs.'
)


def generate_plan(command: str, scene: str = "") -> dict:
    scene_block = ""
    if scene:
        scene_block = (
            "The room was filmed beforehand. Everything known to be in it:\n"
            + scene
            + "\nOnly reference things from that list. If the command needs something "
            "that is not there, say so in notes instead of inventing it.\n"
        )

    prompt = (
        "You are a mission planner for an indoor rover. Break the command into "
        "a sequence of discrete, executable steps.\n"
        + _VOCABULARY + "\n"
        + scene_block
        + f'Command: "{command}"\n'
        "Respond ONLY with JSON: "
        '{"steps": [{"id": 1, "action": "...", "target": "..."}], "notes": "..."}'
    )

    started = time.perf_counter()
    try:
        provider = get_provider("planner")
    except ProviderError as exc:
        log.error("[plan] no provider: %s", exc)
        return {"steps": [], "notes": f"planner unavailable: {exc}"}

    log.info("[plan] -> %s/%s command=%r", provider.name, provider.model, command)
    log.debug("[plan] prompt:\n%s", prompt)

    try:
        completion = provider.complete([user_message(prompt)], json_mode=True)
    except ProviderError as exc:
        log.error("[plan] failed after %.1fs: %s", time.perf_counter() - started, exc)
        return {"steps": [], "notes": f"planner failed: {exc}"}

    log.info("[plan] <- %.1fs", completion.latency_s)
    log.debug("[plan] raw response:\n%s", completion.text)
    log_completion(log, "planner", "generate_plan", completion)

    try:
        plan = json.loads(completion.text)
    except (json.JSONDecodeError, TypeError):
        log.warning("[plan] unparseable JSON: %r", completion.text[:300])
        return {"steps": [], "notes": "planner returned unparseable output"}

    steps = plan.get("steps", [])
    log.info("[plan] %d step(s)", len(steps))
    for step in steps:
        log.info("       %s. %s -> %s", step.get("id"), step.get("action"), step.get("target"))
    return plan

def revise_plan(confirmed_plan: dict, remaining_steps: list, digest: list, command: str) -> dict:
    """Propose a revision given what the rover can now see.

    The planner only ever *proposes*; revision.classify decides in plain Python
    whether the human has to be asked.
    """
    prompt = (
        "You are re-checking a rover's plan while it is already moving.\n"
        f'Original command: "{command}"\n'
        f"Confirmed plan: {json.dumps(confirmed_plan)}\n"
        f"Steps not yet done: {json.dumps(remaining_steps)}\n"
        "What the rover has seen since departing, oldest first:\n"
        + "\n".join(f"  - {line}" for line in digest)
        + "\nOnly propose a change if what it sees makes the remaining steps wrong or "
        "impossible. Prefer keeping the same targets. If nothing needs to change, say so.\n"
        "Respond ONLY with JSON: "
        '{"change": true/false, "reason": "...", '
        '"steps": [{"id": 1, "action": "...", "target": "..."}]}'
    )

    try:
        provider = get_provider("planner")
        completion = provider.complete([user_message(prompt)], json_mode=True)
    except ProviderError as exc:
        log.error("[revise] failed: %s", exc)
        return {"change": False, "reason": f"revision unavailable: {exc}", "steps": []}

    log.info("[revise] <- %.1fs", completion.latency_s)
    log_completion(log, "planner", "revise_plan", completion)

    try:
        return json.loads(completion.text)
    except (json.JSONDecodeError, TypeError):
        log.warning("[revise] unparseable JSON: %r", completion.text[:300])
        return {"change": False, "reason": "unparseable revision output", "steps": []}
