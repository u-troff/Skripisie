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
