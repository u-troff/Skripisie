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
                       target text ("left" -> positive, else negative — same
                       convention as rover.py's own _do_turn; reuses
                       rover.py's _is_backward for move/approach).
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
most important safety property in this file. It sends STOP *twice* — once
immediately and again 0.7 s later — because a STOP that lands inside the
node's 0.5 s "forward nudge" before a pivot turn does not cancel the turn
that follows it (found during T0-T5; spec §6.2).

Bends (spec §7): with `Dashboard/pi_client/line_follow_corner.py` running in
place of the stock node, a 90-degree bend is a pivot turn rather than a
crossbar park, announced on ~/corner. Two consequences live in this file: a
pause must never land mid-pivot (corner_guard_active()), and the bends taken
are cheap sensor-grounded evidence of the physical route, so they are kept
and handed to the report.

Gimbal (spec §8): both servos are mounted reversed relative to the labels in
main.py, so "left" in code pointed right in the room. _pwm() is the single
place that is compensated for; every caller that means a *direction* goes
through it, and look() is the one deliberate exception (LOOK_LEFT_PAN is a
raw PWM value set by eye against the physical robot).
"""

import threading
import time
from typing import List, Optional, Tuple

import config
import httpx
from log_setup import get_logger
from rover import RoverController, _is_backward

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

# Rough PWM safety clamp for both gimbal servos — tuned by feel, not from a
# datasheet. Offsets are clamped to whichever side of centre is narrower, so a
# held-down dashboard button can't wind the stored offset past the limit and
# then snap when it comes back.
GIMBAL_PWM_MIN = 1000
GIMBAL_PWM_MAX = 2000

# A STOP sent this soon after the previous one covers the stock node's 0.5 s
# pre-turn forward nudge, during which a STOP is swallowed.
HALT_REPEAT_S = 0.7
# -- status lights (spec-supervisor-feedback-2026-10-05.md B) ---------------------
# RGBStates{states: [RGBState{index, red, green, blue}]}, the message the stock nodes use
# (see line_follow_corner.py). The sonar ring's indices (0,1) are the ones that file uses;
# the board's (1,2) are NOT confirmed - check on the unit and override in .env.
_RGB_BOARD_IDX = [int(i) for i in config.get("RGB_BOARD_INDICES", "1,2").split(",") if i.strip()]
_RGB_SONAR_IDX = [int(i) for i in config.get("RGB_SONAR_INDICES", "0,1").split(",") if i.strip()]
# state -> (frames [(rgb, dwell_s)], loop?, final steady colour). Colours are a starting point.
_OFF = (0, 0, 0)
_LIGHTS = {
    "listening": ([((0, 0, 255), 0.0)], False, (0, 0, 255)),
    "thinking": ([((255, 0, 0), 0.5), ((0, 255, 0), 0.5)], True, None),
    "confirm": ([((255, 160, 0), 0.0)], False, (255, 160, 0)),
    "executing": ([((0, 255, 0), 0.0)], False, (0, 255, 0)),
    "searching": ([((0, 255, 255), 0.0)], False, (0, 255, 255)),
    "awaiting_guidance": ([((255, 0, 255), 1.0), (_OFF, 1.0)], True, None),
    "arrived": ([((0, 255, 0), 0.25), (_OFF, 0.25)] * 3, False, (0, 255, 0)),
    "blocked": ([((255, 0, 0), 0.0)], False, (255, 0, 0)),
}



class PiRoverController(RoverController):
    """Drives the real TurboPi over rosbridge_websocket."""

    name = "pi"

    def __init__(self, host: str, rosbridge_port: int = 9090,
                 move_seconds: float = 1.0, turn_seconds: float = 1.0,
                 linear_speed: float = 0.3, angular_speed: float = 4.0,
                 free_speed_x:float = 0.25,free_speed_cmps:float = 15.0,
                 free_turn_z:float = 3.0,fallback_hop_cm:float =30.0,
                 tilt_servo_id: int = 1, pan_servo_id: int = 2,
                 gimbal_center: int = 1500, gimbal_sweep: int = 300,
                 connect_timeout: float = 10.0,
                 camera_port: int = 8080,
                 sonar_stop_mm: int = 200, sonar_clear_mm: int = 300,
                 obstacle_wait_s: float = 5.0, line_timeout_s: float = 40.0,
                 pause_max_s: float = 10.0,
                 line_route: str = "STOP_NEXT_ROAD", corner_guard_s: float = 5.0,
                 pan_invert: bool = False, tilt_invert: bool = False,
                 look_offset: int = 400, aim_settle_s: float = 0.5,
                 rgb_enabled: bool = True):
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
        #self driving angular speed
        self.free_speed_x = free_speed_x
        self.free_speed_cmps = free_speed_cmps
        self.free_turn_z = free_turn_z
        self.fallback_hop_cm = fallback_hop_cm
        self.tilt_servo_id = tilt_servo_id
        self.pan_servo_id = pan_servo_id
        self.gimbal_center = gimbal_center
        self.gimbal_sweep = gimbal_sweep
        self.pan_invert = pan_invert
        self.tilt_invert = tilt_invert
        # How far off centre aim("left"/"right") swings, as a LOGICAL offset —
        # so the two directions are genuinely mirrored whichever way the servo
        # is mounted. 400 is |1500 - 1100|, i.e. the same throw LOOK_LEFT_PAN
        # was set to by eye.
        self.look_offset = look_offset
        self.aim_settle_s = aim_settle_s
        # Logical offsets from centre, NOT raw PWM: negative pan = look left,
        # positive tilt = look up (main.py's convention, kept deliberately).
        # _pwm() turns one into the other and is the only place the servos'
        # physical inversion is applied — spec §8.
        self._pan_offset = 0
        self._tilt_offset = 0

        self.snapshot_url = "http://%s:%d/snapshot?topic=/image_raw" % (host, camera_port)
        self.sonar_stop_mm = sonar_stop_mm
        self.sonar_clear_mm = sonar_clear_mm
        self.obstacle_wait_s = obstacle_wait_s
        self.line_timeout_s = line_timeout_s
        self.pause_max_s = pause_max_s
        # STOP_NEXT_ROAD parks at the first crossbar (a straight track). An
        # L/R/C string instead makes the node turn at each crossroad in order
        # and park at the next one — so the route belongs in config, not
        # hard-coded here. Note this is the *crossroad* route; 90-degree bends
        # are handled by line_follow_corner.py without consuming a letter.
        self.line_route = line_route or "STOP_NEXT_ROAD"
        self.corner_guard_s = corner_guard_s

        self._halted = threading.Event()
        self._arrived = threading.Event()
        self._pause_req = threading.Event()
        self._resume_req = threading.Event()
        self._halted = threading.Event()
        self._arrived = threading.Event()
        self._pause_req = threading.Event()
        self._resume_req = threading.Event()
        # Distinct from _halted: this means "the named target was confirmed
        # visible mid-drive", which _follow_line reports as a successful
        # arrival, not an abort.
        self._target_confirmed = threading.Event()
        self._target_name: Optional[str] = None


        # _sonar_seq increments on every accepted reading. follow_line uses it
        # to tell "two consecutive readings below the stop threshold" from
        # "one reading polled twice" — the topic is throttled, the loop is
        # not, so without a sequence number one stale value would trip the
        # 2-in-a-row rule on its own.
        self._sonar_mm: Optional[int] = None
        self._sonar_seq = 0

        # Bends taken, from line_follow_corner.py's ~/corner topic. _corner_cursor
        # is how much of it the mission has already been told about; _last_corner_t
        # is what corner_guard_active() reads. It starts far in the past so the
        # guard is open before the first bend rather than closed.
        self._corners: List[dict] = []
        self._corner_cursor = 0
        self._last_corner_t = float("-inf")

        self._tlock = threading.Lock()
        self.telemetry = {"following": False, "paused": False, "sonar_mm": None,
                          "t_start": None, "obstacle_events": 0, "bends": []}

        log.info("[pi] connecting to rosbridge at %s:%d", host, rosbridge_port)
        self.client = roslibpy.Ros(host=host, port=rosbridge_port)
        self.client.run(timeout=connect_timeout)
        if not self.client.is_connected:
            raise RuntimeError(f"could not reach rosbridge at {host}:{rosbridge_port}")

        self._cmd_vel = roslibpy.Topic(self.client, "/cmd_vel", "geometry_msgs/msg/Twist")
        self._cmd_vel.advertise()
        self._pwm_topic = roslibpy.Topic(
            self.client, "/ros_robot_controller/pwm_servo/set_state",
            "ros_robot_controller_msgs/msg/SetPWMServoState",
        )
        self._pwm_topic.advertise()

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

        # Only line_follow_corner.py publishes this; against the stock node the
        # subscription simply never fires and everything below degrades to the
        # straight-track behaviour.
        self._corner_topic = roslibpy.Topic(
            self.client, LF_NS + "/corner", "std_msgs/msg/Int32",
        )
        self._corner_topic.subscribe(self._on_corner)

        log.info("[pi] connected (snapshot=%s, route=%s, pan_invert=%s, tilt_invert=%s)",
                 self.snapshot_url, self.line_route, self.pan_invert, self.tilt_invert)

        # Status lights. A failure here must never stop the rover from starting.
        self._rgb_enabled = bool(rgb_enabled)
        self._light_gen = 0
        self._light_lock = threading.Lock()
        self._rgb_topics = []
        if self._rgb_enabled:
            try:
                for name in ("/ros_robot_controller/set_rgb", "/sonar_controller/set_rgb"):
                    topic = roslibpy.Topic(self.client, name, "ros_robot_controller_msgs/msg/RGBStates")
                    topic.advertise()
                    self._rgb_topics.append(topic)
            except Exception:
                log.warning("[pi] RGB topics unavailable - status lights disabled", exc_info=True)
                self._rgb_topics = []


    # -- the contract --------------------------------------------------------
    def execute_step(self, step: dict) -> dict:
        self._halted.clear()
        self._target_confirmed.clear()
        action = str(step.get("action") or "").strip().lower()

        

        if action == "follow_line":
            return self._follow_line()

        target = step.get("target")

        if action == "approach":
            # Blocking fallback for callers without the NAV_MODE=free
            # controller (mission.py runs the real search/approach loop
            # instead when NAV_MODE=free — see spec-free-roam-approach.md §2).
            return self.hop(self.fallback_hop_cm)

        if action == "move":
            speed = -self.linear_speed if _is_backward(target) else self.linear_speed
            return self._timed_twist(linear_x=speed, seconds=self.move_seconds)

        if action == "turn":
            # Real hardware has no distance/degrees in the plan step to act on
            # (same as move above) — only direction, at a fixed pulse
            # duration. Same "left" convention as rover.py's _do_turn.
            direction = 1.0 if "left" in str(target or "").lower() else -1.0
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
        # ...and again, off a timer. A STOP that lands inside the node's 0.5 s
        # forward nudge before a pivot turn is swallowed, and the turn still
        # happens. The timer thread is what keeps this non-blocking, which
        # halt() must be (see the module docstring).
        threading.Timer(HALT_REPEAT_S, self._halt_again).start()

    def _halt_again(self) -> None:
        try:
            self._lf("STOP")
            self._publish_twist()
        except Exception:
            log.exception("[pi] repeat HALT failed")
    # -- status lights -------------------------------------------------------
    def status_light(self, state: str) -> None:
        """Show a mission state on the board LEDs and the sonar ring. Returns at once: the
        publish is queued by roslibpy, and blinking runs on a short-lived thread that stops
        itself as soon as a newer state arrives. Never raises."""
        if not self._rgb_enabled or not self._rgb_topics:
            return
        pattern = _LIGHTS.get(state)
        if pattern is None:
            log.warning("[pi] status_light: unknown state %r", state)
            return
        with self._light_lock:
            self._light_gen += 1
            gen = self._light_gen
        threading.Thread(target=self._run_light, args=(gen, pattern), daemon=True).start()

    def _run_light(self, gen: int, pattern) -> None:
        frames, loop, final = pattern
        try:
            while True:
                for colour, dwell in frames:
                    if not self._set_rgb(gen, colour):
                        return
                    end = time.time() + dwell
                    while time.time() < end:
                        if gen != self._light_gen:
                            return
                        time.sleep(0.05)
                if not loop:
                    break
            if final is not None and frames[-1][0] != final:   # steady states are already showing
                self._set_rgb(gen, final)
        except Exception:
            log.warning("[pi] status light failed", exc_info=True)

    def _set_rgb(self, gen: int, colour) -> bool:
        """Publish one colour unless a newer state has superseded `gen` (checked under the
        lock, so a stale blink can never overwrite the new state)."""
        red, green, blue = colour
        with self._light_lock:
            if gen != self._light_gen:
                return False
            for topic, indices in zip(self._rgb_topics, (_RGB_BOARD_IDX, _RGB_SONAR_IDX)):
                topic.publish(roslibpy.Message({"states": [
                    {"index": i, "red": red, "green": green, "blue": blue} for i in indices]}))
        return True



    def confirm_target(self, target: str) -> None:
        """Called mid-drive when a scan confirms the step's named target is
        visible. Stops the chassis the same way halt() does, but as a
        distinct event from _halted so _follow_line reports this as a
        successful arrival, not an abort."""
        log.warning("[pi] target confirmed mid-drive: %r", target)
        self._target_name = target
        self._target_confirmed.set()


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
        with self._tlock:
            self._corners = []
            self._corner_cursor = 0
            self._last_corner_t = float("-inf")
            self.telemetry["bends"] = []

        # The route must land BEFORE set_running(True): in `default` mode the
        # node turns RIGHT at every crossroad instead of parking on it.
        self._lf(self.line_route)
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
                if self._target_confirmed.is_set():
                    self._lf("STOP")
                    self._publish_twist()
                    return self._lf_result("ok", None, t_start, stopped_s,
                                           arrived=True, target_confirmed=self._target_name)

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
                   sonar_mm: Optional[int] = None,
                   target_confirmed: Optional[str] = None) -> dict:
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
        if target_confirmed:
            detail["target_confirmed"] = target_confirmed
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
        so the caller can skip the look rather than take a frame at speed. It
        also returns False inside the corner guard — callers that want to wait
        the bend out rather than give up should test corner_guard_active()
        first, which is what mission.keyframe_monitor does.
        """
        with self._tlock:
            if not self.telemetry["following"]:
                return False
        if self.corner_guard_active():
            # Not a failure: the caller is expected to try again in a moment.
            log.info("[pi] pause() refused — a bend was taken less than %.0fs ago",
                     self.corner_guard_s)
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
        """Pan to a RAW PWM position, grab a frame, re-centre.

        `pan_position` (LOOK_LEFT_PAN) deliberately bypasses _pwm() and its
        inversion — spec §8. It is set by eye against the physical robot, so
        it already encodes whichever way the servo is mounted; running it
        through the inversion as well would undo the measurement. Everything
        that means a *direction* rather than a measured PWM value goes through
        _pwm() instead.

        The frame itself is upright and must NOT be rotated or flipped before
        the VLM — checked against left_look.jpg on 2026-09-29.
        """
        self._set_pwm(self.pan_servo_id, int(pan_position))
        time.sleep(settle_s)
        frame = self.get_frame()
        self._pan_offset = 0
        self._set_pwm(self.pan_servo_id, self._pwm(0, self.pan_invert))
        return frame

    # Logical pan offsets. "centre" is spelled both ways because callers come
    # from config strings as well as code.
    _AIM = {"left": -1, "centre": 0, "center": 0, "right": 1}

    def aim(self, direction: str) -> Optional[str]:
        """Point the camera left, centre or right and settle. No frame taken.

        The counterpart to look(): look() takes a measured RAW PWM value and is
        the §8 exception, while aim() takes a *direction* and goes through
        _pwm(), so "left" is left in the room and right is its exact mirror.
        That symmetry is the whole reason this exists — there is no way to
        mirror LOOK_LEFT_PAN, because a measured PWM value has no sign.

        Panning does not disturb the drive: the IR array is bolted to the
        chassis, not the gimbal, which is exactly why §0 chose it over the
        camera line-follower ("keeps the camera free for the VLM"). So this is
        safe to call while following a line, with no pause.

        Returns the direction actually aimed at, or None if it was not a
        recognised direction — so a caller can log the physical direction
        rather than a PWM number, per §8.
        """
        key = str(direction or "").strip().lower()
        if key not in self._AIM:
            log.warning("[pi] aim(%r): not a direction", direction)
            return None
        self.set_gimbal(pan=self._AIM[key] * self.look_offset)
        return "centre" if self._AIM[key] == 0 else key

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

    def _on_corner(self, message: dict) -> None:
        """line_follow_corner.py took a 90-degree bend: -1 left, +1 right."""
        value = message.get("data")
        if not isinstance(value, int) or value == 0:
            return
        side = "left" if value < 0 else "right"
        now = time.time()
        with self._tlock:
            self._last_corner_t = now
            self._corners.append({"side": side, "at": now})
            self.telemetry["bends"] = [c["side"] for c in self._corners]
        log.info("[pi] bend taken: %s", side)

    def corner_guard_active(self) -> bool:
        """True while the follower may still be mid-pivot.

        A STOP during the pivot cancels the turn, and the node then resumes
        straight — off the tape. So the one planned stop waits this out rather
        than risking the run. Known gap: the sonar's obstacle stop does NOT
        respect this (a real obstacle outranks a lost line), so a box dropped
        exactly on a bend is expected to end in line_timeout. Accepted for this
        slice; the report says so.
        """
        with self._tlock:
            last = self._last_corner_t
        return (time.time() - last) < self.corner_guard_s

    def drain_corners(self) -> List[dict]:
        """Bends taken since the last call, as {"side", "at"} (wall clock).

        Drained rather than read so the mission can file each bend once, at the
        time it happened, instead of re-reporting the whole list every tick.
        """
        with self._tlock:
            new = self._corners[self._corner_cursor:]
            self._corner_cursor = len(self._corners)
        return [dict(c) for c in new]

    def corners(self) -> List[dict]:
        with self._tlock:
            return [dict(c) for c in self._corners]

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


    def pivot(self, direction: int, seconds: float) -> dict:
        """Timed in-place turn for the free-roam approach loop
        (spec-free-roam-approach.md §2/§3A).

        direction: +1 = right (clockwise), -1 = left — the OPPOSITE sign
        convention from execute_step's "turn" (where +1 means left). Kept
        separate deliberately: mission.py's approach loop derives direction
        from e = x_center - 0.5 (right of centre = positive), and this
        mirrors that directly rather than making the caller flip a sign.

        Sign flipped 2026-10-02, then flipped BACK 2026-10-03: the 10-02 fix
        was based on an inferred symptom (target pushed off-centre) and
        turned out backward. Directly observed 2026-10-03
        (mission_69ebe46cc023.json, cycle 5 — x_center=0.116, pivot_dir=-1,
        logged as "L"): the chassis physically turned RIGHT on `direction`
        with no negation. -direction is right; direction was wrong.
        """
        self._lf("STOP")
        return self._timed_twist(angular_z=-direction * self.free_turn_z, seconds=seconds)

    def hop(self, cm: float, backwards: bool = False) -> dict:
        """Timed forward/back Twist pulse with its own 20 Hz sonar watchdog.

        Distinct from _timed_twist's halt-only watchdog: two consecutive NEW
        sonar readings below sonar_stop_mm zero the Twist immediately and
        return {"status": "sonar_stop", "sonar_mm": ..., "moved_s": ...}
        instead of running the full duration. Backwards hops skip the sonar —
        it faces forward, so a reading taken while reversing says nothing
        about what is behind the direction of travel.
        """
        self._lf("STOP")
        seconds = abs(cm) / self.free_speed_cmps if self.free_speed_cmps > 0 else 0.0
        linear_x = -self.free_speed_x if backwards else self.free_speed_x

        self._publish_twist(linear_x=linear_x)
        deadline = time.perf_counter() + seconds
        started = time.perf_counter()
        near_count = 0
        last_seq = -1

        while time.perf_counter() < deadline:
            if self._halted.is_set():
                self._publish_twist()
                return {"status": "halted", "detail": "halted mid-hop"}
            if not backwards:
                mm, seq = self._sonar()
                if seq != last_seq:
                    last_seq = seq
                    if mm is not None and mm < self.sonar_stop_mm:
                        near_count += 1
                    else:
                        near_count = 0
                    if near_count >= 2:
                        self._publish_twist()
                        return {"status": "sonar_stop", "sonar_mm": mm,
                                "moved_s": round(time.perf_counter() - started, 2)}
            time.sleep(0.05)

        self._publish_twist()
        return {"status": "ok", "moved_s": round(time.perf_counter() - started, 2)}


    def _gimbal_sweep(self) -> None:
        # Placeholder motion, not aimed at anything — see the original
        # docstring note this replaces (kept for context, not removed).
        for offset in (-self.gimbal_sweep, self.gimbal_sweep, 0):
            self.set_gimbal(pan=offset, settle_s=0.4)


    def _set_pwm(self, servo_id: int, position: int, duration: float = 0.3) -> None:
        msg = roslibpy.Message({
            "duration": duration,
            "state": [{"id": [servo_id], "position": [int(position)], "offset": [0]}],
        })
        self._pwm_topic.publish(msg)

    def close(self) -> None:
        try:
            self._lf("STOP")
            self._sonar_topic.unsubscribe()
            self._arrived_topic.unsubscribe()
            self._corner_topic.unsubscribe()
            self._cmd_vel.unadvertise()
            self._pwm_topic.unadvertise()
            self.client.terminate()
        except Exception:
            log.exception("[pi] error during close(), ignoring")

    def _pwm(self, offset: int, invert: bool) -> int:
        """Logical offset from centre -> servo PWM. The ONLY inversion point.

        Both servos are mounted reversed relative to main.py's labels, so
        "left" in code pointed right in the room until this existed (spec §8).
        Everything that means a direction — nudge_gimbal, the dashboard's
        /pi/gimbal/{direction}, scan's sweep, centre — resolves through here,
        so there is exactly one sign to get right rather than four.
        """
        value = self.gimbal_center - offset if invert else self.gimbal_center + offset
        return max(GIMBAL_PWM_MIN, min(GIMBAL_PWM_MAX, int(value)))

    def _offset_limit(self) -> int:
        """Clamp offsets, not just PWM: otherwise a held-down dashboard button
        winds the stored offset past the servo's range and the first nudge
        back does nothing visible."""
        return min(self.gimbal_center - GIMBAL_PWM_MIN, GIMBAL_PWM_MAX - self.gimbal_center)

    def _apply_gimbal(self, duration: float = 0.3) -> dict:
        pan_pwm = self._pwm(self._pan_offset, self.pan_invert)
        tilt_pwm = self._pwm(self._tilt_offset, self.tilt_invert)
        self._set_pwm(self.pan_servo_id, pan_pwm, duration=duration)
        self._set_pwm(self.tilt_servo_id, tilt_pwm, duration=duration)
        return {"pan": pan_pwm, "tilt": tilt_pwm,
                "pan_offset": self._pan_offset, "tilt_offset": self._tilt_offset}

    def nudge_gimbal(self, pan_delta: int = 0, tilt_delta: int = 0, duration: float = 0.15) -> dict:
        """pan_delta/tilt_delta are LOGICAL: negative pan = left, positive
        tilt = up, matching main.py's direction map unchanged."""
        limit = self._offset_limit()
        self._pan_offset = max(-limit, min(limit, self._pan_offset + int(pan_delta)))
        self._tilt_offset = max(-limit, min(limit, self._tilt_offset + int(tilt_delta)))
        return self._apply_gimbal(duration=duration)

    def set_gimbal(self, pan: Optional[int] = None, tilt: Optional[int] = None,
                    settle_s: Optional[float] = None, duration: float = 0.35) -> dict:
        """Absolute counterpart to nudge_gimbal. pan/tilt are LOGICAL offsets
        (negative pan = left, positive tilt = up); None leaves that axis alone
        — the search sweep must be able to move pan without disturbing tilt."""
        limit = self._offset_limit()
        if pan is not None:
            self._pan_offset = max(-limit, min(limit, int(pan)))
        if tilt is not None:
            self._tilt_offset = max(-limit, min(limit, int(tilt)))
        result = self._apply_gimbal(duration=duration)
        time.sleep(settle_s if settle_s is not None else self.aim_settle_s)
        return result


    def center_gimbal(self) -> dict:
        self._pan_offset = 0
        self._tilt_offset = 0
        return self._apply_gimbal()
