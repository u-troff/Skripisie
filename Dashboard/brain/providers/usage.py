"""Usage ledger: every model call this process makes, for report.py's
usage_summary and the local-vs-cloud sweep (RQ2). A module-level list behind
a lock is enough — the UI and the sweep both assume one mission runs at a
time in this process, so there is no per-session partitioning to do.

Hooked from base.log_completion, the one place every role (planner, vlm,
confirm) already reports latency, tokens and cost — see
Progress/spec-planner-profiles-and-virtual-sweep.md §3F.
"""

import threading
import time
from typing import Any, Dict, List

_lock = threading.Lock()
_records: List[Dict[str, Any]] = []


def reset() -> None:
    """Called when /ws/dialogue starts a new session — see main.py. That
    assumes one mission at a time in this process, which holds for both the
    UI and tools/virtual_sweep.py."""
    with _lock:
        _records.clear()


def record(entry: Dict[str, Any]) -> None:
    with _lock:
        _records.append(dict(entry, t=entry.get("t", time.time())))


def snapshot() -> List[Dict[str, Any]]:
    """A copy — callers must not be able to mutate the ledger they read."""
    with _lock:
        return [dict(r) for r in _records]
