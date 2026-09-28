import threading
import json
import random
import re
from datetime import datetime
from pathlib import Path

import time
from abc import ABC, abstractmethod
from typing import Optional, List

import nav
import config
from log_setup import get_logger
from room_map import Point, Pose, RoomMap, load_room, wrap_deg, footprint_clearance_m

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
def _is_backward(target) -> bool:
    # Crude on purpose: "move to the back of the room" reads as reverse here.
    # A test harness mis-signing one step is cheaper than parsing English.
    text = str(target or "").lower()
    return any(word in text for word in ("backward", "backwards", "reverse", "retreat", "back up"))


# -- move/turn magnitude parsing ------------------------------------------
# distance_m/degrees from the step schema win; failing that, the target text
# is read for an explicit number; failing that, the config constant applies.
# See Progress/spec-planner-profiles-and-virtual-sweep.md §1 ("move / turn").
_METRES_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:metres|meters|metre|meter|m\b)", re.I)
_CM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*cm\b", re.I)
_HALF_METRE_RE = re.compile(r"half\s+an?\s+(?:metre|meter)\b", re.I)
_DEG_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:deg|degree)", re.I)


def _distance_from_text(text) -> Optional[float]:
    t = str(text or "")
    if _HALF_METRE_RE.search(t):
        return 0.5
    found = _CM_RE.search(t)
    if found:
        return float(found.group(1)) / 100.0
    found = _METRES_RE.search(t)
    if found:
        return float(found.group(1))
    return None


def _turn_magnitude_from(step: dict, target, default_deg: float) -> float:
    value = step.get("degrees")
    if isinstance(value, (int, float)):
        return abs(float(value))
    text = str(target or "").lower()
    if "around" in text or "180" in text or "behind" in text:
        return 180.0
    found = _DEG_RE.search(text)
    if found:
        return float(found.group(1))
    return default_deg


class VirtualRover(RoverController):
    """Drives on a hand-typed RoomMap using two kinds of motion.

    `approach` is a navigation skill: it plans a collision-free A* route on
    an inflated occupancy grid (nav.py) and follows it exactly, so a failure
    there can only be a wrong plan (unresolved target or no path) — never a
    collision, because the grid that planned the route is the same grid
    `move`/`turn`'s literal sweep checks against.

    `move` and `turn` are literal: executed exactly as planned, with no
    obstacle avoidance, so a collision there means the chain of instructions
    (not the navigation) went wrong. See
    Progress/spec-planner-profiles-and-virtual-sweep.md §0-§1.

    Every metric (dwell, summary) is in *simulated* time — distance/speed +
    turn/rate — and the wall-clock the rover actually sleeps is simulated
    time divided by VIRTUAL_TIME_SCALE, so a sweep can run at 20x without
    changing what gets measured.
    """

    name = "virtual"

    def __init__(self, room: RoomMap,
                 linear_speed_mps: float = 0.17, turn_rate_dps: float = 90.0,
                 time_scale: float = 1.0,
                 step_distance_m: float = 0.5, step_turn_deg: float = 90.0,
                 step_seconds: float = 1.0,
                 drift_frac: float = 0.0, drift_deg: float = 0.0,
                 clearance_m: Optional[float] = None, cell_m: float = 0.02,
                 robot_length_m: Optional[float] = None,
                 robot_width_m: Optional[float] = None,
                 seed: Optional[int] = None,
                 trace_dir: Optional[Path] = None):
        self.room = room
        self.pose = room.start_pose()
        self.linear_speed_mps = linear_speed_mps
        self.turn_rate_dps = turn_rate_dps
        self.time_scale = time_scale if time_scale > 0 else 1.0
        # Fallbacks only: used when a step has no distance_m/degrees field
        # and the target text carries no explicit magnitude either.
        self.step_distance_m = step_distance_m
        self.step_turn_deg = step_turn_deg
        # Real-time dwell for scan/stop/report only — they have no distance
        # or turn, so the simulated-time formula gives them 0 regardless.
        self.step_seconds = step_seconds
        self.drift_frac = drift_frac
        self.drift_deg = drift_deg
        self.seed = random.randrange(2 ** 31) if seed is None else int(seed)
        self._rng = random.Random(self.seed)

        # Kept alongside clearance_m (not just folded into it) so
        # tools/plot_run.py can draw the actual footprint rectangle, not just
        # the clearance radius, reading nothing but the trace file.
        self.robot_length_m = (robot_length_m if robot_length_m is not None
                               else config.get_float("VIRTUAL_ROBOT_LENGTH_M", 0.187))
        self.robot_width_m = (robot_width_m if robot_width_m is not None
                              else config.get_float("VIRTUAL_ROBOT_WIDTH_M", 0.162))
        self.clearance_m = clearance_m if clearance_m is not None else footprint_clearance_m()
        # The one grid, shared by approach's planner and move/turn's
        # collision sweep — they can never disagree about what is free.
        self.grid = nav.OccupancyGrid(room, self.clearance_m, cell_m=cell_m)

        self._lock = threading.Lock()
        self.trace: List[dict] = []
        self.hallucinations: List[dict] = []
        self._halted = threading.Event()
        self._run_index = 0
        self._run_steps = 0
        self.odometer_m = 0.0
        self.sim_time_s = 0.0
        self.collisions = 0
        self.no_path_count = 0
        self._visits: List[str] = []
        self._last_planned_path: Optional[List[Point]] = None

        folder = Path(trace_dir) if trace_dir else Path(__file__).parent / "logs"
        folder.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.trace_path: Optional[Path] = folder / ("virtual_run_%s.jsonl" % stamp)

    # -- the contract ------------------------------------------------------
    def execute_step(self, step: dict) -> dict:
        with self._lock:
            self._halted.clear()
            self._begin_run_if_needed(step)

            action = str(step.get("action") or "").strip().lower()
            target = step.get("target")
            start = self.pose

            if action == "approach":
                return self._do_approach(step, start, target)
            if action == "observe":
                return self._do_observe(step, start, target)
            if action == "move":
                return self._do_move(step, start, target)
            if action == "turn":
                return self._do_turn(step, start, target)
            if action in ("scan", "stop", "report"):
                if self._dwell_fixed(self.step_seconds):
                    return self._finish(step, start, start, "halted",
                                        {"reason": "halted_mid_step"}, 0.0)
                return self._finish(step, start, start, "ok", None, 0.0)

            return self._finish(step, start, start, "blocked",
                                {"reason": "unknown_action", "action": action}, 0.0)

    def halt(self) -> None:
        log.warning("[virtual] HALT")
        self._halted.set()

    # -- actions -------------------------------------------------------
    def _do_approach(self, step: dict, start: Pose, target) -> dict:
        landmark, how = self.room.resolve(target)
        if landmark is None:
            return self._unresolved(step, target)

        goal = landmark.point()
        waypoints = nav.plan_path(self.grid, start.point(), goal)
        if waypoints is None:
            return self._finish(step, start, start, "blocked",
                                {"reason": "no_path", "target": landmark.name}, 0.0)
        self._last_planned_path = waypoints

        pose = start
        total_sim_time = 0.0
        travelled = 0.0
        for point in waypoints[1:]:
            heading = pose.bearing_to(point)
            turn_delta = abs(wrap_deg(heading - pose.theta))
            leg_dist = pose.distance_to(point)
            leg_sim_time = (
                (turn_delta / self.turn_rate_dps if self.turn_rate_dps > 0 else 0.0)
                + (leg_dist / self.linear_speed_mps if self.linear_speed_mps > 0 else 0.0)
            )

            if self._dwell(leg_sim_time):
                self.odometer_m += travelled
                self.pose = pose
                return self._finish(step, Pose(point[0], point[1], heading), pose, "halted",
                                    {"reason": "halted_mid_route"}, total_sim_time,
                                    planned_path=waypoints, node=landmark.name, matched_via=how)

            pose = Pose(point[0], point[1], heading)
            travelled += leg_dist
            total_sim_time += leg_sim_time

        self.odometer_m += travelled
        self.pose = pose
        self._visits.append(landmark.name)
        return self._finish(step, pose, pose, "ok", None, total_sim_time,
                            planned_path=waypoints, node=landmark.name, matched_via=how,
                            path_length_m=round(travelled, 3),
                            straight_line_m=round(start.distance_to(goal), 3))

    def _do_observe(self, step: dict, start: Pose, target) -> dict:
        # Aims the gimbal only; the base never moves. Grouped with approach
        # because both resolve a named target the same way.
        landmark, how = self.room.resolve(target)
        if landmark is None:
            return self._unresolved(step, target)

        heading = start.bearing_to(landmark.point())
        turn_delta = wrap_deg(heading - start.theta)
        sim_time = abs(turn_delta) / self.turn_rate_dps if self.turn_rate_dps > 0 else 0.0

        if self._dwell(sim_time):
            return self._finish(step, Pose(start.x, start.y, heading), start, "halted",
                                {"reason": "halted_mid_step"}, sim_time,
                                node=landmark.name, matched_via=how)

        achieved = Pose(start.x, start.y, self._drift_turn(heading))
        self.pose = achieved
        return self._finish(step, achieved, achieved, "ok", None, sim_time,
                            node=landmark.name, matched_via=how,
                            look_bearing=round(heading, 2))

    def _do_move(self, step: dict, start: Pose, target) -> dict:
        magnitude = self._parse_distance(step, target)
        signed = -magnitude if _is_backward(target) else magnitude
        intended = start.advance(signed)
        sim_time = abs(signed) / self.linear_speed_mps if self.linear_speed_mps > 0 else 0.0

        if self._dwell(sim_time):
            return self._finish(step, intended, start, "halted",
                                {"reason": "halted_mid_step"}, sim_time)

        achieved = self._drift_move(start, intended)
        last_free, blocker = nav.sweep_segment(self.grid, start.point(), achieved.point())
        if blocker is not None:
            travelled = start.distance_to(last_free)
            self.odometer_m += travelled
            stopped = Pose(last_free[0], last_free[1], start.theta)
            self.pose = stopped
            return self._finish(step, intended, stopped, "blocked",
                                {"reason": "collision", "obstacle": blocker,
                                 "commanded_m": round(abs(signed), 3),
                                 "travelled_m": round(travelled, 3)}, sim_time)

        travelled = start.distance_to(achieved.point())
        self.odometer_m += travelled
        self.pose = achieved
        return self._finish(step, intended, achieved, "ok", None, sim_time)

    def _do_turn(self, step: dict, start: Pose, target) -> dict:
        magnitude = _turn_magnitude_from(step, target, self.step_turn_deg)
        signed = magnitude if "left" in str(target or "").lower() else -magnitude
        intended = start.rotate(signed)
        sim_time = abs(signed) / self.turn_rate_dps if self.turn_rate_dps > 0 else 0.0

        if self._dwell(sim_time):
            return self._finish(step, intended, start, "halted",
                                {"reason": "halted_mid_step"}, sim_time)

        # A rotation in place can't collide: the clearance radius is the
        # footprint's half-diagonal, safe anywhere the centre already is.
        achieved = Pose(start.x, start.y, self._drift_turn(intended.theta))
        self.pose = achieved
        return self._finish(step, intended, achieved, "ok", None, sim_time)

    # -- magnitude parsing ---------------------------------------------
    def _parse_distance(self, step: dict, target) -> float:
        value = step.get("distance_m")
        if isinstance(value, (int, float)):
            return abs(float(value))
        parsed = _distance_from_text(target)
        return parsed if parsed is not None else self.step_distance_m

    # -- drift (off by default; VIRTUAL_DRIFT_FRAC/DEG=0 in sweeps) -----
    def _drift_move(self, start: Pose, intended: Pose) -> Pose:
        if self.drift_frac <= 0.0:
            return intended
        sigma = self.drift_frac * start.distance_to(intended.point())
        if sigma <= 0.0:
            return intended
        return intended.jittered(self._rng.gauss(0.0, sigma), self._rng.gauss(0.0, sigma), 0.0)

    def _drift_turn(self, theta: float) -> float:
        if self.drift_deg <= 0.0:
            return theta
        return wrap_deg(theta + self._rng.gauss(0.0, self.drift_deg))

    # -- dwell ------------------------------------------------------------
    def _dwell(self, sim_time_s: float) -> bool:
        """Wall-clock = simulated time / VIRTUAL_TIME_SCALE. Polls so halt()
        lands within ~50ms regardless of how long the step simulates to."""
        wall_seconds = max(0.0, sim_time_s) / self.time_scale
        if wall_seconds <= 0.0:
            return self._halted.is_set()
        deadline = time.perf_counter() + wall_seconds
        while time.perf_counter() < deadline:
            if self._halted.is_set():
                return True
            time.sleep(max(0.0, min(0.05, deadline - time.perf_counter())))
        return self._halted.is_set()

    def _dwell_fixed(self, seconds: float) -> bool:
        """Unscaled real-time dwell for scan/stop/report, which have no
        distance or turn and so no simulated-time figure of their own."""
        deadline = time.perf_counter() + max(0.0, seconds)
        while time.perf_counter() < deadline:
            if self._halted.is_set():
                return True
            time.sleep(max(0.0, min(0.05, deadline - time.perf_counter())))
        return self._halted.is_set()

    # -- internals ----------------------------------------------------
    def _unresolved(self, step: dict, target) -> dict:
        self.hallucinations.append({"run": self._run_index, "step": step, "target": target})
        log.warning("[virtual] step %s: %r is not in %s's node table — hallucination %d",
                    step.get("id"), target, self.room.name, len(self.hallucinations))
        return self._finish(step, self.pose, self.pose, "blocked",
                            {"reason": "unresolved_target", "target": target}, 0.0)

    def _begin_run_if_needed(self, step: dict) -> None:
        # No mission-start hook reaches the rover, so a plan landing back on
        # step 1 is the signal. A revision that resets the cursor restarts
        # the run, which is what the figure should show anyway.
        restarted = self._run_steps > 0 and str(step.get("id")) == "1"
        if self._run_index == 0 or restarted:
            self._begin_run("restart" if restarted else "start")

    def _begin_run(self, why: str) -> None:
        self._run_index += 1
        self._run_steps = 0
        self.pose = self.room.start_pose()
        self.trace = []
        self.hallucinations = []
        self.odometer_m = 0.0
        self.sim_time_s = 0.0
        self.collisions = 0
        self.no_path_count = 0
        self._visits = []
        self._last_planned_path = None
        log.info("[virtual] run %d (%s) on %r — seed %d, trace %s",
                 self._run_index, why, self.room.name, self.seed,
                 self.trace_path.name if self.trace_path else "off")
        self._write({
            "event": "run_start", "run": self._run_index, "why": why,
            "t": time.time(), "seed": self.seed, "pose": self.pose.to_dict(),
            "settings": {
                "linear_speed_mps": self.linear_speed_mps,
                "turn_rate_dps": self.turn_rate_dps,
                "time_scale": self.time_scale,
                "step_distance_m": self.step_distance_m,
                "step_turn_deg": self.step_turn_deg,
                "clearance_m": round(self.clearance_m, 4),
                "robot_length_m": self.robot_length_m,
                "robot_width_m": self.robot_width_m,
                "drift_frac": self.drift_frac,
                "drift_deg": self.drift_deg,
            },
            # Embedded so the trace plots without needing the room file too.
            "room": self.room.to_dict(),
        })

    def _finish(self, step: dict, intended: Pose, achieved: Pose,
                status: str, detail: Optional[dict], sim_time_s: float,
                planned_path: Optional[List] = None, **extra) -> dict:
        self._run_steps += 1
        self.sim_time_s += sim_time_s
        reason = detail.get("reason") if isinstance(detail, dict) else None
        if status == "blocked":
            if reason == "collision":
                self.collisions += 1
            elif reason == "no_path":
                self.no_path_count += 1

        record = {
            "event": "step", "run": self._run_index, "seq": self._run_steps,
            "t": time.time(), "step": step, "status": status, "detail": detail,
            "reason": reason, "pose": achieved.to_dict(), "intended": intended.to_dict(),
            "sim_time_s": round(sim_time_s, 3), "odometer_m": round(self.odometer_m, 3),
        }
        if planned_path is not None:
            record["planned_path"] = [list(p) for p in planned_path]
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

    def state_snapshot(self) -> dict:
        """For the UI's live map (§4): current pose, the travelled path, the
        most recently planned approach route, and the running totals."""
        with self._lock:
            return {
                "pose": self.pose.to_dict(),
                "path": [r["pose"] for r in self.trace if r.get("event") == "step"],
                "planned_path": ([list(p) for p in self._last_planned_path]
                                 if self._last_planned_path else None),
                "odometer_m": round(self.odometer_m, 3),
                "sim_time_s": round(self.sim_time_s, 3),
            }

    def summary(self) -> dict:
        """RQ2 roll-up for the current run."""
        steps = [r for r in self.trace if r.get("event") == "step"]
        return {
            "run": self._run_index,
            "seed": self.seed,
            "steps": len(steps),
            "blocked": sum(1 for r in steps if r["status"] == "blocked"),
            "hallucinations": len(self.hallucinations),
            "unresolved_targets": [h["target"] for h in self.hallucinations],
            "odometer_m": round(self.odometer_m, 3),
            "sim_time_s": round(self.sim_time_s, 3),
            "collisions": self.collisions,
            "no_path_count": self.no_path_count,
            "visits": list(self._visits),
            "final_pose": self.pose.to_dict(),
            "trace": str(self.trace_path) if self.trace_path else None,
        }

_cache: Optional[RoverController] = None


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
            linear_speed_mps=config.get_float("VIRTUAL_LINEAR_SPEED_MPS", 0.17),
            turn_rate_dps=config.get_float("VIRTUAL_TURN_RATE_DPS", 90.0),
            time_scale=config.get_float("VIRTUAL_TIME_SCALE", 1.0),
            step_distance_m=config.get_float("VIRTUAL_STEP_M", 0.5),
            step_turn_deg=config.get_float("VIRTUAL_TURN_DEG", 90.0),
            step_seconds=config.get_float("VIRTUAL_STEP_SECONDS", 1.0),
            drift_frac=config.get_float("VIRTUAL_DRIFT_FRAC", 0.0),
            drift_deg=config.get_float("VIRTUAL_DRIFT_DEG", 0.0),
            robot_length_m=config.get_float("VIRTUAL_ROBOT_LENGTH_M", 0.187),
            robot_width_m=config.get_float("VIRTUAL_ROBOT_WIDTH_M", 0.162),
            seed=int(seed_text) if seed_text.lstrip("-").isdigit() else None,
        )
    elif name == "pi":
        # Lazy import: roslibpy (and its deps) is only needed for ROVER=pi,
        # so sim/virtual work stays dependency-free. See rover_pi.py and
        # Progress/turbopi-ros2-programming-guide.md §8 for what this does
        # and doesn't cover yet (motion only — no line-following, no
        # perception feed for ingest_frame, "approach" == "move" for now;
        # follow_line and the keyframe/arrival checks ARE wired up as of
        # 2026-09-28 — see Progress/spec-grounded-line-mission.md).
        from rover_pi import PiRoverController
        host = config.get("ROVER_PI_HOST", "").strip()
        if not host:
            raise RoverError(
                "ROVER=pi requires ROVER_PI_HOST (the robot's IP/hostname) in .env"
            )
        _cache = PiRoverController(
            host=host,
            rosbridge_port=config.get_int("ROVER_PI_ROSBRIDGE_PORT", 9090),
            move_seconds=config.get_float("ROVER_PI_MOVE_SECONDS", 1.0),
            turn_seconds=config.get_float("ROVER_PI_TURN_SECONDS", 1.0),
            linear_speed=config.get_float("ROVER_PI_LINEAR_SPEED", 0.3),
            angular_speed=config.get_float("ROVER_PI_ANGULAR_SPEED", 4.0),
            camera_port=config.get_int("ROVER_PI_CAMERA_PORT", 8080),
            sonar_stop_mm=config.get_int("ROVER_PI_SONAR_STOP_MM", 200),
            sonar_clear_mm=config.get_int("ROVER_PI_SONAR_CLEAR_MM", 300),
            obstacle_wait_s=config.get_float("ROVER_PI_OBSTACLE_WAIT_S", 5.0),
            line_timeout_s=config.get_float("ROVER_PI_LINE_TIMEOUT_S", 40.0),
        )
    else:
        raise RoverError(f"unknown ROVER={name!r} — 'sim', 'virtual', and 'pi' are implemented")
    log.info("[rover] %s", _cache.name)
    return _cache
