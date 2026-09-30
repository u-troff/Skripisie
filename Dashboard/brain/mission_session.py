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

    # -- grounded line mission (spec-grounded-line-mission.md §E/§F) ---------
    # What the human actually said, kept next to the resolved command so a run
    # log shows how much of the plan came from clarification rather than from
    # the original utterance. That difference is the RQ1 evidence.
    original_command: str = ""
    scene_id: Optional[str] = None
    scene_text: str = ""

    # Every VLM check taken while driving, including the ones that were skipped
    # because a previous check was still running. Skips are data, not noise:
    # "how many checks fit in one run on this host" is a measured RQ1 number.
    checks: List[dict] = field(default_factory=list)
    look_left: Optional[dict] = None
    arrival: Optional[dict] = None
    target_confirmed: Optional[dict] = None
    # Consecutive completed progress checks reporting path_clear=false. Two in
    # a row warns; it never halts — the sonar is the only authority on stopping.
    path_unclear_streak: int = 0
    rover_telemetry: dict = field(default_factory=dict)
    # Live asyncio Tasks for in-flight VLM checks. Not serialisable and
    # deliberately absent from snapshot(); run_mission drains it before
    # building the report so a slow check still lands in the log.
    check_tasks: List[Any] = field(default_factory=list)

    # -- planner-profiles / virtual-sweep (spec-planner-profiles-and-virtual-
    # sweep.md §3D) ----------------------------------------------------------
    # rover.summary() if the rover has one (VirtualRover); {} on a rover that
    # doesn't (e.g. PiRoverController).
    rover_summary: dict = field(default_factory=dict)
    # One entry per confirmed-plan step with a target, resolved through
    # rover.room.resolve — only meaningful for ROVER=virtual, where
    # room.resolve exists; {} otherwise.
    grounding: List[dict] = field(default_factory=list)
    # What the pre-departure dialogue looked like: turn_count, capped,
    # verified, concerns, replan_count. Copied once at mission creation —
    # see MissionStore.create.
    dialogue_meta: dict = field(default_factory=dict)
    # providers.usage.snapshot() at mission end: every model call's role,
    # provider, model, latency, tokens and cost.
    usage: List[dict] = field(default_factory=list)

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
            "checks": self.checks,
            "look_left": self.look_left,
            "arrival": self.arrival,
            "target_confirmed": self.target_confirmed,
            "rover_telemetry": self.rover_telemetry,
            "pending_material": self.pending_material,
            "results": self.results,
            "revisions": [
                {"at": r.at, "kind": r.kind, "reason": r.reason, "applied": r.applied}
                for r in self.revision_log
            ],
            "rover_summary": self.rover_summary,
            "grounding": self.grounding,
            "dialogue_meta": self.dialogue_meta,
            "usage": self.usage,
        }


def _replan_count(dialogue: DialogueSession) -> int:
    """How many times the plan was rebuilt after a "no, change this" during
    the dialogue. DialogueSession doesn't track this as its own counter, so
    it's read off pipeline.handle_confirmation_text's REJECT marker: every
    REJECT appends exactly one Turn with this question before looping back
    into _advance_to_confirmation, the only path (besides the initial
    ambiguity loop) that regenerates session.plan after the first time."""
    return sum(1 for turn in dialogue.turns if turn.question == "What should I change?")


class MissionStore:
    def __init__(self):
        self._missions: Dict[str, MissionSession] = {}
        self._lock = threading.Lock()

    def create(self, dialogue: DialogueSession) -> MissionSession:
        plan = copy.deepcopy(dialogue.plan or {})
        mission = MissionSession(
            session_id=dialogue.session_id,
            command=dialogue.effective_command(),
            original_command=dialogue.command,
            scene_id=dialogue.scene_id,
            scene_text=dialogue.scene_text,
            confirmed_plan=plan,
            active_plan=copy.deepcopy(plan),
            dialogue_meta={
                "turn_count": len(dialogue.turns),
                "capped": dialogue.capped,
                "verified": dialogue.verified,
                "concerns": dialogue.concerns,
                "replan_count": _replan_count(dialogue),
            },
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
