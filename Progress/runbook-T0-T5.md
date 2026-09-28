# Runbook — bench tests T0–T5 (grounded line mission, Day 1)

Companion to `Progress/spec-grounded-line-mission.md`. No models are involved. T0, T1 and T3a run
**on the Pi**; T2 and T3b–T5 run **from the Mac** with `Dashboard/brain/tools/line_bench.py`.

**Before you start**
- Lay the course: matte black tape on the beige floor, **2.5–4 m long**, with a crossbar about
  20 cm wide at the end. Mark a 100 cm stretch with two small side ticks, and measure the full
  length from the start point to the crossbar.
- Keep a hand near the rover for every run. Panic stop is Ctrl+C in the script, or the
  one-liner in §Panic.
- Record every result in the table at the bottom — it becomes Ch4 evidence.

---

## Terminal setup (on the Pi)

Open three SSH sessions to the Pi. In each one, enter the container:

```bash
docker exec -it -u ubuntu -w /home/ubuntu turbopi /bin/zsh
source ~/.zshrc
```

- **Terminal A:** bring-up (if it isn't already running from boot) and then the follower node.
- **Terminal B:** service calls.
- **Terminal C:** echoes.

---

## T0 — Bring-up and follower node (about 10 min)

Only if bring-up isn't already clean — this is your known-good sequence from the host, not
inside the container. Ask yourself before you restart:

```bash
docker restart turbopi && sleep 8 && docker exec -u ubuntu -w /home/ubuntu turbopi /bin/zsh -c \
  "source ~/.zshrc && ros2 launch bringup bringup.launch.py"
```

Then in **Terminal B**:

```bash
ros2 pkg executables example | grep line_follow   # expect: example line_follow
ros2 node list | grep -E "mecanum|sonar|line"      # expect exactly one /mecanum, and /sonar_controller
```

- If `/sonar_controller` is missing (it arrives through `avoidance_node.launch.py`), start it
  yourself in a spare shell: `ros2 run peripherals sonar_controller`.
- If `line_follow` isn't listed as an executable, stop and tell me. Building the package is an
  ask-first step.

Start the follower in **Terminal A**. Run the node on its own — **not** `line_follow.launch.py`,
which would start a second `mecanum` node:

```bash
ros2 run example line_follow
```

Expect `Line follow ready`. The node sits idle and prints nothing until it is running.

Back in **Terminal B**:

```bash
ros2 service list | grep line_follow
ros2 node list | grep -c mecanum     # must print 1
```

✅ **Pass:** services `/line_follow/cmd`, `/line_follow/set_running` (and others) are listed, and
there is exactly one mecanum node.

---

## T1 — IR polarity, then follow to the crossbar (about 20 min)

**T1a — polarity (the rover doesn't move).** In **Terminal B**, hold the rover over the tape,
then over bare floor:

```bash
python3 -c "
import smbus, time
b = smbus.SMBus(1)
while True:
    v = b.read_byte_data(0x78, 1)
    print(['LINE' if v & m else '....' for m in (1, 2, 4, 8)]); time.sleep(0.2)"
```

✅ **Pass:** sensors over the tape read `LINE` and sensors over the floor read `....`. That's the
polarity `line_follow.py` assumes.
- ❌ If it's **inverted** (floor reads `LINE`), the stock node will not work as-is. Stop and tell
  me — it needs a one-line fix.
- ❌ If the readings flicker over plain floor, the floor is too glossy. Move the course.

Ctrl+C to exit.

**T1b — follow to the crossbar.** Put the rover on the tape, pointing along it, at least 30 cm
before the crossbar and **not** on it.

**Terminal C** (watch for the arrival event):

```bash
ros2 topic echo /line_follow/crossroads_stop
```

**Terminal B** — this order matters:

```bash
ros2 service call /line_follow/cmd interfaces/srv/SetString "{data: 'STOP_NEXT_ROAD'}"
ros2 service call /line_follow/set_running std_srvs/srv/SetBool "{data: true}"
```

**Stop commands** (keep them ready):

```bash
ros2 service call /line_follow/cmd interfaces/srv/SetString "{data: 'STOP'}"
```

✅ **Pass, 5 out of 5 runs:**
- the rover tracks the tape;
- it parks on the crossbar (Terminal A logs `Crossroads stop — parked`);
- Terminal C prints `data: true`.

Note how far past the crossbar it stops each time.

⚠️ **Never** call `set_running true` without sending `STOP_NEXT_ROAD` first. In default mode the
rover turns RIGHT at the crossbar and keeps going.

---

## Mac setup (once, before T2)

The script `line_bench.py` goes in `Dashboard/brain/tools/`.

```bash
cd ~/Desktop/Skripsie/Dashboard/brain
venv/bin/python -c "import roslibpy; print(roslibpy.__version__)"   # if this fails: venv/bin/pip install roslibpy
export PI=172.20.10.3
```

Leave `line_follow` running in Terminal A throughout, so you can watch its logs.

---

## T2 — Speed calibration (about 10 min)

Put the rover at the start of the course. Use the full measured length from the start point to
the crossbar:

```bash
venv/bin/python tools/line_bench.py --host $PI run --length-cm 300
```

Do 3 runs. The script prints `speed ≈ X cm/s`; take the mean.

✅ **Pass:** all 3 runs arrive, and the speeds agree within about 10%.

➡️ Then:
- set `LINE_SPEED_CMPS` to the mean;
- set `ROVER_PI_LINE_TIMEOUT_S` to `1.5 × length ÷ speed`;
- use that value as `--timeout` from now on.

(The figure includes the acceleration phase, which is fine — that's what the mission sees too.)

---

## T3 — Sonar (about 10 min)

**T3a — on the Pi, Terminal C:**

```bash
ros2 topic echo --once /sonar_controller/get_distance
```

Run it with a box at 10, 20 and 30 cm in front of the sensor.

**T3b — from the Mac** (the same subscription path the mission uses):

```bash
venv/bin/python tools/line_bench.py --host $PI sonar --seconds 20
```

Move the box between 10, 20 and 30 cm while it prints.

✅ **Pass:**
- readings are in **mm**, about 100 / 200 / 300, within ±20 mm;
- no `None` values once a reading has come in;
- no wild jumps while the box is still.

If readings are unstable at 20 cm, raise `--stop-mm` or tell me.

---

## T4 — The `follow_line` loop, without models (about 30 min)

Replace `40` below with your T2 timeout.

**(a) Plain run**

```bash
venv/bin/python tools/line_bench.py --host $PI run --timeout 40
```

✅ `RESULT: {'status': 'ok', 'arrived': True, ...}`

**(b) Obstacle, then cleared.** Same command. Mid-run, drop the box on the line about 15 cm in
front of the rover. After about 2 s, take it away.

✅ The log shows `OBSTACLE at ... mm -> STOP`, then `cleared -> CONTINUE`, then arrival.

**(c) Obstacle stays.** Same command, but leave the box there.

✅ About 5 s after the stop: `RESULT: {'status': 'blocked', 'reason': 'obstacle', ...}`, and the
rover stays still.

**(d) Halt — the safety gate**

```bash
venv/bin/python tools/line_bench.py --host $PI run --timeout 40 --halt-after 3
```

✅ **The wheels stop within about 0.5 s of `>>> HALT` and stay stopped.**
❌ If the rover keeps driving, stop here. This is the Day-1 exit condition. Also check that
Ctrl+C mid-run stops it.

**(e) Lost line.** Lift the rover off the floor (wheels spinning freely), then:

```bash
venv/bin/python tools/line_bench.py --host $PI run --timeout 8
```

✅ After about 8 s: `RESULT: {'status': 'blocked', 'reason': 'line_timeout'}`, and the wheels
stop.

---

## T5 — Look-left pause and resume (about 15 min)

First, find the left pan value while the rover sits still:

```bash
# try 1100; if the camera goes RIGHT instead, use 1900
venv/bin/python -c "
import roslibpy, time
r = roslibpy.Ros(host='$PI', port=9090); r.run()
t = roslibpy.Topic(r, '/ros_robot_controller/pwm_servo/set_state', 'ros_robot_controller_msgs/msg/SetPWMServoState'); t.advertise()
for p in (1100, 1500):
    t.publish(roslibpy.Message({'duration': 0.3, 'state': [{'id': [2], 'position': [p], 'offset': [0]}]})); time.sleep(1.5)
r.terminate()"
```

Then the real test. Set `--look-left-at` to about half your T2 run time:

```bash
venv/bin/python tools/line_bench.py --host $PI run --timeout 40 --look-left-at 8 --look-left-pan 1100
```

✅ **Pass:**
- the rover stops, the camera pans left, `left_look.jpg` is saved and shows the left-side object;
- the camera re-centres and the rover **picks the line up again**;
- it **still parks at the crossbar** with `arrived: True`.
- ❌ If it drives past the crossbar after the look, the arrival trigger was lost. Tell me.

➡️ Then set `LOOK_LEFT_PAN` to the value that worked.

---

## Panic

```bash
# Mac
venv/bin/python tools/line_bench.py --host $PI stop
# or Pi, Terminal B
ros2 service call /line_follow/cmd interfaces/srv/SetString "{data: 'STOP'}"
# last resort: Ctrl+C the line_follow node in Terminal A, or pick the rover up
```

---

## Results (fill in)

| Test | Pass? | Numbers / notes |
|---|---|---|
| T0 | | one mecanum? sonar node present? |
| T1a | | polarity OK? |
| T1b | | 5/5? stop distance past crossbar: |
| T2 | | runs (cm/s): __ / __ / __ → mean __ ; timeout __ s |
| T3 | | 10 cm → __ mm, 20 → __, 30 → __ |
| T4a | | elapsed __ s |
| T4b | | paused + resumed? |
| T4c | | blocked after __ s |
| T4d | | **wheels stopped?** |
| T4e | | timeout fired? |
| T5 | | resumed + arrived? LOOK_LEFT_PAN = __ |

**Day-1 done** when T4d passes and T5 arrives. Send me this table, and Day 2 (models and report)
builds on these numbers.
