"""Classify a proposed in-flight plan against the plan the human confirmed.

No model runs here, for the same reason the confirmation keyword pass exists:
the decision about whether a human must be consulted is not something to
delegate to a model that can hallucinate its way to "no change needed".
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Set

from log_setup import get_logger

log = get_logger("revision")

NO_CHANGE = "NO_CHANGE"
REROUTE = "REROUTE"    # same goal, same targets — apply and announce
MATERIAL = "MATERIAL"  # scope moved — halt and re-confirm
BLOCKED = "BLOCKED"    # goal unreachable — stop and report

# Navigation is the only class of action a reroute may introduce. Anything
# that touches the world (pick, open, push) is material by definition.
NAVIGATION_ACTIONS = {
    "move", "go", "goto", "navigate", "drive", "turn", "rotate", "face",
    "approach", "follow", "stop", "wait", "pause", "scan", "look", "search",
    "beweeg", "gaan", "draai", "soek", "kyk", "wag",
}

_ARTICLES = re.compile(r"^(the|a|an|die|n)\s+")


@dataclass
class Revision:
    kind: str
    reason: str
    added_targets: List[str] = field(default_factory=list)
    dropped_targets: List[str] = field(default_factory=list)
    new_actions: List[str] = field(default_factory=list)
    step_delta: int = 0

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "added_targets": self.added_targets,
            "dropped_targets": self.dropped_targets,
            "new_actions": self.new_actions,
            "step_delta": self.step_delta,
        }


def _normalise(value) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return _ARTICLES.sub("", text)


def _targets(plan: dict) -> Set[str]:
    out = set()
    for step in (plan or {}).get("steps") or []:
        target = _normalise(step.get("target"))
        if target:
            out.add(target)
    return out


def _actions(plan: dict) -> List[str]:
    return [_normalise(step.get("action")) for step in (plan or {}).get("steps") or []]


def _head(action: str) -> str:
    return action.split(" ")[0] if action else ""


def classify(confirmed: dict, proposed: dict) -> Revision:
    confirmed_steps = (confirmed or {}).get("steps") or []
    proposed_steps = (proposed or {}).get("steps") or []

    if not proposed_steps:
        return Revision(BLOCKED, "the revised plan has no executable steps")

    confirmed_targets, proposed_targets = _targets(confirmed), _targets(proposed)
    added = sorted(proposed_targets - confirmed_targets)
    dropped = sorted(confirmed_targets - proposed_targets)

    confirmed_actions = set(_head(a) for a in _actions(confirmed))
    new_actions = sorted(
        {
            _head(a)
            for a in _actions(proposed)
            if _head(a) and _head(a) not in confirmed_actions
        }
    )
    world_touching = [a for a in new_actions if a not in NAVIGATION_ACTIONS]

    delta = len(proposed_steps) - len(confirmed_steps)

    if added:
        kind, reason = MATERIAL, f"new target(s) not in the confirmed plan: {', '.join(added)}"
    elif dropped:
        kind, reason = MATERIAL, f"confirmed target(s) dropped: {', '.join(dropped)}"
    elif world_touching:
        kind, reason = MATERIAL, f"new non-navigation action(s): {', '.join(world_touching)}"
    elif _actions(confirmed) == _actions(proposed) and delta == 0:
        kind, reason = NO_CHANGE, "identical to the confirmed plan"
    else:
        kind, reason = REROUTE, "same goal and targets, different route"

    revision = Revision(kind, reason, added, dropped, new_actions, delta)
    log.info("[revision] %s — %s", kind, reason)
    return revision


def summarise(revision: Revision, proposed: dict) -> str:
    """Spoken delta for the re-confirmation prompt."""
    parts = ["The situation changed.", revision.reason + "."]
    parts.append("The revised plan is:")
    for index, step in enumerate((proposed or {}).get("steps") or [], start=1):
        target = step.get("target")
        parts.append(f"Step {index}: {step.get('action') or 'act'}{' ' + str(target) if target else ''}.")
    parts.append("Say yes to accept the change, or no to stop.")
    return " ".join(parts)
