import json
import os
import time

import ollama

from log_setup import get_logger

log = get_logger("planner")

PLANNER_MODEL = os.getenv("PLANNER_MODEL", "gemma4:e2b")
PLANNER_NUM_CTX = int(os.getenv("PLANNER_NUM_CTX", "8192"))


def generate_plan(command: str) -> dict:
    prompt = (
        "You are a mission planner for an indoor rover. Break the command into "
        "a sequence of discrete, executable steps.\n"
        f'Command: "{command}"\n'
        "Respond ONLY with JSON: "
        '{"steps": [{"id": 1, "action": "...", "target": "..."}], "notes": "..."}'
    )

    log.info("[plan] -> %s (num_ctx=%d) command=%r", PLANNER_MODEL, PLANNER_NUM_CTX, command)
    log.debug("[plan] prompt:\n%s", prompt)

    started = time.perf_counter()
    try:
        response = ollama.chat(
            model=PLANNER_MODEL,
            messages=[{"role": "user", "content": prompt}],
            format="json",
            options={"num_ctx": PLANNER_NUM_CTX},
        )
    except ollama.ResponseError as exc:
        log.error("[plan] ollama error after %.1fs: %s", time.perf_counter() - started, exc)
        return {"steps": [], "notes": f"planner failed: {exc}"}

    raw = response["message"]["content"]
    log.info("[plan] <- %.1fs", time.perf_counter() - started)
    log.debug("[plan] raw response:\n%s", raw)

    try:
        plan = json.loads(raw)
    except (json.JSONDecodeError, KeyError):
        log.warning("[plan] unparseable JSON: %r", raw[:300])
        return {"steps": [], "notes": "planner returned unparseable output"}

    steps = plan.get("steps", [])
    log.info("[plan] %d step(s)", len(steps))
    for step in steps:
        log.info("       %s. %s -> %s", step.get("id"), step.get("action"), step.get("target"))
    return plan
