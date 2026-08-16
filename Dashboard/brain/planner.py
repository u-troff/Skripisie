import json
import time

from log_setup import get_logger
from providers import ProviderError, get_provider, log_completion, user_message

log = get_logger("planner")


def generate_plan(command: str) -> dict:
    prompt = (
        "You are a mission planner for an indoor rover. Break the command into "
        "a sequence of discrete, executable steps.\n"
        f'Command: "{command}"\n'
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
