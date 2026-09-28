# Spec — grounded line mission, end-to-end (2026-09-28)

**Owner:** Utroff · **Target:** done by end of Tue 29 Sep (2 working days)
**Hand to:** Claude Code, working in `Dashboard/brain/`.

**Permissions for this spec (overrides the CLAUDE.md default):** Utroff authorises edits to
`rover_pi.py`, `mission.py`, `vlm.py`, `planner.py`, `config.py`, `.env`, and a new `report.py`.
Still ask before `docker restart`/`stop` and before the first physical run of a session. The brain
venv runs Python 3.9, so use `Optional[...]`, not `X | None`.

---

## 0. Goal and locked decisions

**Goal:** one voice (or typed) command → plan → rover follows a taped line to an end marker →
keyframes are checked against the target while it drives → one stop for a look to the left →
arrival check → structured report with uncertainty flags. Each run leaves a log that can go
straight into Ch4/Ch5.

| Decision | Choice | Why |
|---|---|---|
| Steering | **IR 4-channel line sensor**, via Hiwonder's stock `example/line_follow` node | Keeps the camera free for the VLM. No heartbeat, unlike the camera `line_following` node |
| Distance and stopping | **Ultrasonic** (`/sonar_controller/get_distance`) plus an **end crossbar** on the tape | Sensors handle motion. The VLM never makes a distance or stop call |
| Keyframe checks | **Run while moving** and single-flight: if a check is still running, skip that tick | VLM latency is seconds. The checks are observations, not control |
| Look-left | **The only planned stop**: pause, pan left, grab a frame, re-centre, resume. The VLM call runs *after* resume | Line steering pauses cleanly (see §1). Inference doesn't hold up the motion |
| Grounding reference | **Text target description** from the plan step (e.g. "a red chair") | Walkthrough keyframe comparison is out of scope this week |
| Course | **One straight or gently curved black tape line, with a perpendicular tape crossbar at the end** | The crossbar is the arrival trigger the stock node already detects |

Out of scope this week: turns at junctions, multi-leg routes, gimbal sweeps, feeding the existing
`ingest_frame` revision loop on hardware, a dead-man switch on the Pi side, and RQ2 sweeps.

---

## 1. Facts verified in the source (read on 2026-09-28 — don't re-derive)

**Stock IR line follower:** `pi_transfers/ros2_ws_src/example/example/line_follow/line_follow.py`

- The node is named **`line_follow`**, so its services are `/line_follow/...`. The comment in
  `line_follow.launch.py` says `/line_follower` — that is wrong.
- **Don't use `line_follow.launch.py`.** It also includes `controller.launch.py`, which would start
  a second `mecanum` node alongside bringup's. Run the node alone:
  `ros2 run example line_follow` (entry point confirmed in `example/setup.py`).
- Services:
  - `~/set_running` (`std_srvs/SetBool`)
  - `~/cmd` (`interfaces/SetString`): `STOP`, `CONTINUE`, `STOP_NEXT_ROAD`, or a route string
    made of `C/R/L/S`
  - `~/set_mode` (`default|receive`)
  - `~/get_crossroads_count`, `~/reset_crossroads_count`, `~/u_turn`, `~/debug_turn`
- Topics: `~/crossroads_stop` (`std_msgs/Bool`, published **once** when it parks at a crossbar
  in receive mode) and `~/all_black` (`Bool`, every tick while running).
- There is **no heartbeat and no lost-line auto-exit**. When the line is lost (all four sensors
  read white), it keeps driving straight at `linear.x = 0.16`. Our code has to impose a timeout.
- Crossroad detection: the pattern `[1,1,1,0]`, `[0,1,1,1]` or `[1,1,1,1]` on 2 consecutive
  10 ms ticks, outside a 1 s cooldown.
- ⚠️ **In `default` mode it turns RIGHT at every crossroad.** Always send `cmd STOP_NEXT_ROAD`
  *before* `set_running true`. After it parks, the node resets itself to `default` mode.
- `cmd STOP` only sets `running=False`. It keeps `mode=receive` and `receive_cmd=stop_next_road`.
  `cmd CONTINUE` sets `running=True`. So **STOP → look left → CONTINUE keeps the arrival trigger
  armed.** Confirmed from the source.
- The follower's speed is fixed at `linear.x` 0.16–0.18. Real cm/s is unknown and gets measured
  in T2.
- While running, it publishes `/cmd_vel` at 100 Hz and logs `S0..S3` every tick (noisy but
  useful).
- Sensor meaning: this node treats `True` = black line. `example/line_follower.py` inverts the
  raw readings instead. **Check which is right on this unit in T1.**

**Ultrasonic:** `peripherals/peripherals/sonar_controller_node.py`

- Topic `/sonar_controller/get_distance`, `std_msgs/Int32`, in **millimetres**
  (`avoidance_node.py` divides by 10 to get cm).
- It publishes in a tight loop with no sleep. Subscribe with `throttle_rate=100` (ms) over
  rosbridge.
- The IR sensor is at I2C 0x78 and the sonar at 0x77, on the same bus 1. No conflict expected.

**Existing brain code:**

- `rover_pi.PiRoverController.halt()` only publishes a zero `Twist`. **The line follower overrides
  that within 10 ms.** Halt must also call `/line_follow/cmd STOP` (see §3B). This is the most
  important safety fix in this spec.
- The snapshot path already works: `GET http://<pi>:8080/snapshot?topic=/image_raw` (used by
  `main.camera_snapshot`).
- Gimbal: `main.py` maps "left" to a pan delta of `-GIMBAL_STEP`. Keep the same sign convention,
  and make the left pan position a config value you set by eye.
- `mission.ingest_frame` is fed only by `/ws/execution` frames. No Pi forwarder exists, so it
  stays idle on `ROVER=pi`. Leave it that way this week.
- CLAUDE.md quotes about 15 s per frame for the local VLM. The Titan X figure isn't measured yet.
  At about 17 cm/s, 15 s is about 2.5 m, so expect **1–3 checks per run** on a slow host. That
  is a real RQ1 finding, not a bug — log it.

---

## 2. Course setup (physical)

- Floor tape: black, 2–3 cm wide, on a light floor. Length **2.5–4 m** — long enough for 2+
  checks and short enough that a runaway ends quickly.
- End marker: a perpendicular crossbar about 20 cm wide (wider than the sensor array), so all four
  sensors read black.
- Target: place the target object (e.g. a chair or a coloured box) **about 40–80 cm past the
  crossbar**, in camera view at the default tilt.
- Look-left subject: place one distinctive object to the left of the line near its midpoint, so
  the look-left frame has a verifiable answer.
- Obstacle test: have a box ready to drop onto the line.

---

## 3. Changes

### A. Pi side (no code changes)

After the normal bring-up sequence (restart container → `bringup.launch.py`), start the node in a
second shell:

```bash
docker exec -it -u ubuntu -w /home/ubuntu turbopi /bin/zsh -c \
  "source ~/.zshrc && ros2 run example line_follow"
```

If `ros2 pkg executables example` doesn't list `line_follow`, the workspace needs a
`colcon build --packages-select example`. Ask before doing this.

### B. `rover_pi.py`

Add to `PiRoverController` (all over the existing roslibpy client):

1. **Handles**
   - `roslibpy.Service` for `/line_follow/cmd` (type `interfaces/srv/SetString`) and
     `/line_follow/set_running` (`std_srvs/srv/SetBool`).
   - `roslibpy.Topic` subscriptions:
     - `/sonar_controller/get_distance` (`std_msgs/msg/Int32`, `throttle_rate=100`,
       `queue_length=1`) → stores `self._sonar_mm`. Ignore readings `<= 0` or `> 4000`.
     - `/line_follow/crossroads_stop` (`std_msgs/msg/Bool`) → sets `self._arrived` (a
       `threading.Event`).
   - `threading.Event`s `_pause_req` and `_resume_req`, plus a small lock-protected `telemetry`
     dict: `{"following": bool, "paused": bool, "sonar_mm": int|None, "t_start": float|None,
     "obstacle_events": int}`.

2. **`_lf(cmd: str)`** — call `/line_follow/cmd` with a callback, so it doesn't block. Log the
   reply. **`_lf_run(flag: bool)`** — the same for `set_running`.

3. **New action `follow_line`** in `execute_step`:
   ```
   _arrived.clear(); _lf("STOP_NEXT_ROAD"); _lf_run(True); telemetry.following=True, t_start=now
   loop at 20 Hz:
     if _halted            -> _lf("STOP"); zero twist; return halted
     if _arrived           -> return ok, detail {"arrived": True, "elapsed_s": ...}
     if _pause_req         -> _lf("STOP"); paused=True; wait _resume_req (max 10 s); _lf("CONTINUE"); paused=False
     if sonar_mm < SONAR_STOP_MM on 2 consecutive NEW readings:
         _lf("STOP"); obstacle_events += 1
         wait up to OBSTACLE_WAIT_S for sonar_mm > SONAR_CLEAR_MM -> _lf("CONTINUE")
         else -> return blocked, detail {"reason": "obstacle", "sonar_mm": ...}
     if elapsed > LINE_TIMEOUT_S -> _lf("STOP"); return blocked, detail {"reason": "line_timeout"}
   finally: telemetry.following=False
   ```
   The line timeout covers the case where the line is lost and the follower drives straight on
   blindly. Set it to `1.5 × LINE_LENGTH_CM / LINE_SPEED_CMPS`.

4. **`pause()` / `resume()`** — set `_pause_req` and `_resume_req`. They only work while
   `follow_line` is running. `pause()` blocks until `telemetry.paused` is True (max 1 s).

5. **`get_frame() -> Optional[bytes]`** — `httpx.get(snapshot_url, timeout=2.0)`. Return `None`
   on error; never raise.

6. **`look(pan_position: int, settle_s: float = 0.6) -> Optional[bytes]`** — set pan → sleep
   `settle_s` → `get_frame()` → re-centre pan. Returns the frame.

7. **`halt()`** — `_halted.set()`, `_lf("STOP")` (non-blocking callback), then the zero twist.
   It still does no inference and no blocking round-trip. Update the docstring.

8. **`observe` on the Pi** — centre the gimbal and hold 0.5 s, with no sweep. `scan` keeps the
   placeholder sweep.

### C. `vlm.py` — two new functions (reuse `_ask`)

```python
def check_progress(image, target: str) -> dict:
    """Mid-route keyframe check. Observation only; never a control signal."""
    prompt = (
        "You are the camera check for a small rover driving along a floor line toward: "
        f'"{target}".\n'
        "Answer only from what is visible in this image.\n"
        "Respond ONLY with JSON: "
        '{"target_visible": true/false, "path_clear": true/false, '
        '"description": "one short sentence"}'
    )
    return _ask("check_progress", prompt, image)

def check_arrival(image, target: str, expected: list) -> dict:
    """Arrival check for the report. `expected` = other things the command or scene says should be here."""
    prompt = (
        f'A rover has stopped where it should be able to see: "{target}".\n'
        f"Other things that may be nearby: {json.dumps(expected)}\n"
        "Answer only from what is visible in this image. Do not assume.\n"
        "Respond ONLY with JSON: "
        '{"target_visible": true/false, "confidence": "high"|"medium"|"low", '
        '"seen": ["things from the lists that ARE visible"], '
        '"missing": ["things from the lists that are NOT visible"], '
        '"description": "one or two sentences"}'
    )
    return _ask("check_arrival", prompt, image)

def check_side_look(image, target: str) -> dict:
    prompt = (
        "This frame was taken with the rover's camera turned LEFT, off its direction of travel.\n"
        f'The rover is heading toward: "{target}".\n'
        "Respond ONLY with JSON: "
        '{"objects": ["..."], "target_visible": true/false, "description": "one sentence"}'
    )
    return _ask("check_side_look", prompt, image)
```

Add all three to `__all__`. A failed call returns `{"_error": ...}`. Callers must record that
as "check failed", **never** as "target not visible".

### D. `planner.py`

- Add to `ACTIONS`:
  `"follow_line": "follow the floor line to its end marker — the only way to travel more than a short distance"`.
- Add one sentence after `_VOCABULARY`: *"To go somewhere, use one follow_line step whose target
  is what should be visible at the end, then observe that target, then report."*
- Leave `revise_plan` untouched.

### E. `mission.py`

1. **`_frames_dir(session_id)`** → `logs/frames/<session_id>/`. Save every frame that goes to
   the VLM as `kf_<nn>.jpg`, `left_<nn>.jpg` or `arrival.jpg`.

2. **`keyframe_monitor(mission, rover, step, emit)`** — a coroutine. `run_mission` starts it
   with `asyncio.create_task` just before calling `execute_step` for a `follow_line` step, **only
   if** `hasattr(rover, "get_frame")`, and cancels it right after the step returns.
   - `interval_s = KEYFRAME_SPACING_CM / LINE_SPEED_CMPS` (floor 1.0 s).
   - `look_left_at_s = LOOK_LEFT_AT_FRACTION × LINE_LENGTH_CM / LINE_SPEED_CMPS`, done once.
   - Each tick:
     - if a check is still in flight → append `{"skipped": "vlm_busy", t}` and continue;
     - otherwise `frame = await to_thread(rover.get_frame)` and launch
       `check_progress(frame, step["target"])` as a task.
   - At `look_left_at_s`:
     - `await to_thread(rover.pause)`;
     - `left = await to_thread(rover.look, LOOK_LEFT_PAN)`;
     - `await to_thread(rover.resume)`;
     - then run `check_side_look(left, target)` **after** resuming.
   - Each result is appended to `mission.checks` (new list on `MissionSession`) as
     `{kind, t_rel_s, est_distance_cm = t_rel_s × LINE_SPEED_CMPS, latency_s, frame_path, result}`
     and emitted as `{"type": "check", ...}` for the dashboard.
   - **Policy this week:** checks are observation only. If `path_clear` is false on 2
     consecutive completed checks, emit `{"type": "warning"}` and log it. **Don't halt** — the
     sonar is the only authority on distance and stopping.

3. **Arrival check** — after a successful `observe` step, or straight after a `follow_line` step
   that returned `arrived` if the plan has no `observe` step:
   - `frame = rover.get_frame()`;
   - call `check_arrival(frame, target, expected)`, where `expected` = target words from the
     command plus scene-digest objects, if a scene is loaded;
   - store the result on `mission.arrival`.

4. **After the loop** (success, blocked or halted — *always*): `report = await
   to_thread(report_mod.build_report, mission)` → emit `{"type": "report", "report": report}` →
   speak `report["spoken_summary"]` → write `logs/mission_<session_id>.json`.

### F. `report.py` (new)

`build_report(mission) -> dict`:

- **Deterministic block** (no model):
  - command, resolved command, plan, per-step status and detail;
  - `checks[]`, `look_left`, `arrival`;
  - `sonar_obstacle_events`;
  - timings: total, per-step, VLM latencies (mean/max), checks completed vs skipped vs failed;
  - provider/model names for the planner and VLM (so a cloud run can never pass as local);
  - `rover=pi`.
- **`uncertainties[]`** — rule-based, no model. This is the citable "flag residual uncertainty
  in the report" moment. Flag when:
  - the arrival `target_visible` is false, or confidence is `low`;
  - an arrival or progress check failed (`_error`);
  - the target was visible mid-route but not at arrival, or the reverse;
  - `path_clear` was false in any check;
  - the mission ended `blocked` (with the reason) or `halted`;
  - fewer than 2 progress checks completed (thin evidence);
  - `missing` is non-empty.
- **`spoken_summary`** — one planner call: *"Write at most 3 short sentences for a spoken
  report. Use ONLY facts in this JSON. If uncertainties is non-empty, say the most important
  one."* On failure, fall back to the template `"Mission {outcome}. Target {seen/not seen}.
  {n} uncertainties flagged."`.

### G. Config (`.env` plus `config.py` getters)

```
ROVER_PI_SONAR_STOP_MM=200
ROVER_PI_SONAR_CLEAR_MM=300
ROVER_PI_OBSTACLE_WAIT_S=5
ROVER_PI_LINE_TIMEOUT_S=40        # recompute after T2
LINE_LENGTH_CM=300                # measure the course
LINE_SPEED_CMPS=17                # placeholder — set from T2
KEYFRAME_SPACING_CM=40
LOOK_LEFT_AT_FRACTION=0.5
LOOK_LEFT_PAN=1100                # set by eye in T5
```

---

## 4. Two-day test ladder (each rung is go/no-go — don't skip ahead)

### Day 1 — Mon 28 Sep: instrument and `rover_pi`, no models

| # | Test | Pass when |
|---|---|---|
| T0 | Bring-up, then `ros2 run example line_follow`; `ros2 service list \| grep line_follow` | All services listed; no second `mecanum` node |
| T1 | From a container shell: `cmd STOP_NEXT_ROAD` → `set_running true`. Rover on the tape | Follows the line and parks on the crossbar 5/5 runs; `crossroads_stop` echoes `true`. Note the S0..S3 polarity |
| T2 | Time 3 runs over a marked 100 cm stretch | `LINE_SPEED_CMPS` set (mean); `LINE_TIMEOUT_S` recomputed |
| T3 | `ros2 topic echo /sonar_controller/get_distance` with a box at 10/20/30 cm | Readings are in mm and within ±2 cm; no 0 values or spikes at those distances |
| T4 | `ROVER=pi`, call `follow_line` from a 10-line script (no models): (a) plain run (b) drop the box mid-run, then remove it within 5 s (c) leave the box (d) call `halt()` mid-run (e) lift the rover off the line | (a) ok/arrived (b) pauses then resumes (c) blocked/obstacle (d) **wheels stop within 0.5 s and stay stopped** (e) blocked/line_timeout |
| T5 | `pause()` → `look(LOOK_LEFT_PAN)` → `resume()` mid-line | Picks the line up again and still parks at the crossbar (arrival trigger still armed) |

**Day 1 exit:** T4d passes. If halt doesn't stop the wheels, stop and fix before doing anything
with models.

### Day 2 — Tue 29 Sep: models, report, full runs

| # | Test | Pass when |
|---|---|---|
| T6 | Run `check_progress`, `check_side_look` and `check_arrival` offline on 3 saved frames from T4/T5 | Valid JSON; latency logged for the host used (Mac vs desktop) |
| T7 | Typed command from the dashboard, e.g. *"Follow the line to the red box and tell me if it's there"*, 3 runs | Plan contains `follow_line`; ≥1 progress check completes; look-left frame saved; report JSON written and spoken |
| T8 | Voice command, 3 runs: (a) normal (b) **target removed** (c) obstacle dropped and left | (a) report says target seen (b) report flags target missing, not a false "seen" (c) mission blocked; report explains why |
| T9 | Copy the 6 mission JSONs and frames to `Progress/runs-2026-09-29/`; append a results table to the status doc | Table: run, outcome, checks done/skipped, mean VLM latency, arrival verdict vs ground truth |

**Day 2 exit (definition of done):** T8a and T8b both pass on the **local** VLM. T8b is the one
that matters for the report — it shows the system saying "I couldn't see it" rather than
hallucinating.

---

## 5. Known risks (accepted for this week)

- **No dead-man switch.** If the brain or rosbridge dies mid-run, the follower keeps going until
  the crossbar, or drives straight on if it loses the line. Mitigations: a short course that ends
  at a crossbar, a hand near the rover, and the T4d halt test. A Pi-side watchdog is future work.
- **VLM checks are sparse on the local host.** On the slow host, expect 1–3 checks per run.
  Report this as a measured latency and density figure, not a failure.
- **The target has to be in frame at arrival** at the default tilt. Set its position in §2
  before T7, not during.
- **I2C bus shared by sonar and IR.** Watch for `OSError` in the node logs during T1 and T3.
