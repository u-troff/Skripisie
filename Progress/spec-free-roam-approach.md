# Spec — free-roam "approach" mission, no tape (drafted 2026-09-29)

**Owner:** Utroff · **Build window:** only **after T8 of the line mission passes** — about 3 working days.
**Hand to:** Claude Code. Builds on `Progress/spec-grounded-line-mission.md` (the sonar subscription,
`get_frame`, gimbal inversion, `check_arrival` and `report.py` are reused, not rebuilt).

**Permissions** are the same as the line spec: edits to `rover_pi.py`, `mission.py`, `vlm.py`,
`planner.py`, `config.py`, `.env` and `report.py`. Ask before the first physical run of a session.
The venv is Python 3.9.



- **Amendment (2026-10-01):** one narrow exception. Every tick now scans the full
  centre/left/right cycle (not one direction per tick — `KEYFRAME_SPACING_CM` moved to
  100 cm), and if any of those frames or the look-left frame reports `target_visible: true`
  for the step's own named target, the rover halts immediately (`rover.confirm_target`) and
  the step is recorded as arrived. This is unrelated to `path_clear`/obstacle judgement, which
  still never halts — sonar and the crossbar remain the only authority there.



## 0. Goal and locked decisions

**Goal:** the rover starts at the doorway. From a voice command like *"go to the chair"*, it searches
for the target, turns towards it, and closes in with a stop-and-look cycle. Sonar is the safety layer
throughout. It ends with an arrival check and the same structured report as the line mission. This is
the real-hardware version of `VirtualRover`'s `approach`, and it closes the known gap where
`approach` is the same as `move` in `rover_pi.py`.

| Decision        | Choice                                                                                                                                                                                                |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Target          | A**visible object** named in the plan (`approach` step target). No room map or node table.                                                                                                    |
| Obstacle        | **Stop and report.** No sidestep and no avoidance. A sonar stop that isn't the target → `blocked`.                                                                                           |
| Start           | The target**may not be visible** from the doorway, so there is a **search phase** (pivot in place and look).                                                                              |
| Control split   | **Sonar** = fast reactive stop, 20 Hz, during every movement. **VLM** = slow decisions, and **only while the rover is stationary**. The rover never moves while waiting on a model. |
| Evaluation role | A second experiment next to the line mission:**same planner, instrumented navigation versus free navigation.** Every failure gets a cause (§5).                                                |

**Out of scope:** avoiding obstacles, mapping or SLAM, place-based targets ("the corner"), moving
targets, multiple rooms, and doorways narrower than about 2× the rover's width.

---

## 1. Physical limits the design accepts (stated in the report too)

- **No odometry.** Mecanum wheels slip, so a hop of "30 cm" is a **timed** hop, calibrated once
  (F1). Every hop is corrected by the next look, so errors don't build up.
- **The sonar only sees hard surfaces at about its own height, roughly face-on.** It misses cables,
  chair legs and low steps, and soft targets (bed, couch, curtains) may never read as close.
  **Arrival is therefore decided by vision first**, with sonar as confirmation only for hard targets.
- **VLM latency is 2–15 s per call, depending on the host.** A 3 m approach at about 30 cm per hop is
  roughly 10 cycles, so expect **1–3 minutes per mission**. Log it, and report it as a measured
  RQ1 cost.

---

## 2. Control loop (lives in `mission.py`, because it calls models; the rover only gets primitives)

```
approach(target):
  SEARCH  (skip if the first look already sees the target)
    for k in 0..SEARCH_STEPS-1 (default 8 × ~45° = 360°):
        frame → locate_target(frame, target)
        if visible: break
        pivot(SEARCH_DIR, SEARCH_PIVOT_S)       # ~45°, calibrated in F1
    if not found → status=blocked, reason=target_not_found

  APPROACH  (max APPROACH_MAX_CYCLES = 20, max APPROACH_MAX_S = 240 s)
    loop:
      frame → loc = locate_target(frame, target)          # stationary
      if not loc.visible:
          lost += 1; if lost >= 2 → back to SEARCH (once only), else hop back HOP_BACK_S and retry
          continue
      lost = 0
      if arrived(loc, sonar): → ARRIVED
      e = loc.x_center - 0.5                               # -0.5 .. +0.5, right = +
      if |e| > CENTER_TOL (0.12):
          pivot(sign(e), PIVOT_S_PER_UNIT × |e|)           # proportional turn, then fall through
      hop_cm = HOP_NEAR_CM if loc.fill > NEAR_FILL else HOP_CM
      r = hop(hop_cm)                                      # sonar watchdog inside
      if r.sonar_stop:
          if loc.fill > NEAR_FILL and |e| <= CENTER_TOL → ARRIVED (reason=sonar)
          else → status=blocked, reason=obstacle, sonar_mm=r.sonar_mm
  ARRIVED → check_arrival(frame, target, expected)  (reused from the line spec) → report
```

**`arrived()`** returns true if `loc.fill >= ARRIVE_FILL` (bbox area / frame area, default 0.20), or
`loc.bottom >= ARRIVE_BOTTOM` (bbox bottom edge in the lowest 10% of the frame, meaning the object
reaches the floor right in front of the rover), or `sonar_mm <= ARRIVE_MM` (250) **while** the target
is centred.

**Direction conventions:** `x_center` is measured in the image, and the image is upright
(verified 2026-09-29). Positive `e` means the target is right of centre, so **pivot right**. Base
pivots are independent of the gimbal inversion in line-spec §8; keep the gimbal **centred** for the
whole of `approach`.

---

## 3. Changes

### A. `rover_pi.py` — primitives only, no models

- **`pivot(direction: int, seconds: float) -> dict`:** a timed `Twist` with `angular.z = ±FREE_TURN_Z`,
  using `_timed_twist` so halt works. Direction +1 = right (clockwise) = **negative** `angular.z`.
  Confirm in F1.
- **`hop(cm: float, backwards: bool = False) -> dict`:** timed forward `Twist` at `FREE_SPEED_X`, with
  seconds = `cm / FREE_SPEED_CMPS`. It runs the **sonar watchdog at 20 Hz**: 2 consecutive new
  readings below `ROVER_PI_SONAR_STOP_MM` → zero `Twist` immediately, and return
  `{"status": "sonar_stop", "sonar_mm": ..., "moved_s": ...}`. Otherwise it returns `{"status": "ok"}`.
  Backwards hops skip the sonar, which faces forward.
- **Before any `pivot` or `hop`,** send `/line_follow/cmd STOP`, so the follower node can never fight
  the brain for `/cmd_vel`.
- **Reuse** from the line spec: the sonar subscription, `get_frame()`, the gimbal helper with
  inversion, and `halt()` (double STOP).
- **`execute_step` for `approach`** keeps a blocking fallback (a single `hop`) for callers without
  the controller. The real behaviour lives in `mission.py` (below).

### B. `vlm.py` — `locate_target(image, target) -> dict`

Qwen2.5-VL is trained to output bounding boxes as JSON, so use that and derive the geometry in
Python. Don't ask the model for fractions.

```python
def locate_target(image, target: str) -> dict:
    prompt = (
        f'Find "{target}" in this image from a small floor robot\'s camera.\n'
        "If it is visible, give its bounding box in pixel coordinates.\n"
        "Respond ONLY with JSON: "
        '{"visible": true/false, "bbox_2d": [x1, y1, x2, y2] or null, '
        '"confidence": "high"|"medium"|"low", "description": "one short sentence"}'
    )
    return _ask("locate_target", prompt, image)
```

The caller derives `x_center`, `fill` and `bottom` from `bbox_2d` and the real frame size
(`frames.analyse` or PIL). It **validates** the box: coordinates inside the frame, x1 < x2,
y1 < y2. An invalid box, or `_error`, counts as **"not visible, check failed"**, logged as such and
never read as a confident miss. A `low`-confidence detection only counts after 2 consecutive looks
agree.

### C. `planner.py`

- Keep the `approach` action. Make its text more specific: *"find a visible object and drive up to
  it (searches by turning if it is not in view)"*.
- Add a hint: *"To go to an object without a floor line, use approach with the object as target,
  then observe it, then report."* While the line track is in use, the line-mission hint stays. Pick
  which hint to include with `NAV_MODE=line|free` in `.env`, so the same command runs both
  experiments.

### D. `mission.py`

- When `ROVER=pi`, `NAV_MODE=free` and the step is `approach`, run
  `await approach_controller(mission, rover, step, emit)` (the §2 loop) instead of
  `execute_step`.
- Emit a `{"type": "approach_cycle", ...}` event per cycle to the dashboard.
- Append one trace record per cycle to `mission.checks`, with these fields:
  - `cycle`, `phase` (search/approach), `t_rel_s`;
  - `bbox`, `x_center`, `fill`, `bottom`;
  - `sonar_mm`, `action` (`pivot L 0.4 s` / `hop 30` / `arrived` / `blocked`);
  - `vlm_latency_s`, `frame_path`.
- Frames go to `logs/frames/<session_id>/ap_<nn>.jpg`.

### E. `report.py` — additions

- The deterministic block gets: cycles used, search steps used, total VLM time versus total motion
  time, sonar stops, and the arrival reason (`fill` / `bottom` / `sonar`).
- New uncertainty rules:
  - arrival reached **only** via `bottom` or `fill`, with a soft-target warning ("sonar did not
    confirm");
  - target lost ≥ 2 times;
  - more than 50% of the cycle budget used;
  - any `low`-confidence detection used in steering.

### F. Config (`.env`)

```dotenv
NAV_MODE=free                 # free | line
FREE_SPEED_X=0.25             # Twist linear.x for hops
FREE_SPEED_CMPS=15            # measured in F1
FREE_TURN_Z=3.0               # Twist angular.z for pivots
FREE_TURN_DEG_PER_S=90        # measured in F1
HOP_CM=30
HOP_NEAR_CM=15
HOP_BACK_S=0.6
NEAR_FILL=0.08
ARRIVE_FILL=0.20
ARRIVE_BOTTOM=0.90
ARRIVE_MM=250
CENTER_TOL=0.12
CAMERA_HFOV_DEG=60            # measured in F2; PIVOT_S_PER_UNIT = HFOV / DEG_PER_S
SEARCH_STEPS=8
SEARCH_PIVOT_S=0.5            # ≈ 45° at the measured deg/s
APPROACH_MAX_CYCLES=20
APPROACH_MAX_S=240
```

---

## 4. Test ladder (go/no-go at each rung)

| #            | Test                                                                                                                                                                   | Pass when                                                                                                                                        |
| ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| F1           | Calibrate the primitives (no models): 5 ×`hop(100 cm)`, then measure the distance. 5 × `pivot` for 1 s, then measure the angle                                   | Mean and spread recorded;`FREE_SPEED_CMPS` and `FREE_TURN_DEG_PER_S` set; pivot direction sign confirmed                                     |
| F2           | Camera FOV: put an object at the left and right frame edges at 1 m, and measure the width it spans                                                                     | `CAMERA_HFOV_DEG` set                                                                                                                          |
| **F3** | **Offline localisation (the kill test):** 12 frames of the target at 3 m / 2 m / 1 m / 0.5 m × left / centre / right. Run `locate_target` on each             | **≥ 10/12 correct side (L/C/R)** and a plausible box; latency logged per host. **If it scores < 8/12, stop free roam** (see §6)    |
| F4           | Sonar stop during a hop: box dropped in the path                                                                                                                       | Stops within about 5 cm of the threshold; returns`sonar_stop`                                                                                  |
| F5           | Controller with the target in view, 1.5 m straight ahead, 3 runs                                                                                                       | Arrives; the report is written; cycles ≤ 8                                                                                                      |
| F6           | Full voice mission from the doorway, 5 runs: target (a) in view (b) behind the rover (search) (c) removed (d) hard obstacle in the path (e) soft target (bed or couch) | (a,b) arrive (c)`target_not_found` reported honestly (d) `blocked: obstacle` (e) arrives on vision; the report flags "sonar did not confirm" |
| F7           | Log all runs and ground truth into the results table next to the line runs                                                                                             | A comparison table exists: success rate, time, VLM calls, failure causes                                                                         |

---

## 5. Failure taxonomy (logged per run — this is what makes free roam valid evidence)

For each run, record the **ground truth by hand** (did it actually reach the target?) and
**one cause:**

| Code               | Meaning                                                | Whose fault                               |
| ------------------ | ------------------------------------------------------ | ----------------------------------------- |
| `PLAN`           | Wrong or hallucinated target, or unexecutable steps    | Planner (RQ1/RQ2)                         |
| `PERCEIVE_MISS`  | Target visible to a human but not found or lost        | VLM                                       |
| `PERCEIVE_FALSE` | Claimed "arrived" or "visible" when it wasn't          | VLM (the dangerous one)                   |
| `DRIVE`          | Timed out or oscillated with the target correctly seen | Navigation (instrumentation)              |
| `BLOCKED`        | Sonar stop on a real obstacle                          | Environment (expected, correct behaviour) |
| `OK`             | Arrived, and the report matches the ground truth       | —                                        |

Planning failures stay separable from driving failures, which is the requirement CLAUDE.md sets for
any navigation mode.

---

## 6. Kill and fallback criteria

- **F3 scores < 8/12:** the local VLM can't localise reliably. Stop building. Record it as a result
  ("a 3B local VLM cannot close a visual-servo loop on this camera"), keep the line mission as the
  demo, and move the time to the report. Optional side test: try a cloud VLM benchmark on the same
  12 frames, clearly labelled as a benchmark, not the system under test.
- **F5 hasn't passed by the end of build day 2:** freeze free roam at whatever works, log what
  exists, and go back to the write-up. The line mission is the demo either way.

## 7. Schedule (after T8)

- **Build day 1:** F1, F2 (no models); `rover_pi` primitives; `locate_target`; **F3 offline** → go/kill decision.
- **Build day 2:** controller in `mission.py`, planner hint, report additions; F4, F5.
- **Build day 3:** F6 (5 voice runs), F7 table. Freeze.
