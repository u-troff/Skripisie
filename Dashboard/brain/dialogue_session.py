import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

import config
from providers import ImageSource

MAX_CLARIFYING_TURNS = config.get_int("MAX_CLARIFYING_TURNS", 5)
MAX_CONFIRM_RETRIES = config.get_int("MAX_CONFIRM_RETRIES", 2)
SESSION_TTL_S = config.get_int("SESSION_TTL_S", 1800)


class Phase(str, Enum):
    """str mixin so a Phase serialises straight into a WebSocket JSON frame."""

    CLARIFYING = "clarifying"
    PLANNING = "planning"
    VERIFYING = "verifying"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    EXECUTING = "executing"
    REPORTING = "reporting"
    CANCELLED = "cancelled"


@dataclass
class Turn:
    question: str
    answer: Optional[str] = None


@dataclass
class DialogueSession:
    session_id: str
    language: str = "af"
    phase: Phase = Phase.CLARIFYING
    command: str = ""
    resolved_command: str = ""
    turns: List[Turn] = field(default_factory=list)
    plan: Optional[dict] = None
    verified: Optional[bool] = None
    concerns: Optional[str] = None
    image: Optional[ImageSource] = None
    # True when the loop exited on the turn cap rather than on confidence.
    # Surfaced in the readback and logged for RQ2.
    capped: bool = False
    confirm_retries: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def pending_turn(self) -> Optional[Turn]:
        if self.turns and self.turns[-1].answer is None:
            return self.turns[-1]
        return None

    def history(self) -> List[Dict[str, str]]:
        return [{"question": t.question, "answer": t.answer or ""} for t in self.turns]

    def effective_command(self) -> str:
        """What the planner actually plans against."""
        if self.resolved_command:
            return self.resolved_command
        parts = [self.command]
        for turn in self.turns:
            if turn.answer:
                parts.append(f"Clarification — {turn.question} {turn.answer}")
        return "\n".join(parts)

    def touch(self) -> None:
        self.updated_at = time.time()

    def snapshot(self) -> dict:
        return {
            "session_id": self.session_id,
            "phase": self.phase.value,
            "command": self.command,
            "resolved_command": self.resolved_command,
            "turns": [{"question": t.question, "answer": t.answer} for t in self.turns],
            "turn_count": len(self.turns),
            "capped": self.capped,
            "plan": self.plan,
            "verified": self.verified,
            "concerns": self.concerns,
        }


class SessionStore:
    """In-memory, single-rover. A lock is still needed because model calls run
    in a threadpool, so two frames can land concurrently."""

    def __init__(self, ttl_s: int = SESSION_TTL_S):
        self._sessions: Dict[str, DialogueSession] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_s

    def create(self, session_id: Optional[str] = None, language: str = "af") -> DialogueSession:
        session = DialogueSession(session_id=session_id or uuid.uuid4().hex[:12], language=language)
        with self._lock:
            self._sweep()
            self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> Optional[DialogueSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def _sweep(self) -> None:
        cutoff = time.time() - self._ttl
        for key in [k for k, s in self._sessions.items() if s.updated_at < cutoff]:
            del self._sessions[key]


store = SessionStore()


def summarise_plan(session: DialogueSession) -> str:
    """The spoken readback. Deliberately plain — it goes through TTS."""
    steps = (session.plan or {}).get("steps") or []
    if not steps:
        return "I could not build a plan for that. Please say the command again."

    parts = []
    if session.capped:
        parts.append(
            "I am not fully sure I understood, but here is my best interpretation."
        )
    if session.verified is False and session.concerns:
        parts.append("One concern: " + str(session.concerns) + ".")

    parts.append("Here is the plan.")
    for index, step in enumerate(steps, start=1):
        action = step.get("action") or "do something"
        target = step.get("target")
        parts.append(f"Step {index}: {action}{' ' + str(target) if target else ''}.")
    parts.append("Say yes to go, or tell me what to change.")
    return " ".join(parts)
