"""line_bench.py — bench tests T2-T5 for the grounded line mission (no models).

Runs from the Mac against the real rover, over rosbridge (same path as rover_pi.py).
Uses the stock IR `line_follow` node, which has to be running on the Pi first
(see Progress/runbook-T0-T5.md, T0).

This is deliberately standalone: it proves the follow_line control loop from
Progress/spec-grounded-line-mission.md §3B *before* it is folded into rover_pi.py,
so Claude Code can lift `follow_line()` almost verbatim once it passes.

Usage (from Dashboard/brain, with the venv python):
  venv/bin/python tools/line_bench.py --host 172.20.10.3 sonar                  # T3
  venv/bin/python tools/line_bench.py --host 172.20.10.3 run --length-cm 300    # T2 / T4a / T4b / T4c
  venv/bin/python tools/line_bench.py --host 172.20.10.3 run --halt-after 3     # T4d
  venv/bin/python tools/line_bench.py --host 172.20.10.3 run --timeout 8        # T4e (rover lifted)
  venv/bin/python tools/line_bench.py --host 172.20.10.3 run --look-left-at 4   # T5
  venv/bin/python tools/line_bench.py --host 172.20.10.3 stop                   # panic stop

Ctrl+C at any time sends STOP to the follower and a zero Twist.
"""

import argparse
import os
import sys
import threading
import time
import urllib.request

import roslibpy

T0 = time.perf_counter()


def log(msg: str) -> None:
    print(f"[{time.perf_counter() - T0:7.2f}s] {msg}", flush=True)


class Bench:
    def __init__(self, host: str, port: int, cam_port: int, pan_id: int, pan_center: int):
        self.host, self.cam_port = host, cam_port
        self.pan_id, self.pan_center = pan_id, pan_center

        self.ros = roslibpy.Ros(host=host, port=port)
        self.ros.run(timeout=10)
        if not self.ros.is_connected:
            sys.exit(f"could not reach rosbridge at {host}:{port}")
        log(f"connected to rosbridge {host}:{port}")

        self.lf_cmd = roslibpy.Service(self.ros, "/line_follow/cmd", "interfaces/srv/SetString")
        self.lf_run = roslibpy.Service(self.ros, "/line_follow/set_running", "std_srvs/srv/SetBool")

        self.cmd_vel = roslibpy.Topic(self.ros, "/cmd_vel", "geometry_msgs/msg/Twist")
        self.cmd_vel.advertise()
        self.pwm = roslibpy.Topic(self.ros, "/ros_robot_controller/pwm_servo/set_state",
                                  "ros_robot_controller_msgs/msg/SetPWMServoState")
        self.pwm.advertise()

        self._lock = threading.Lock()
        self.sonar_mm = None          # latest valid reading
        self.sonar_seq = 0            # increments per valid reading
        self.arrived = threading.Event()

        roslibpy.Topic(self.ros, "/sonar_controller/get_distance", "std_msgs/msg/Int32",
                       throttle_rate=100, queue_length=1).subscribe(self._on_sonar)
        roslibpy.Topic(self.ros, "/line_follow/crossroads_stop",
                       "std_msgs/msg/Bool").subscribe(self._on_crossroads)
        time.sleep(0.5)  # let subscriptions settle

    # -- callbacks -----------------------------------------------------------
    def _on_sonar(self, msg):
        mm = int(msg.get("data", 0))
        if 0 < mm <= 4000:  # 0 / huge = bad echo, ignore
            with self._lock:
                self.sonar_mm = mm
                self.sonar_seq += 1

    def _on_crossroads(self, msg):
        if msg.get("data"):
            log("crossroads_stop received -> ARRIVED")
            self.arrived.set()

    # -- follower control -------------------------------------------------------
    def lf(self, cmd: str, blocking: bool = False) -> None:
        req = roslibpy.ServiceRequest({"data": cmd})
        if blocking:
            resp = self.lf_cmd.call(req, timeout=3)
            log(f"cmd {cmd} -> {resp.get('message')}")
        else:  # halt path: never wait on a round trip
            self.lf_cmd.call(req, callback=lambda r: log(f"cmd {cmd} -> {r.get('message')}"),
                             errback=lambda e: log(f"cmd {cmd} ERROR {e}"))

    def set_running(self, flag: bool) -> None:
        resp = self.lf_run.call(roslibpy.ServiceRequest({"data": flag}), timeout=3)
        log(f"set_running {flag} -> {resp.get('message')}")

    def zero_twist(self) -> None:
        self.cmd_vel.publish(roslibpy.Message({"linear": {"x": 0.0, "y": 0.0, "z": 0.0},
                                               "angular": {"x": 0.0, "y": 0.0, "z": 0.0}}))

    def halt(self) -> None:
        """What rover_pi.halt() must do: stop the follower AND publish zero."""
        self.lf("STOP")
        self.zero_twist()

    # -- gimbal / camera ---------------------------------------------------------
    def pan(self, position: int, duration: float = 0.3) -> None:
        self.pwm.publish(roslibpy.Message({"duration": duration, "state": [
            {"id": [self.pan_id], "position": [int(position)], "offset": [0]}]}))

    def snapshot(self, path: str) -> bool:
        url = f"http://{self.host}:{self.cam_port}/snapshot?topic=/image_raw"
        try:
            with urllib.request.urlopen(url, timeout=2.0) as r, open(path, "wb") as f:
                f.write(r.read())
            return True
        except Exception as exc:
            log(f"snapshot failed: {exc}")
            return False

    def close(self) -> None:
        try:
            self.cmd_vel.unadvertise()
            self.pwm.unadvertise()
            self.ros.terminate()
        except Exception:
            pass

    # -- the loop that becomes rover_pi.follow_line -----------------------------
    def follow_line(self, stop_mm: int, clear_mm: int, obstacle_wait_s: float,
                    timeout_s: float, halt_after: float = 0.0, look_left_at: float = 0.0,
                    look_left_pan: int = 1100, out_dir: str = ".") -> dict:
        self.arrived.clear()
        self.lf("STOP_NEXT_ROAD", blocking=True)   # MUST precede running (default mode turns right)
        self.set_running(True)
        start = time.perf_counter()
        below, last_seq = 0, self.sonar_seq
        obstacle_events, looked = 0, False
        log("following...")

        try:
            while True:
                el = time.perf_counter() - start

                if halt_after and el >= halt_after:
                    log(">>> HALT (test) — watch the wheels: must stop within 0.5 s")
                    self.halt()
                    return {"status": "halted", "elapsed_s": round(el, 2)}

                if self.arrived.is_set():
                    return {"status": "ok", "arrived": True, "elapsed_s": round(el, 2),
                            "obstacle_events": obstacle_events}

                if look_left_at and not looked and el >= look_left_at:
                    looked = True
                    log("look-left: STOP")
                    self.lf("STOP", blocking=True)
                    time.sleep(0.3)
                    self.pan(look_left_pan)
                    time.sleep(0.6)
                    path = os.path.join(out_dir, "left_look.jpg")
                    ok = self.snapshot(path)
                    log(f"look-left frame {'saved to ' + path if ok else 'FAILED'}")
                    self.pan(self.pan_center)
                    time.sleep(0.5)
                    self.lf("CONTINUE", blocking=True)
                    log("look-left: CONTINUE (arrival trigger should still be armed)")

                with self._lock:
                    mm, seq = self.sonar_mm, self.sonar_seq
                if seq != last_seq:                      # only count NEW readings
                    last_seq = seq
                    below = below + 1 if (mm is not None and mm < stop_mm) else 0
                if below >= 2:
                    obstacle_events += 1
                    log(f"OBSTACLE at {mm} mm -> STOP, waiting up to {obstacle_wait_s}s")
                    self.lf("STOP", blocking=True)
                    t_wait = time.perf_counter()
                    cleared = False
                    while time.perf_counter() - t_wait < obstacle_wait_s:
                        with self._lock:
                            mm = self.sonar_mm
                        if mm is not None and mm > clear_mm:
                            cleared = True
                            break
                        time.sleep(0.1)
                    if not cleared:
                        return {"status": "blocked", "reason": "obstacle", "sonar_mm": mm,
                                "elapsed_s": round(time.perf_counter() - start, 2)}
                    log(f"cleared ({mm} mm) -> CONTINUE")
                    self.lf("CONTINUE", blocking=True)
                    below = 0
                    start += time.perf_counter() - t_wait   # don't count the wait against timeout

                if el > timeout_s:
                    self.lf("STOP", blocking=True)
                    return {"status": "blocked", "reason": "line_timeout", "elapsed_s": round(el, 2)}

                time.sleep(0.05)   # 20 Hz
        finally:
            self.zero_twist()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=9090)
    ap.add_argument("--cam-port", type=int, default=8080)
    ap.add_argument("--pan-id", type=int, default=2)
    ap.add_argument("--pan-center", type=int, default=1500)
    sub = ap.add_subparsers(dest="mode", required=True)

    s = sub.add_parser("sonar", help="T3: print sonar for N seconds")
    s.add_argument("--seconds", type=float, default=15)

    r = sub.add_parser("run", help="T2/T4/T5: follow the line to the crossbar")
    r.add_argument("--length-cm", type=float, default=0, help="if set, prints measured speed")
    r.add_argument("--stop-mm", type=int, default=200)
    r.add_argument("--clear-mm", type=int, default=300)
    r.add_argument("--obstacle-wait", type=float, default=5)
    r.add_argument("--timeout", type=float, default=40)
    r.add_argument("--halt-after", type=float, default=0)
    r.add_argument("--look-left-at", type=float, default=0)
    r.add_argument("--look-left-pan", type=int, default=1100)
    r.add_argument("--out", default=".")

    sub.add_parser("stop", help="panic stop")
    args = ap.parse_args()

    b = Bench(args.host, args.port, args.cam_port, args.pan_id, args.pan_center)
    try:
        if args.mode == "stop":
            b.halt()
            time.sleep(0.5)
        elif args.mode == "sonar":
            end = time.time() + args.seconds
            while time.time() < end:
                log(f"sonar = {b.sonar_mm} mm  ({(b.sonar_mm or 0) / 10:.1f} cm)")
                time.sleep(0.5)
        else:
            res = b.follow_line(args.stop_mm, args.clear_mm, args.obstacle_wait, args.timeout,
                                args.halt_after, args.look_left_at, args.look_left_pan, args.out)
            log(f"RESULT: {res}")
            if args.length_cm and res.get("arrived"):
                log(f"speed ≈ {args.length_cm / res['elapsed_s']:.1f} cm/s over {args.length_cm:.0f} cm")
    except KeyboardInterrupt:
        log("Ctrl+C -> halt")
        b.halt()
        time.sleep(0.5)
    finally:
        b.close()


if __name__ == "__main__":
    main()
