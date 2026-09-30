#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# SKRIPSIE PATCH of Hiwonder's example/line_follow/line_follow.py (2026-09-29)
# Adds automatic 90-degree bend following. Everything else is stock, and the
# node keeps the SAME name ("line_follow") and services, so rover_pi.py /
# line_bench.py need no changes to drive it.
#
#   one-sided pattern  [1,1,1,0] -> left bend  -> pivot left, keep following
#                      [0,1,1,1] -> right bend -> pivot right, keep following
#   all four black     [1,1,1,1] -> crossbar   -> stock crossroads logic
#                                                 (STOP_NEXT_ROAD parks here)
#
# Sensor order S0..S3 = left..right (from the PID sign: S0 active ->
# positive angular.z -> CCW). Disable with:  --ros-args -p auto_corners:=false
# Publishes ~/corner (std_msgs/Int32: -1 left, +1 right) each time it takes one.
# Run inside the container:  python3 ~/line_follow_corner.py
# ---------------------------------------------------------------------------
import time
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from std_msgs.msg import Bool, Int32
from std_srvs.srv import SetBool, Trigger
from interfaces.srv import SetString
from ros_robot_controller_msgs.msg import RGBStates, RGBState
import sdk.FourInfrared as infrared
from sdk.pid import PID

TURN_DURATION = 3.5                    # pivot turn duration s（原地转弯持续时间 秒）
TURN_SPEED = -1.45                     # turn angular speed rad/s（转弯角速度 弧度/秒）
COOLDOWN = 1.0                         # cooldown after turn / crossroad s（转弯/路口后冷却时间 秒）
CROSSROADS_CONFIRM = 2                 # consecutive frames to confirm crossroads（确认路口所需连续帧数）
CROSSROADS_STOP_DURATION = 0.5         # stop duration at crossroads s（路口停车持续时间 秒）
FORWARD_BEFORE_TURN_DURATION = 0.5     # forward nudge before pivot turn s（转弯前向前微调时间 秒）
FORWARD_BEFORE_TURN_SPEED = 0.17       # forward nudge speed m/s（转弯前向前微调速度 米/秒）
U_TURN_DURATION = 5.0                  # U-turn phase 0 duration s
CORNER_CONFIRM = 4                     # skripsie: consecutive 10 ms ticks (~6 mm) of a one-sided pattern = bend


class LineFollow(Node):
    def __init__(self):
        super().__init__('line_follow')
        self.cmd_pub = self.create_publisher(Twist, '/cmd_vel', 1)
        self.all_black_pub = self.create_publisher(Bool, '~/all_black', 1)
        self.crossroads_stop_pub = self.create_publisher(Bool, '~/crossroads_stop', 1)
        self.rgb_pub = self.create_publisher(RGBStates, 'sonar_controller/set_rgb', 10)
        self.line = infrared.FourInfrared()

        # turn off all RGB LEDs on start (delayed for DDS discovery)
        self._rgb_off_timer = self.create_timer(1.5, self._delayed_rgb_off)

        self.running = False

        # skripsie patch: bend following
        self.declare_parameter('auto_corners', True)
        self.auto_corners = bool(self.get_parameter('auto_corners').value)
        self.corner_confirm = 0
        self.corner_count = 0
        self.corner_pub = self.create_publisher(Int32, '~/corner', 1)

        self.mode = 'default'          # 'default' | 'receive'
        self.receive_cmd = ''          # '' | 'stop_next_road'
        self.route = []                # crossroads action queue ['C', 'R', 'L', 'S']
        self._pending_turn = 0         # 0=straight, +1=right, -1=left

        # turn state
        self.turn_dir = 0              # 0=none, +1=right, -1=left
        self.turn_end_time = 0.0
        self.turn_start_time = 0.0
        self.cooldown_until = 0.0
        self.is_u_turn = False
        self.u_turn_phase = 0          # 0=forward rotate, 1=reverse rotate, 2=forward drive
        self.u_turn_phase_start = 0.0

        # crossroads state
        self.crossroads_phase = ''       # '' | 'stopping' | 'forward_before_turn'
        self.crossroads_phase_start = 0.0
        self.crossroads_confirm = 0
        self.crossroads_count = 0

        # PID for smooth line following
        self.pid = PID(P=0.6, I=0.0, D=0.02)
        self.pid.SetPoint = 0

        # services·
        self.create_service(Trigger, '~/init_finish', self.init_finish_callback)
        self.create_service(SetBool, '~/set_running', self.set_running_callback)
        self.create_service(SetString, '~/set_mode', self.set_mode_callback)
        self.create_service(SetString, '~/cmd', self.cmd_callback)
        self.create_service(Trigger, '~/get_crossroads_count', self.get_crossroads_count_callback)
        self.create_service(Trigger, '~/reset_crossroads_count', self.reset_crossroads_count_callback)
        self.create_service(SetString, '~/debug_turn', self.debug_turn_callback)
        self.create_service(Trigger, '~/u_turn', self.u_turn_callback)

        self.timer = self.create_timer(0.01, self.control_loop)
        self.cmd_pub.publish(Twist())
        self.get_logger().info('\033[1;32mLine follow ready\033[0m')

    # ── services ────────────────────────────
    def init_finish_callback(self, req, resp):
        resp.success = True
        return resp

    def set_running_callback(self, req, resp):
        self.running = req.data
        if req.data:
            self.pid.clear()
            self.pid.SetPoint = 0
        else:
            self.cmd_pub.publish(Twist())
            self.turn_dir = 0
        resp.success = True
        resp.message = 'running' if req.data else 'stopped'
        self.get_logger().info(f'SetBool: {resp.message}')
        return resp

    def set_mode_callback(self, req, resp):
        m = req.data.strip().lower()
        if m in ('default', 'receive'):
            self.mode = m
            self.receive_cmd = ''
            self.route = []
            resp.success = True
            resp.message = f'mode={self.mode}'
            self.get_logger().info(f'\033[1;36mMode: {self.mode}\033[0m')
        else:
            resp.success = False
            resp.message = 'invalid mode (default|receive)'
        return resp

    def cmd_callback(self, req, resp):
        c = req.data.strip().upper()
        if c == 'STOP':
            self.running = False
            self.cmd_pub.publish(Twist())
            self.turn_dir = 0
            resp.success = True
            resp.message = 'stopped'
        elif c == 'CONTINUE':
            self.running = True
            resp.success = True
            resp.message = 'continuing'
        elif c == 'STOP_NEXT_ROAD':
            self.receive_cmd = 'stop_next_road'
            self.mode = 'receive'
            resp.success = True
            resp.message = 'will stop at next crossroads'
        elif len(c) > 1:
            route_chars = [ch for ch in c if ch in ('C', 'R', 'L', 'S')]
            if route_chars:
                self.route = route_chars
                self.mode = 'receive'
                resp.success = True
                resp.message = f'route: {"".join(self.route)}'
            else:
                resp.success = False
                resp.message = 'invalid route chars (C R L S)'
        else:
            route_chars = [ch for ch in c if ch in ('C', 'R', 'L', 'S')]
            if route_chars:
                self.route = route_chars
                self.mode = 'receive'
                resp.success = True
                resp.message = f'route: {"".join(self.route)}'
            else:
                resp.success = False
                resp.message = 'invalid cmd (stop|continue|stop_next_road|R|L|C|S)'
        if resp.success:
            self.get_logger().info(f'\033[1;36mCmd: {resp.message}\033[0m')
        return resp

    def get_crossroads_count_callback(self, req, resp):
        resp.success = True
        resp.message = f'crossroads passed: {self.crossroads_count}'
        self.get_logger().info(f'\033[1;36m{resp.message}\033[0m')
        return resp

    def reset_crossroads_count_callback(self, req, resp):
        self.crossroads_count = 0
        resp.success = True
        resp.message = 'crossroads count reset'
        self.get_logger().info('\033[1;36mcount reset\033[0m')
        return resp

    def debug_turn_callback(self, req, resp):
        c = req.data.strip().upper()
        if c == 'R':
            self._start_turn(time.time(), 1)
            resp.message = 'debug right turn'
        elif c == 'L':
            self._start_turn(time.time(), -1)
            resp.message = 'debug left turn'
        elif c == 'C':
            self._start_turn(time.time(), 0)
            resp.message = 'debug straight'
        else:
            resp.success = False
            resp.message = 'use R/L/C'
            return resp
        resp.success = True
        self.get_logger().info(f'\033[1;35mDebug turn: {resp.message}\033[0m')
        return resp

    def u_turn_callback(self, req, resp):
        now = time.time()
        self.turn_dir = 1
        self.turn_end_time = now + U_TURN_DURATION
        self.is_u_turn = True
        self.u_turn_phase = 0
        self.u_turn_phase_start = now
        resp.success = True
        resp.message = 'u-turn'
        self.get_logger().info('\033[1;35mU-turn 180°\033[0m')
        return resp

    # ── RGB ──────────────────────────────────
    def _delayed_rgb_off(self):
        self._rgb_off_timer.cancel()
        msg = RGBStates()
        for i in [0, 1]:
            s = RGBState()
            s.index = i
            s.red = 0
            s.green = 0
            s.blue = 0
            msg.states.append(s)
        self.rgb_pub.publish(msg)
        self.get_logger().info('\033[1;36mRGB off\033[0m')

    # ── turn helper ─────────────────────────
    def _start_turn(self, now, direction):
        """Begin forward-nudge → pivot-turn. direction: -1=L, 0=C, +1=R"""
        self._pending_turn = direction
        self.crossroads_phase = 'forward_before_turn'
        self.crossroads_phase_start = now

    # ── main loop ───────────────────────────
    def control_loop(self):
        now = time.time()

        # turning in progress (no running check — debug_turn needs this)
        if self.turn_dir != 0:
            if self.is_u_turn:
                # check middle two sensors for line re-acquisition (after 1s delay)
                if now - self.u_turn_phase_start >= 2.5:
                    s = self.line.readData()
                    if s[1] and s[2]:
                        self.turn_dir = 0
                        self.is_u_turn = False
                        self.cooldown_until = now + COOLDOWN
                        self.pid.clear()
                        self.pid.SetPoint = 0
                        self.cmd_pub.publish(Twist())
                        self.get_logger().info('\033[1;35mU-turn done (line detected)\033[0m')
                        return
                # phase transitions
                if self.u_turn_phase == 0 and now - self.u_turn_phase_start >= U_TURN_DURATION:
                    self.u_turn_phase = 1
                    self.u_turn_phase_start = now
                    self.turn_dir = -1
                    self.get_logger().info('\033[1;33mU-turn phase 1 → reverse\033[0m')
                elif self.u_turn_phase == 1 and now - self.u_turn_phase_start >= 2.0:
                    self.u_turn_phase = 2
                    self.u_turn_phase_start = now
                    self.turn_dir = 0
                    self.get_logger().info('\033[1;33mU-turn phase 2 → forward\033[0m')
                elif self.u_turn_phase == 2 and now - self.u_turn_phase_start >= 0.5:
                    self.turn_dir = 0
                    self.is_u_turn = False
                    self.cooldown_until = now + COOLDOWN
                    self.pid.clear()
                    self.pid.SetPoint = 0
                    self.cmd_pub.publish(Twist())
                    self.get_logger().info('\033[1;31mU-turn fallback\033[0m')
                    return
                # publish
                twist = Twist()
                if self.u_turn_phase in (0, 1):
                    twist.angular.z = self.turn_dir * TURN_SPEED
                else:
                    twist.linear.x = 0.12
                self.cmd_pub.publish(twist)
                return

            if now >= self.turn_end_time:
                self.turn_dir = 0
                self.cooldown_until = now + COOLDOWN
                self.pid.clear()
                self.pid.SetPoint = 0
                self.cmd_pub.publish(Twist())
                self.get_logger().info('\033[1;33mTurn timeout\033[0m')
            else:
                # sensor-based early stop after 1s delay
                if now - self.turn_start_time >= 1.0:
                    s = self.line.readData()
                    if s[1] and s[2]:
                        self.turn_dir = 0
                        self.cooldown_until = now + COOLDOWN
                        self.pid.clear()
                        self.pid.SetPoint = 0
                        self.cmd_pub.publish(Twist())
                        self.get_logger().info('\033[1;35mTurn done (line detected)\033[0m')
                        return
                twist = Twist()
                twist.angular.z = self.turn_dir * TURN_SPEED
                self.cmd_pub.publish(twist)
            return

        # crossroads stop phase — hold robot still for 2s
        if self.crossroads_phase == 'stopping':
            self.cmd_pub.publish(Twist())
            if now - self.crossroads_phase_start >= CROSSROADS_STOP_DURATION:
                if self.mode == 'default':
                    self._start_turn(now, 1)
                elif self.mode == 'receive':
                    if self.receive_cmd == 'stop_next_road':
                        self.running = False
                        self.crossroads_stop_pub.publish(Bool(data=True))
                        self.receive_cmd = ''
                        self.route = []
                        self.mode = 'default'
                        self.get_logger().info('\033[1;35mCrossroads stop — parked, mode=default\033[0m')
                        self.crossroads_phase = ''
                        self.cooldown_until = now + COOLDOWN
                    elif self.route:
                        action = self.route.pop(0)
                        if action == 'S':
                            self.running = False
                            self.crossroads_stop_pub.publish(Bool(data=True))
                            self.route = []
                            self.mode = 'default'
                            self.get_logger().info('\033[1;35mRoute S — parked, mode=default\033[0m')
                            self.crossroads_phase = ''
                            self.cooldown_until = now + COOLDOWN
                    else:
                        self.crossroads_phase = ''
                        self.cooldown_until = now + COOLDOWN
            return

        # forward nudge before pivot turn — align rotation axis over crossroads
        if self.crossroads_phase == 'forward_before_turn':
            if now - self.crossroads_phase_start >= FORWARD_BEFORE_TURN_DURATION:
                self.turn_dir = self._pending_turn
                if self._pending_turn != 0:
                    self.turn_end_time = now + TURN_DURATION
                    self.turn_start_time = now
                else:
                    self.pid.clear()
                    self.pid.SetPoint = 0
                self.crossroads_phase = ''
                self.cooldown_until = now + COOLDOWN
            else:
                twist = Twist()
                twist.linear.x = FORWARD_BEFORE_TURN_SPEED
                self.cmd_pub.publish(twist)
            return

        if not self.running:
            return

        # read sensors
        s = self.line.readData()
        self.get_logger().info(
            f'S0={int(s[0])} S1={int(s[1])} S2={int(s[2])} S3={int(s[3])}')
        self.all_black_pub.publish(Bool(data=all(s)))

        # crossroads detection (both modes, outside cooldown)
        if now > self.cooldown_until:
            if self.auto_corners:
                # --- skripsie patch: a one-sided pattern is a BEND, not a crossroad ---
                corner_dir = -1 if s == [True, True, True, False] else (
                    1 if s == [False, True, True, True] else 0)
                if corner_dir != 0:
                    self.corner_confirm += 1
                    if self.corner_confirm >= CORNER_CONFIRM:
                        self.corner_confirm = 0
                        self.crossroads_confirm = 0
                        self.corner_count += 1
                        self.corner_pub.publish(Int32(data=corner_dir))
                        self.get_logger().info(
                            f'\033[1;35mBend #{self.corner_count}: '
                            f'{"LEFT" if corner_dir < 0 else "RIGHT"}\033[0m')
                        self._start_turn(now, corner_dir)
                        return
                else:
                    self.corner_confirm = 0
                # only a full-width bar counts as a crossroad / end marker
                is_crossroads = (s == [True, True, True, True])
            else:
                is_crossroads = (s == [True, True, True, False] or
                               s == [False, True, True, True] or
                               s == [True, True, True, True])
            if is_crossroads:
                self.crossroads_confirm += 1
                if self.crossroads_confirm >= CROSSROADS_CONFIRM:
                    self.crossroads_confirm = 0
                    self.crossroads_count += 1
                    self.get_logger().info(
                        f'\033[1;33mCrossroads #{self.crossroads_count}\033[0m')

                    if self.mode == 'receive':
                        if self.receive_cmd == 'stop_next_road':
                            self.crossroads_phase = 'stopping'
                            self.crossroads_phase_start = now
                        elif self.route:
                            action = self.route[0]  # peek
                            if action in ('C', 'R', 'L'):
                                self.route.pop(0)
                                if not self.route:
                                    self.receive_cmd = 'stop_next_road'
                                    self.get_logger().info('\033[1;36mRoute empty — next crossroads auto-S\033[0m')
                                self._start_turn(now, {'C': 0, 'R': 1, 'L': -1}[action])
                            else:  # S
                                self.crossroads_phase = 'stopping'
                                self.crossroads_phase_start = now
                        else:
                            self.crossroads_phase = ''
                            self.cooldown_until = now + COOLDOWN
                    else:  # default
                        self.crossroads_phase = 'stopping'
                        self.crossroads_phase_start = now
                    return
            else:
                self.crossroads_confirm = max(0, self.crossroads_confirm - 1)

        # all-black → immediate stop, don't let PID drive through crossroads
        if all(s):
            self.cmd_pub.publish(Twist())
            return

        # PID line following — weighted sensor position
        twist = Twist()
        active = [i for i, v in enumerate(s) if v]
        if active:
            positions = [-3, -1, 1, 3]
            feedback = sum(positions[i] for i in active) / len(active)
            self.pid.update(feedback)
            twist.angular.z = max(-1.38, min(1.38, float(self.pid.output)))
            twist.linear.x = max(0.16, 0.18 - abs(feedback) * 0.01)
        else:
            twist.linear.x = 0.16

        self.cmd_pub.publish(twist)


def main():
    rclpy.init()
    node = LineFollow()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
