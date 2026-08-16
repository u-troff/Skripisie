import copy
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import config
from dialogue_session import DialogueSession

DIGEST_KEEP = config.get_int("PERCEPTION_DIGEST_KEEP", 6)


class MissionPhase(str, Enum):
    EXECUTING = "executing"
    AWAITING_REVISION_CONFIRMATION = "awaiting_revision_confirmation"
    HALTED = "halted"
    COMPLETED = "completed"
    ABORTED = "aborted"


TERMINAL = (MissionPhase.HALTED, MissionPhase.COMPLETED, MissionPhase.ABORTED)


@dataclass
class RevisionRecord:
    at: float
    kind: str
    reason: str
    applied: bool
    proposed: Optional[dict] = None


@dataclass
class MissionSession:
    session_id: str
    command: str
    # Frozen at the moment of voice confirmation. Never mutated — every
    # proposal is diffed against this, so consent stays auditable.
    confirmed_plan: dict = field(default_factory=dict)
    active_plan: dict = field(default_factory=dict)
    cursor: int = 0
    phase: MissionPhase = MissionPhase.EXECUTING

    digest: List[str] = field(default_factory=list)
    frames_seen: int = 0
    frames_analysed: int = 0
    last_stats: Any = None
    last_analysis_at: float = 0.0
    analysing: bool = False

    # Set by perception, applied by the step loop between steps. This is what
    # removes the race: perception never touches active_plan directly.
    pending_swap: Optional[dict] = None
    pending_material: Optional[dict] = None

    revision_log: List[RevisionRecord] = field(default_factory=list)
    results: List[dict] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def steps(self) -> List[dict]:
        return self.active_plan.get("steps") or []

    def remaining(self) -> List[dict]:
        return self.steps()[self.cursor :]

    def touch(self) -> None:
        self.updated_at = time.time()

    def note(self, line: str) -> None:
        if line:
            self.digest.append(line)
            del self.digest[:-DIGEST_KEEP]

    def snapshot(self) -> dict:
        return {
            "session_id": self.session_id,
            "phase": self.phase.value,
            "command": self.command,
            "confirmed_plan": self.confirmed_plan,
            "active_plan": self.active_plan,
            "cursor": self.cursor,
            "step_count": len(self.steps()),
            "digest": list(self.digest),
            "frames_seen": self.frames_seen,
            "frames_analysed": self.frames_analysed,
            "pending_material": self.pending_material,
            "results": self.results,
            "revisions": [
                {"at": r.at, "kind": r.kind, "reason": r.reason, "applied": r.applied}
                for r in self.revision_log
            ],
        }


class MissionStore:
    def __init__(self):
        self._missions: Dict[str, MissionSession] = {}
        self._lock = threading.Lock()

    def create(self, dialogue: DialogueSession) -> MissionSession:
        plan = copy.deepcopy(dialogue.plan or {})
        mission = MissionSession(
            session_id=dialogue.session_id,
            command=dialogue.effective_command(),
            confirmed_plan=plan,
            active_plan=copy.deepcopy(plan),
        )
        with self._lock:
            self._missions[mission.session_id] = mission
        return mission

    def get(self, session_id: str) -> Optional[MissionSession]:
        with self._lock:
            return self._missions.get(session_id)

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._missions.pop(session_id, None)


store = MissionStore()
