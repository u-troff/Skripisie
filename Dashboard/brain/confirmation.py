import json
import re
from typing import Optional, Tuple

from log_setup import get_logger
from providers import ProviderError, get_provider, log_completion, user_message

log = get_logger("confirm")

CONFIRM = "CONFIRM"
REJECT = "REJECT"
UNCLEAR = "UNCLEAR"

# Checked before the confirm list: "no, don't do that" must not match on a
# stray affirmative later in the sentence.
REJECT_PHRASES = (
    "no", "nope", "cancel", "stop", "abort", "wait", "don't", "do not",
    "negative", "hold on", "not right", "wrong", "change",
    "nee", "moenie", "kanselleer", "wag", "verkeerd", "hou op", "nie reg",
)

CONFIRM_PHRASES = (
    "yes", "yeah", "yep", "yes please", "confirm", "confirmed", "go", "go ahead",
    "execute", "proceed", "affirmative", "correct", "okay", "ok", "do it",
    "ja", "jip", "bevestig", "korrek", "reg so", "gaan voort", "doen dit",
    "maak so", "begin",
)


def _normalise(text: str) -> str:
    lowered = (text or "").lower()
    # Keep apostrophes so "don't" survives; everything else becomes a gap.
    return " " + re.sub(r"[^a-z0-9']+", " ", lowered).strip() + " "


def _match(text: str, phrases) -> Optional[str]:
    for phrase in phrases:
        if re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text):
            return phrase
    return None


def keyword_pass(transcript: str) -> Tuple[str, Optional[str]]:
    """Deterministic first gate. No model latency and no hallucination risk on
    the last check before a physical rover moves."""
    text = _normalise(transcript)
    hit = _match(text, REJECT_PHRASES)
    if hit:
        return REJECT, hit
    hit = _match(text, CONFIRM_PHRASES)
    if hit:
        return CONFIRM, hit
    return UNCLEAR, None


def classify(transcript: str) -> str:
    """Keyword pass first, lightweight model classification only as fallback."""
    verdict, hit = keyword_pass(transcript)
    if verdict != UNCLEAR:
        log.info("[confirm] keyword %s on %r (transcript=%r)", verdict, hit, transcript)
        return verdict

    prompt = (
        "Classify this reply to a robot asking for confirmation before it moves.\n"
        "The reply may be in English or Afrikaans.\n"
        f'Reply: "{transcript}"\n'
        "CONFIRM means go ahead. REJECT means do not go, or change the plan. "
        "UNCLEAR means neither.\n"
        'Respond ONLY with JSON: {"verdict": "CONFIRM" | "REJECT" | "UNCLEAR"}'
    )

    try:
        provider = get_provider("planner")
        completion = provider.complete([user_message(prompt)], json_mode=True)
    except ProviderError as exc:
        # Fail safe: an unreachable classifier must never read as consent.
        log.error("[confirm] classifier unavailable, failing safe: %s", exc)
        return UNCLEAR

    log_completion(log, "planner", "confirm_classify", completion)
    try:
        verdict = str(json.loads(completion.text).get("verdict", "")).upper()
    except (json.JSONDecodeError, TypeError, AttributeError):
        log.warning("[confirm] unparseable classifier output: %r", completion.text[:200])
        return UNCLEAR

    if verdict not in (CONFIRM, REJECT, UNCLEAR):
        return UNCLEAR
    log.info("[confirm] model %s (transcript=%r)", verdict, transcript)
    return verdict
