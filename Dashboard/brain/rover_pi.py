"""Real-hardware RoverController for the TurboPi (Advanced kit, ROS2/Docker).

Talks to the robot over `rosbridge_websocket` (default port 9090) using the
`roslibpy` client library — no ROS2 install needed on the Mac side. See
`Progress/turbopi-ros2-programming-guide.md` §8 for the full reference this
implementation is built from, and `Progress/spec-grounded-line-mission.md`
for the line-following slice added on 2026-09-28.

Action mapping (rover.py's RoverController.execute_step receives a plan step
{"id", "action", "target"} and must return
{"status": "ok" | "blocked" | "halted", "detail": ...}):

- follow_line      -> hand steering to the stock `line_follow` node (IR
                       4-channel sensor) and supervise it: arrival is the
                       node's own crossbar detection, obstacles are the
                       ultrasonic, and a wall-clock timeout covers the case
                       where the line is lost (the node keeps driving
                       straight at 0.16 m/s forever — it has no lost-line
                       exit of its own). The VLM never makes a distance or
                       stop call; see the spec's §0 decision table.
- move / approach  -> timed forward/back Twist pulse on /cmd_vel. There is no
                       distance in a plan step (same situation VirtualRover
                       handles with a config constant), and the real robot
                       has no landmark/node table, so "approach" is currently
                       identical to "move" — a known gap, not a design
                       choice. Travel of any real distance is follow_line's
                       job now.
- turn             -> timed Twist with angular.z, direction from the step's
                       target text (reuses rover.py's own _is_backward /
                       _turn_degrees helpers so "left"/"right"/"backward"
                       parsing stays in one place).
- observe          -> centre the gimbal and hold, so the frame the arrival
                       check reads is the forward view, not the middle of a
                       sweep.
- scan             -> the old placeholder pan sweep. Still not aimed at
                       anything real.
- stop / report    -> no-op (the base is already stationary between steps).

halt() constraint: rover.py's ABC docstring requires halt() to not need
"inference, a model, or a network call," and mission.py calls it directly
from the asyncio event loop (not via asyncio.to_thread) on cancellation, so
it must never block. roslibpy.Topic.publish() and Service.call(..., callback=)
on an already-open connection queue the message on the client's own
background thread and return immediately — neither opens a new connection or
waits on a round trip. That is the sense in which this respects the
constraint. This is an interpretation of an ambiguous rule, not a settled
one — flagged rather than silently assumed; revisit if it ever causes a
stall.

⚠️ halt() MUST send /line_follow/cmd STOP as well as a zero Twist. While the
follower is running it republishes /cmd_vel at 100 Hz, so a zero Twist alone
is overwritten within 10 ms and the rover keeps driving. This is the single
most important safety property in this file.
"""

import threading
import time
from typing import Optional, Tuple

import config
import httpx
from log_setup import get_logger
from rover import RoverController, _is_backward, _turn_degrees

log = get_logger("rover_pi")

try:
    import roslibpy
except ImportError as exc:  # pragma: no cover - surfaced at construction time
    roslibpy = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

# The node is named `line_follow`, so ~/ services resolve under /line_follow/.
# The comment in line_follow.launch.py saying /line_follower is wrong — read
# from the source on 2026-09-28, don't re-derive.
LF_NS = "/line_follow"

# Sonar readings outside this band are dropped as noise rather than treated as
# "the path is clear" or "we are about to hit something".
SONAR_MIN_MM = 1
SONAR_MAX_MM = 4000


class PiRoverController(RoverController):
    """Drives the real TurboPi over rosbridge_websocket."""

    name = "pi"

    def __init__(self, host: str, rosbridge_port: int = 9090,
                 move_seconds: float = 1.0, turn_seconds: float = 1.0,
                 linear_speed: float = 0.3, angular_speed: float = 4.0,
                 tilt_servo_id: int = 1, pan_servo_id: int = 2,
                 gimbal_center: int = 1500, gimbal_sweep: int = 300,
                 connect_timeout: float = 10.0,
                 camera_port: int = 8080,
                 sonar_stop_mm: int = 200, sonar_clear_mm: int = 300,
                 obstacle_wait_s: float = 5.0, line_timeout_s: float = 40.0,
                 pause_max_s: float = 10.0):
        if roslibpy is None:
            raise RuntimeError(
                "roslibpy is not installed — add it to requirements.txt and "
                "`pip install roslibpy` in the venv before using ROVER=pi"
            ) from _IMPORT_ERROR

        self.host = host
        self.move_seconds = move_seconds
        self.turn_seconds = turn_seconds
        self.linear_speed = linear_speed
        self.angular_speed = angular_speed
        self.tilt_servo_id = tilt_servo_id
        self.pan_servo_id = pan_servo_id
        self.gimbal_center = gimbal_center
        self.gimbal_sweep = gimbal_sweep
        self._pan_position = gimbal_center
        self._tilt_position = gimbal_center

        self.snapshot_url = "http://%s:%d/snapshot?topic=/image_raw" % (host, camera_port)
        self.sonar_stop_mm = sonar_stop_mm
        self.sonar_clear_mm = sonar_clear_mm
        self.obstacle_wait_s = obstacle_wait_s
        self.line_timeout_s = line_timeout_s
        self.pause_max_s = pause_max_s

        self._halted = threading.Event()
        self._arrived = threading.Event()
        self._pause_req = threading.Event()
        self._resume_req = threading.Event()

        # _sonar_seq increments on every accepted reading. follow_line uses it
        # to tell "two consecutive readings below the stop threshold" from
        # "one reading polled twice" — the topic is throttled, the loop is
        # not, so without a sequence number one stale value would trip the
        # 2-in-a-row rule on its own.
        self._sonar_mm: Optional[int] = None
        self._sonar_seq = 0
        self._tlock = threading.Lock()
        self.telemetry = {"following": False, "paused": False, "sonar_mm": None,
                          "t_start": None, "obstacle_events": 0}

        log.info("[pi] connecting to rosbridge at %s:%d", host, rosbridge_port)
        self.client = roslibpy.Ros(host=host, port=rosbridge_port)
        self.client.run(timeout=connect_timeout)
        if not self.client.is_connected:
            raise RuntimeError(f"could not reach rosbridge at {host}:{rosbridge_port}")

        self._cmd_vel = roslibpy.Topic(self.client, "/cmd_vel", "geometry_msgs/msg/Twist")
        self._cmd_vel.advertise()
        self._pwm = roslibpy.Topic(
            self.client, "/ros_robot_controller/pwm_servo/set_state",
            "ros_robot_controller_msgs/msg/SetPWMServoState",
        )
        self._pwm.advertise()

        self._lf_cmd = roslibpy.Service(self.client, LF_NS + "/cmd",
                                        "interfaces/srv/SetString")
        self._lf_running = roslibpy.Service(self.client, LF_NS + "/set_running",
                                            "std_srvs/srv/SetBool")

        # throttle_rate is in ms. The sonar node publishes in a tight loop with
        # no sleep, so an unthrottled subscription would flood the websocket.
        self._sonar_topic = roslibpy.Topic(
            self.client, "/sonar_controller/get_distance", "std_msgs/msg/Int32",
            throttle_rate=100, queue_length=1,
        )
        self._sonar_topic.subscribe(self._on_sonar)

        self._arrived_topic = roslibpy.Topic(
            self.client, LF_NS + "/crossroads_stop", "std_msgs/msg/Bool",
        )
        self._arrived_topic.subscribe(self._on_crossroads_stop)

        log.info("[pi] connected (snapshot=%s)", self.snapshot_url)

    # -- the contract --------------------------------------------------------
    def execute_step(self, step: dict) -> dict:
        self._halted.clear()
        action = str(step.get("action") or "").strip().lower()
        target = step.get("target")

        if action == "follow_line":
            return self._follow_line()

        if action in ("move", "approach"):
            speed = -self.linear_speed if _is_backward(target) else self.linear_speed
            return self._timed_twist(linear_x=speed, seconds=self.move_seconds)

        if action == "turn":
            degrees = _turn_degrees(target, 90.0)
            direction = 1.0 if degrees > 0 else -1.0
            return self._timed_twist(angular_z=direction * self.angular_speed,
                                      seconds=self.turn_seconds)

        if action == "observe":
            # Centre and hold: the arrival check reads the frame right after
            # this, and a mid-sweep frame would point at a wall.
            self.center_gimbal()
            time.sleep(0.5)
            return {"status": "ok", "detail": None}

        if action == "scan":
            self._gimbal_sweep()
            return {"status": "ok", "detail": None}

        if action in ("stop", "report"):
            return {"status": "ok", "detail": None}

        return {"status": "blocked", "detail": f"unknown action {action!r}"}

    def halt(self) -> None:
        log.warning("[pi] HALT")
        self._halted.set()
        # Order matters: tell the follower to stop republishing BEFORE the zero
        # Twist, or it overwrites it within 10 ms and the rover drives on.
        self._lf("STOP")
        self._publish_twist()

    # -- line following ------------------------------------------------------
    def _follow_line(self) -> dict:
        """Supervise the stock IR line follower to its end crossbar.

        The node does the steering; this owns the three things it has no
        opinion about — arrival (its own crossroads_stop topic), obstacles
        (ultrasonic) and giving up (wall clock).
        """
        self._arrived.clear()
        self._pause_req.clear()
        self._resume_req.clear()

        # STOP_NEXT_ROAD must land BEFORE set_running(True): in `default` mode
        # the node turns RIGHT at every crossroad instead of parking on it.
        self._lf("STOP_NEXT_ROAD")
        time.sleep(0.25)
        self._lf_run(True)

        t_start = time.time()
        with self._tlock:
            self.telemetry["following"] = True
            self.telemetry["paused"] = False
            self.telemetry["t_start"] = t_start

        # Time spent deliberately stationary (a look-left pause, or waiting out
        # an obstacle) does not count toward the lost-line timeout — that
        # timeout is a distance proxy, and a parked rover covers no distance.
        stopped_s = 0.0
        near_count = 0
        last_seq = -1

        try:
            while True:
                if self._halted.is_set():
                    self._lf("STOP")
                    self._publish_twist()
                    return self._lf_result("halted", "halted",
                                           t_start, stopped_s)

                if self._arrived.is_set():
                    # The node parks itself and resets to mode=default; no STOP
                    # needed, but send one anyway so nothing can restart it.
                    self._lf("STOP")
                    return self._lf_result("ok", None, t_start, stopped_s,
                                           arrived=True)

                if self._pause_req.is_set():
                    stopped_s += self._do_pause()
                    continue

                mm, seq = self._sonar()
                if seq != last_seq:
                    last_seq = seq
                    near_count = near_count + 1 if (mm is not None and mm < self.sonar_stop_mm) else 0
                    if near_count >= 2:
                        near_count = 0
                        blocked, waited = self._wait_out_obstacle(mm)
                        stopped_s += waited
                        if blocked is not None:
                            return self._lf_result(blocked["status"], blocked["reason"],
                                                   t_start, stopped_s,
                                                   sonar_mm=blocked.get("sonar_mm"))

                if (time.time() - t_start - stopped_s) > self.line_timeout_s:
                    self._lf("STOP")
                    self._publish_twist()
                    return self._lf_result("blocked", "line_timeout",
                                           t_start, stopped_s)

                time.sleep(0.05)
        finally:
            with self._tlock:
                self.telemetry["following"] = False
                self.telemetry["paused"] = False

    def _lf_result(self, status: str, reason: Optional[str], t_start: float,
                   stopped_s: float, arrived: bool = False,
                   sonar_mm: Optional[int] = None) -> dict:
        detail = {
            "arrived": arrived,
            "elapsed_s": round(time.time() - t_start, 2),
            "stopped_s": round(stopped_s, 2),
            "obstacle_events": self.telemetry.get("obstacle_events", 0),
        }
        if reason:
            detail["reason"] = reason
        if sonar_mm is not None:
            detail["sonar_mm"] = sonar_mm
        log.info("[pi] follow_line -> %s %s", status, detail)
        return {"status": status, "detail": detail}

    def _do_pause(self) -> float:
        """Park for a look-left, then resume. Returns seconds spent stopped."""
        started = time.time()
        self._lf("STOP")
        self._publish_twist()
        self._pause_req.clear()
        with self._tlock:
            self.telemetry["paused"] = True
        log.info("[pi] follow_line paused")

        deadline = started + self.pause_max_s
        while time.time() < deadline:
            if self._resume_req.is_set() or self._halted.is_set():
                break
            time.sleep(0.05)
        self._resume_req.clear()

        with self._tlock:
            self.telemetry["paused"] = False
        if not self._halted.is_set():
            # CONTINUE, not set_running: cmd STOP left mode=receive and
            # receive_cmd=stop_next_road intact, so the arrival trigger is
            # still armed. Confirmed from line_follow.py's cmd_callback.
            self._lf("CONTINUE")
            log.info("[pi] follow_line resumed")
        return time.time() - started

    def _wait_out_obstacle(self, mm: Optional[int]) -> Tuple[Optional[dict], float]:
        """Stop, wait for the way to clear, resume. (result_or_None, seconds)."""
        started = time.time()
        self._lf("STOP")
        self._publish_twist()
        with self._tlock:
            self.telemetry["obstacle_events"] += 1
        log.warning("[pi] obstacle at %s mm — stopping", mm)

        deadline = started + self.obstacle_wait_s
        last = mm
        while time.time() < deadline:
            if self._halted.is_set():
                return ({"status": "halted", "reason": "halted"}, time.time() - started)
            last, _ = self._sonar()
            if last is not None and last > self.sonar_clear_mm:
                self._lf("CONTINUE")
                log.info("[pi] obstacle cleared at %s mm — resuming", last)
                return (None, time.time() - started)
            time.sleep(0.1)

        log.warning("[pi] obstacle still at %s mm after %.1fs — blocked", last,
                    self.obstacle_wait_s)
        return ({"status": "blocked", "reason": "obstacle", "sonar_mm": last},
                time.time() - started)

    def pause(self) -> bool:
        """Ask follow_line to park. Blocks until it has (max 1 s).

        Only meaningful while follow_line is running; returns False otherwise
        so the caller can skip the look rather than take a frame at speed.
        """
        with self._tlock:
            if not self.telemetry["following"]:
                return False
        self._resume_req.clear()
        self._pause_req.set()
        deadline = time.time() + 1.0
        while time.time() < deadline:
            with self._tlock:
                if self.telemetry["paused"]:
                    return True
            time.sleep(0.02)
        log.warning("[pi] pause() timed out waiting for the follower to park")
        return False

    def resume(self) -> None:
        self._resume_req.set()

    def telemetry_snapshot(self) -> dict:
        with self._tlock:
            return dict(self.telemetry)

    # -- camera --------------------------------------------------------------
    def get_frame(self) -> Optional[bytes]:
        """One JPEG from web_video_server. Never raises — a missed frame is a
        skipped check, not a failed mission."""
        try:
            response = httpx.get(self.snapshot_url, timeout=2.0)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("[pi] snapshot failed: %s", exc)
            return None
        return response.content

    def look(self, pan_position: int, settle_s: float = 0.6) -> Optional[bytes]:
        """Pan to an absolute position, grab a frame, re-centre.

        Sign convention matches main.py's gimbal endpoint: "left" is a
        *negative* pan delta, so a left look position is below gimbal_center.
        """
        self._pan_position = int(pan_position)
        self._set_pwm(self.pan_servo_id, self._pan_position)
        time.sleep(settle_s)
        frame = self.get_frame()
        self._pan_position = self.gimbal_center
        self._set_pwm(self.pan_servo_id, self._pan_position)
        return frame

    # -- subscriptions -------------------------------------------------------
    def _on_sonar(self, message: dict) -> None:
        value = message.get("data")
        if not isinstance(value, int) or value < SONAR_MIN_MM or value > SONAR_MAX_MM:
            return
        with self._tlock:
            self._sonar_mm = value
            self._sonar_seq += 1
            self.telemetry["sonar_mm"] = value

    def _on_crossroads_stop(self, message: dict) -> None:
        if message.get("data"):
            log.info("[pi] crossroads_stop — parked at the end marker")
            self._arrived.set()

    def _sonar(self) -> Tuple[Optional[int], int]:
        with self._tlock:
            return self._sonar_mm, self._sonar_seq

    # -- service helpers -----------------------------------------------------
    def _lf(self, cmd: str) -> None:
        """Non-blocking /line_follow/cmd. Safe to call from halt()."""
        def _ok(result):
            log.info("[pi] lf cmd %s -> %s", cmd, result.get("message"))

        def _err(error):
            log.error("[pi] lf cmd %s failed: %s", cmd, error)

        try:
            self._lf_cmd.call(roslibpy.ServiceRequest({"data": cmd}), _ok, _err)
        except Exception:
            log.exception("[pi] lf cmd %s could not be sent", cmd)

    def _lf_run(self, flag: bool) -> None:
        def _ok(result):
            log.info("[pi] lf set_running %s -> %s", flag, result.get("message"))

        def _err(error):
            log.error("[pi] lf set_running %s failed: %s", flag, error)

        try:
            self._lf_running.call(roslibpy.ServiceRequest({"data": bool(flag)}), _ok, _err)
        except Exception:
            log.exception("[pi] lf set_running %s could not be sent", flag)

    # -- internals -----------------------------------------------------------
    def _publish_twist(self, linear_x: float = 0.0, linear_y: float = 0.0,
                        angular_z: float = 0.0) -> None:
        msg = roslibpy.Message({
            "linear": {"x": linear_x, "y": linear_y, "z": 0.0},
            "angular": {"x": 0.0, "y": 0.0, "z": angular_z},
        })
        self._cmd_vel.publish(msg)

    def _timed_twist(self, linear_x: float = 0.0, linear_y: float = 0.0,
                      angular_z: float = 0.0, seconds: float = 1.0) -> dict:
        self._publish_twist(linear_x, linear_y, angular_z)
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            if self._halted.is_set():
                self._publish_twist()
                return {"status": "halted", "detail": "halted mid-step"}
            time.sleep(0.05)
        self._publish_twist()  # stop
        return {"status": "ok", "detail": None}

    def _gimbal_sweep(self) -> None:
        # Placeholder motion, not aimed at anything: "scan" has no bearing to
        # look at on real hardware the way VirtualRover's node table gives it
        # one. Revisit once there's a real target to look at.
        for pan in (self.gimbal_center - self.gimbal_sweep,
                    self.gimbal_center + self.gimbal_sweep,
                    self.gimbal_center):
            self._set_pwm(self.pan_servo_id, pan)
            time.sleep(0.4)
        self._pan_position = self.gimbal_center

    def _set_pwm(self, servo_id: int, position: int, duration: float = 0.3) -> None:
        msg = roslibpy.Message({
            "duration": duration,
            "state": [{"id": [servo_id], "position": [int(position)], "offset": [0]}],
        })
        self._pwm.publish(msg)

    def close(self) -> None:
        try:
            self._lf("STOP")
            self._sonar_topic.unsubscribe()
            self._arrived_topic.unsubscribe()
            self._cmd_vel.unadvertise()
            self._pwm.unadvertise()
            self.client.terminate()
        except Exception:
            log.exception("[pi] error during close(), ignoring")

    def nudge_gimbal(self, pan_delta: int = 0, tilt_delta: int = 0, duration: float = 0.15) -> dict:
        # Rough PWM safety clamp — tune by feel; these aren't from a datasheet.
        self._pan_position = max(1000, min(2000, self._pan_position + pan_delta))
        self._tilt_position = max(1000, min(2000, self._tilt_position + tilt_delta))
        self._set_pwm(self.pan_servo_id, self._pan_position, duration=duration)
        self._set_pwm(self.tilt_servo_id, self._tilt_position, duration=duration)
        return {"pan": self._pan_position, "tilt": self._tilt_position}

    def center_gimbal(self) -> dict:
        self._pan_position = self.gimbal_center
        self._tilt_position = self.gimbal_center
        self._set_pwm(self.pan_servo_id, self._pan_position)
        self._set_pwm(self.tilt_servo_id, self._tilt_position)
        return {"pan": self._pan_position, "tilt": self._tilt_position}
