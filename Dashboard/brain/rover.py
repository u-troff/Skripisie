import threading
import json
import random
import re
from datetime import datetime
from pathlib import Path

import time
from abc import ABC, abstractmethod
from typing import Optional,List


import config
from log_setup import get_logger
from room_map import Pose,RoomMap,load_room,wrap_deg

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
def _turn_degrees(target, magnitude: float) -> float:
    """Direction from the step's target text; + is counter-clockwise (left).

    There is no angle field in a plan step, so an explicit "90 degrees" in the
    text is honoured and everything else falls back to the config constant.
    """
    text = str(target or "").lower()
    if "around" in text or "180" in text or "behind" in text:
        return 180.0
    found = re.search(r"(\d+(?:\.\d+)?)\s*(?:deg|degree)", text)
    amount = float(found.group(1)) if found else float(magnitude)
    return amount if "left" in text else -amount


def _is_backward(target) -> bool:
    # Crude on purpose: "move to the back of the room" reads as reverse here.
    # A test harness mis-signing one step is cheaper than parsing English.
    text = str(target or "").lower()
    return any(word in text for word in ("backward", "backwards", "reverse", "retreat", "back up"))


class VirtualRover(RoverController):
    """Integrates each step into a 2D pose on a hand-authored room map.

    Same execute_step contract as SimulatedRover, so mission.py needs no
    changes: a plan that would drive through a wall comes back "blocked" and
    halts the mission exactly like a real collision would (mission.py:158).

    Drift is deliberate, not a bug. The real chassis is mecanum with no encoders
    and no IMU, so open-loop motion slips; the gap between the straight intended
    path and the drifted trace is the evidence for instrumenting navigation
    rather than trusting dead reckoning.

    A target that is not in the node table is a hallucination event to log for
    RQ2, not a crash. Whether it also stops the mission is VIRTUAL_STRICT_TARGETS:
    strict (default) matches real executability and gives you the index of the
    first ungrounded step; non-strict keeps going so one long run counts how many
    of N chained instructions grounded.
    """

    name = "virtual"

    def __init__(self, room: RoomMap, step_distance_m: float = 0.5,
                 step_turn_deg: float = 90.0, arrive_m: float = 0.4,
                 approach_max_m: float = 0.0, step_seconds: float = 1.0,
                 drift_frac: float = 0.06, drift_deg: float = 2.0,
                 seed: Optional[int] = None, strict_targets: bool = True,
                 trace_dir: Optional[Path] = None):
        self.room = room
        self.pose = room.start_pose()
        self.step_distance_m = step_distance_m
        self.step_turn_deg = step_turn_deg
        self.arrive_m = arrive_m
        self.approach_max_m = approach_max_m
        self.step_seconds = step_seconds
        self.drift_frac = drift_frac
        self.drift_deg = drift_deg
        self.strict_targets = strict_targets
        # Always a concrete number, even when unset, so any run can be replayed
        # from what the trace records.
        self.seed = random.randrange(2 ** 31) if seed is None else int(seed)
        self._rng = random.Random(self.seed)

        self.trace: List[dict] = []
        self.hallucinations: List[dict] = []
        self._halted = threading.Event()
        self._run_index = 0
        self._run_steps = 0

        folder = Path(trace_dir) if trace_dir else Path(__file__).parent / "logs"
        folder.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.trace_path: Optional[Path] = folder / ("virtual_run_%s.jsonl" % stamp)

    # -- the contract ------------------------------------------------------
    def execute_step(self, step: dict) -> dict:
        self._halted.clear()
        self._begin_run_if_needed(step)

        action = str(step.get("action") or "").strip().lower()
        target = step.get("target")
        start = self.pose
        extra = {}

        if action in ("approach", "observe"):
            landmark, how = self.room.resolve(target)
            if landmark is None:
                return self._unresolved(step, target)
            extra = {"node": landmark.name, "matched_via": how}
            if action == "approach":
                intended = start.advance_toward(landmark.point(),
                                                self.approach_max_m, self.arrive_m)
            else:
                # observe aims the gimbal; the base does not move, so the pose
                # is unchanged and only the look direction is recorded.
                intended = start
                extra["look_bearing"] = round(start.bearing_to(landmark.point()), 2)
        elif action == "move":
            distance = -self.step_distance_m if _is_backward(target) else self.step_distance_m
            intended = start.advance(distance)
        elif action == "turn":
            intended = start.rotate(_turn_degrees(target, self.step_turn_deg))
        elif action in ("scan", "stop", "report"):
            intended = start
        else:
            return self._finish(step, start, start, "blocked",
                                "unknown action %r" % action, reason="unknown_action")

        if self._dwell():
            return self._finish(step, intended, start, "halted", "halted mid-step", **extra)

        achieved = self._drift(start, intended)
        hit = self.room.obstruction(start, achieved)
        if hit is not None:
            # Pose stays where it was; the attempted pose is in "intended".
            return self._finish(step, intended, start, "blocked",
                                "path crosses %s" % hit, reason="collision", **extra)

        self.pose = achieved
        return self._finish(step, intended, achieved, "ok", None, **extra)

    def halt(self) -> None:
        log.warning("[virtual] HALT")
        self._halted.set()

    # -- internals ---------------------------------------------------------
    def _dwell(self) -> bool:
        """Real-time pacing. Without it a mission finishes before a single frame
        arrives and the perception/revision path never runs."""
        deadline = time.perf_counter() + self.step_seconds
        while time.perf_counter() < deadline:
            if self._halted.is_set():
                return True
            time.sleep(0.05)
        return self._halted.is_set()

    def _drift(self, start: Pose, intended: Pose) -> Pose:
        travelled = start.distance_to(intended.point())
        turned = abs(wrap_deg(intended.theta - start.theta))
        if travelled <= 0.0 and turned <= 0.0:
            return intended  # a scan or a report does not slip
        sigma = self.drift_frac * travelled
        return intended.jittered(
            self._rng.gauss(0.0, sigma) if sigma > 0.0 else 0.0,
            self._rng.gauss(0.0, sigma) if sigma > 0.0 else 0.0,
            self._rng.gauss(0.0, self.drift_deg),
        )

    def _unresolved(self, step: dict, target) -> dict:
        self.hallucinations.append({"run": self._run_index, "step": step, "target": target})
        log.warning("[virtual] step %s: %r is not in %s's node table — hallucination %d%s",
                    step.get("id"), target, self.room.name, len(self.hallucinations),
                    "" if self.strict_targets else " (continuing: strict targets off)")
        return self._finish(step, self.pose, self.pose,
                            "blocked" if self.strict_targets else "ok",
                            "there is nothing called %r in this room" % (target,),
                            reason="unresolved_target")

    def _begin_run_if_needed(self, step: dict) -> None:
        # No mission-start hook reaches the rover, so a plan landing back on
        # step 1 is the signal. A revision that resets the cursor restarts the
        # run, which is what the figure should show anyway.
        restarted = self._run_steps > 0 and str(step.get("id")) == "1"
        if self._run_index == 0 or restarted:
            self._begin_run("restart" if restarted else "start")

    def _begin_run(self, why: str) -> None:
        self._run_index += 1
        self._run_steps = 0
        self.pose = self.room.start_pose()
        self.trace = []
        self.hallucinations = []
        log.info("[virtual] run %d (%s) on %r — seed %d, trace %s",
                 self._run_index, why, self.room.name, self.seed,
                 self.trace_path.name if self.trace_path else "off")
        self._write({
            "event": "run_start", "run": self._run_index, "why": why,
            "t": time.time(), "seed": self.seed, "pose": self.pose.to_dict(),
            "settings": {
                "step_distance_m": self.step_distance_m,
                "step_turn_deg": self.step_turn_deg,
                "arrive_m": self.arrive_m,
                "approach_max_m": self.approach_max_m,
                "drift_frac": self.drift_frac,
                "drift_deg": self.drift_deg,
                "strict_targets": self.strict_targets,
            },
            # Embedded so the trace plots without needing the room file too.
            "room": self.room.to_dict(),
        })

    def _finish(self, step: dict, intended: Pose, achieved: Pose,
                status: str, detail: Optional[str], **extra) -> dict:
        self._run_steps += 1
        record = {
            "event": "step", "run": self._run_index, "seq": self._run_steps,
            "t": time.time(), "step": step, "status": status, "detail": detail,
            "pose": achieved.to_dict(), "intended": intended.to_dict(),
        }
        record.update(extra)
        self._write(record)
        log.info("[virtual] %s. %s -> %s | %s @ (%.2f, %.2f, %.0f deg)",
                 step.get("id"), step.get("action"), step.get("target"),
                 status, achieved.x, achieved.y, achieved.theta)
        return {"status": status, "detail": detail}

    def _write(self, record: dict) -> None:
        self.trace.append(record)
        if self.trace_path is None:
            return
        try:
            with self.trace_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError as exc:
            log.warning("[virtual] cannot append to %s: %s — trace is memory-only now",
                        self.trace_path, exc)
            self.trace_path = None

    def summary(self) -> dict:
        """RQ2 roll-up for the current run."""
        steps = [r for r in self.trace if r.get("event") == "step"]
        travelled = 0.0
        previous = self.room.start_pose()
        for record in steps:
            here = Pose(**record["pose"])
            travelled += previous.distance_to(here.point())
            previous = here
        return {
            "run": self._run_index,
            "seed": self.seed,
            "steps": len(steps),
            "blocked": sum(1 for r in steps if r["status"] == "blocked"),
            "hallucinations": len(self.hallucinations),
            "unresolved_targets": [h["target"] for h in self.hallucinations],
            "path_length_m": round(travelled, 3),
            "final_pose": self.pose.to_dict(),
            "trace": str(self.trace_path) if self.trace_path else None,
        }


_cache: Optional[RoverController] = None

def _get_bool(name: str, default: bool) -> bool:
    raw = config.get(name, "1" if default else "0").strip().lower()
    return raw not in ("0", "false", "no", "off")


def get_rover() -> RoverController:
    global _cache
    if _cache is not None:
        return _cache
    name = config.get("ROVER", "sim").lower()
    if name == "sim":
        _cache = SimulatedRover(step_seconds=config.get_float("SIM_STEP_SECONDS", 3.0))
    elif name == "virtual":
        seed_text = config.get("VIRTUAL_SEED", "").strip()
        _cache = VirtualRover(
            room=load_room(config.get("ROOM_MAP", "rooms/room_tour1.json")),
            step_distance_m=config.get_float("VIRTUAL_STEP_M", 0.5),
            step_turn_deg=config.get_float("VIRTUAL_TURN_DEG", 90.0),
            arrive_m=config.get_float("VIRTUAL_ARRIVE_M", 0.4),
            approach_max_m=config.get_float("VIRTUAL_APPROACH_MAX_M", 0.0),
            step_seconds=config.get_float("VIRTUAL_STEP_SECONDS", 1.0),
            drift_frac=config.get_float("VIRTUAL_DRIFT_FRAC", 0.06),
            drift_deg=config.get_float("VIRTUAL_DRIFT_DEG", 2.0),
            seed=int(seed_text) if seed_text.lstrip("-").isdigit() else None,
            strict_targets=_get_bool("VIRTUAL_STRICT_TARGETS", True),
        )
    else:
        raise RoverError(f"unknown ROVER={name!r} — 'sim' and 'virtual' are implemented")
    log.info("[rover] %s", _cache.name)
    return _cache

