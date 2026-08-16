import threading
import time
from abc import ABC, abstractmethod
from typing import Optional

import config
from log_setup import get_logger

log = get_logger("rover")


class RoverError(RuntimeError):
    pass


class RoverController(ABC):
    name = "base"

    @abstractmethod
    def execute_step(self, step: dict) -> dict:
        """Blocking. Returns {"status": "ok" | "blocked" | "halted", "detail": ...}.
        Called from a worker thread, never the event loop."""

    @abstractmethod
    def halt(self) -> None:
        """Must not require inference, a model, or a network call."""


class SimulatedRover(RoverController):
    """Sleeps for the step duration and reports success.

    Enough to exercise the whole execution and revision path without hardware;
    swap for a serial or ROS implementation without touching mission.py.
    """

    name = "sim"

    def __init__(self, step_seconds: float = 3.0):
        self.step_seconds = step_seconds
        self._halted = threading.Event()

    def execute_step(self, step: dict) -> dict:
        self._halted.clear()
        log.info("[sim] step %s: %s -> %s", step.get("id"), step.get("action"), step.get("target"))
        # Poll rather than sleep in one go, so halt() lands within 100ms.
        deadline = time.perf_counter() + self.step_seconds
        while time.perf_counter() < deadline:
            if self._halted.is_set():
                return {"status": "halted", "detail": "halted mid-step"}
            time.sleep(0.1)
        return {"status": "ok", "detail": None}

    def halt(self) -> None:
        log.warning("[sim] HALT")
        self._halted.set()


_cache: Optional[RoverController] = None


def get_rover() -> RoverController:
    global _cache
    if _cache is not None:
        return _cache
    name = config.get("ROVER", "sim").lower()
    if name == "sim":
        _cache = SimulatedRover(step_seconds=config.get_float("SIM_STEP_SECONDS", 3.0))
    else:
        raise RoverError(f"unknown ROVER={name!r} — only 'sim' is implemented")
    log.info("[rover] %s", _cache.name)
    return _cache
