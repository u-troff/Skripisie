import time
from typing import List, Optional

import confirmation
from dialogue_session import (
    MAX_CLARIFYING_TURNS,
    MAX_CONFIRM_RETRIES,
    DialogueSession,
    Phase,
    Turn,
    summarise_plan,
)
from log_setup import get_logger
from planner import generate_plan
from stt import transcribe_audio
from vlm import ImageSource, check_ambiguity, verify_plan

log = get_logger("pipeline")


# --------------------------------------------------------------------------
# Existing single-shot HTTP flow — unchanged.
# --------------------------------------------------------------------------
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


# --------------------------------------------------------------------------
# Multi-turn pre-departure dialogue.
#
# Every function here returns a list of outbound WebSocket frames. Keeping the
# transport out of this module means the same logic serves an HTTP+polling
# front end if the WebSocket turns out to be awkward on the Pi.
# --------------------------------------------------------------------------
def _speak(session: DialogueSession, text: str) -> dict:
    return {"type": "speak", "text": text, "phase": session.phase.value,
            "session_id": session.session_id}


def handle_dialogue_audio(
    session: DialogueSession,
    audio,
    image: Optional[ImageSource] = None,
) -> List[dict]:
    """One inbound utterance during CLARIFYING. Either asks the next question
    or exits the loop into PLANNING → VERIFYING → AWAITING_CONFIRMATION."""
    if image is not None:
        session.image = image

    started = time.perf_counter()
    text = transcribe_audio(audio, language=session.language)
    log.info("[dlg %s] stt %.1fs transcript=%r", session.session_id,
             time.perf_counter() - started, text)

    pending = session.pending_turn()
    if pending is not None:
        pending.answer = text
    elif not session.command:
        session.command = text
        log.info("=" * 68)
        log.info("[dlg %s] command=%r", session.session_id, text)
    else:
        # Unprompted speech mid-loop: treat it as extra context, not noise.
        session.turns.append(Turn(question="(unprompted)", answer=text))

    session.touch()
    return _advance_to_confirmation(session)


def _advance_to_confirmation(session: DialogueSession) -> List[dict]:
    session.phase = Phase.CLARIFYING
    ambiguity = check_ambiguity(session.command, session.image, history=session.history())

    if ambiguity.get("ambiguous"):
        question = ambiguity.get("clarifying_question")
        if question and len(session.turns) < MAX_CLARIFYING_TURNS:
            session.turns.append(Turn(question=str(question)))
            session.touch()
            log.info("[dlg %s] clarify turn %d/%d: %s", session.session_id,
                     len(session.turns), MAX_CLARIFYING_TURNS, question)
            return [_speak(session, str(question))]

        # Cap hit, or the model said "ambiguous" without offering a question.
        # Don't silently guess — carry the uncertainty into the readback.
        session.capped = True
        log.warning(
            "[dlg %s] clarification cap hit after %d turn(s) — command=%r reason=%r",
            session.session_id, len(session.turns), session.command, ambiguity.get("reason"),
        )
    else:
        resolved = ambiguity.get("resolved_command")
        if resolved:
            session.resolved_command = str(resolved)

    session.phase = Phase.PLANNING
    session.plan = generate_plan(session.effective_command())

    session.phase = Phase.VERIFYING
    verification = verify_plan(session.plan, session.image)
    # Default True: a verifier that fails to answer must not block the plan.
    if not verification.get("verified", True):
        log.info("[dlg %s] verifier rejected, replanning. concerns=%s",
                 session.session_id, verification.get("concerns"))
        session.plan = generate_plan(
            "%s\n\nNote: %s" % (session.effective_command(), verification.get("concerns"))
        )
    session.verified = verification.get("verified")
    session.concerns = verification.get("concerns")

    session.phase = Phase.AWAITING_CONFIRMATION
    session.confirm_retries = 0
    session.touch()
    log.info("[dlg %s] plan ready, awaiting voice confirmation", session.session_id)

    return [
        {"type": "plan_ready", "session_id": session.session_id,
         "plan": session.plan, "verified": session.verified,
         "concerns": session.concerns, "capped": session.capped,
         "turn_count": len(session.turns)},
        _speak(session, summarise_plan(session)),
    ]


def handle_confirmation_audio(session: DialogueSession, audio) -> List[dict]:
    """The voice-only gate. Nothing else may move the rover."""
    text = transcribe_audio(audio, language=session.language)
    log.info("[dlg %s] confirmation transcript=%r", session.session_id, text)
    verdict = confirmation.classify(text)

    if verdict == confirmation.CONFIRM:
        session.phase = Phase.EXECUTING
        session.touch()
        log.info("[dlg %s] CONFIRMED — handing off to execution", session.session_id)
        return [
            _speak(session, "Confirmed. Going now."),
            {"type": "execute", "session_id": session.session_id, "plan": session.plan},
        ]

    if verdict == confirmation.REJECT:
        # A rejection is usually also a change request, so it becomes history
        # rather than just a cancel.
        session.turns.append(Turn(question="What should I change?", answer=text))
        session.capped = False
        session.resolved_command = ""
        session.touch()
        log.info("[dlg %s] REJECTED — back to clarifying", session.session_id)
        return [{"type": "revise", "session_id": session.session_id,
                 "objection": text}] + _advance_to_confirmation(session)

    session.confirm_retries += 1
    session.touch()
    if session.confirm_retries <= MAX_CONFIRM_RETRIES:
        log.info("[dlg %s] UNCLEAR (%d/%d) — re-prompting",
                 session.session_id, session.confirm_retries, MAX_CONFIRM_RETRIES)
        return [_speak(session, "I did not catch that. Say yes to go, or no to cancel.")]

    # Fail safe: never execute on an unresolved answer.
    session.phase = Phase.CANCELLED
    log.warning("[dlg %s] CANCELLED — no clear confirmation", session.session_id)
    return [
        _speak(session, "I did not get a clear answer, so I am cancelling."),
        {"type": "cancelled", "session_id": session.session_id, "reason": "unclear_confirmation"},
    ]
