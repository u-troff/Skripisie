import json
import os
import time
from typing import Any, Dict, Optional, Union

import ollama

from log_setup import get_logger

log = get_logger("vlm")

VLM_MODEL = os.getenv("VLM_MODEL", "qwen2.5vl:3b")

# Ollama defaults to a 4096-token window. An image expands into a large number
# of vision tokens, so image + serialised plan overflows it easily.
VLM_NUM_CTX = int(os.getenv("VLM_NUM_CTX", "8192"))

# ollama.Image accepts a path, raw bytes, or a base64 string. Bytes are the
# useful case here: uploads arrive in memory and never need to touch disk.
ImageSource = Union[str, bytes]


def check_ambiguity(command: str, image: Optional[ImageSource] = None) -> dict:
    prompt = (
        "You are checking whether a robot command is ambiguous.\n"
        f'Command: "{command}"\n'
        "Respond ONLY with JSON: "
        '{"ambiguous": true/false, "reason": "...", "clarifying_question": "..." or null}'
    )
    return _ask("check_ambiguity", prompt, image)


def verify_plan(plan: dict, image: Optional[ImageSource] = None) -> dict:
    prompt = (
        "You are verifying a robot's mission plan before it departs.\n"
        f"Plan: {json.dumps(plan)}\n"
        "Respond ONLY with JSON: "
        '{"verified": true/false, "concerns": "..." or null}'
    )
    return _ask("verify_plan", prompt, image)


def _ask(stage: str, prompt: str, image: Optional[ImageSource]) -> dict:
    message: Dict[str, Any] = {"role": "user", "content": prompt}
    if image:
        message["images"] = [image]

    image_note = f"{len(image)} bytes" if isinstance(image, bytes) else ("path" if image else "none")
    log.info("[%s] -> %s (num_ctx=%d, image=%s)", stage, VLM_MODEL, VLM_NUM_CTX, image_note)
    log.debug("[%s] prompt:\n%s", stage, prompt)

    started = time.perf_counter()
    try:
        response = ollama.chat(
            model=VLM_MODEL,
            messages=[message],
            format="json",
            options={"num_ctx": VLM_NUM_CTX},
        )
    except ollama.ResponseError as exc:
        # Don't take the whole request down because one model call failed.
        # Callers use .get() with defaults, so {} degrades gracefully.
        log.error("[%s] ollama error after %.1fs: %s", stage, time.perf_counter() - started, exc)
        return {}

    raw = response["message"]["content"]
    log.info("[%s] <- %.1fs", stage, time.perf_counter() - started)
    log.debug("[%s] raw response:\n%s", stage, raw)

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, KeyError):
        log.warning("[%s] unparseable JSON, returning {}: %r", stage, raw[:300])
        return {}

    log.info("[%s] parsed: %s", stage, json.dumps(parsed)[:300])
    return parsed
