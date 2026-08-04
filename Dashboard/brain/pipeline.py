import time
from typing import Optional

from log_setup import get_logger
from planner import generate_plan
from stt import transcribe_audio
from vlm import ImageSource, check_ambiguity, verify_plan

log = get_logger("pipeline")


def handle_voice_command(
    audio,
    image: Optional[ImageSource] = None,
    language: Optional[str] = None,
) -> dict:
    log.info("=" * 68)
    log.info("[run] start (language=%s, image=%s)", language, image is not None)

    started = time.perf_counter()
    text = transcribe_audio(audio, language=language)
    log.info("[stt] %.1fs transcript=%r", time.perf_counter() - started, text)

    ambiguity = check_ambiguity(text, image)
    if ambiguity.get("ambiguous"):
        log.info("[run] STOP — needs clarification: %s", ambiguity.get("clarifying_question"))
        return {
            "status": "needs_clarification",
            "transcript": text,
            "question": ambiguity.get("clarifying_question"),
            "reason": ambiguity.get("reason"),
        }

    plan = generate_plan(text)
    verification = verify_plan(plan, image)

    # Default True: if the verifier fails to answer, don't block the plan.
    verified = verification.get("verified", True)
    if not verified:
        log.info("[run] verifier rejected, replanning. concerns=%s", verification.get("concerns"))
        plan = generate_plan(f"{text}\n\nNote: {verification.get('concerns')}")

    log.info("[run] done in %.1fs (verified=%s)", time.perf_counter() - started, verified)
    return {
        "status": "ready",
        "transcript": text,
        "plan": plan,
        "verified": verification.get("verified"),
        "concerns": verification.get("concerns"),
    }
