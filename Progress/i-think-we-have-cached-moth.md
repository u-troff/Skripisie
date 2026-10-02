# Gimbal-first approach search + false-arrival fix

## Context

The free-roam `approach` loop in `mission.py` searches for its target by **pivoting the whole chassis** in 8 steps, calling the VLM at each. On real hardware this fails badly, and the run logs say exactly why:

- `mission_785c51857399.json` ("go to the chair in view 2"): 5 pivots × 59° (at the measured 118°/s) = **295° of spinning**, `motion_time_s: 0` — it never drove forward once — then declared `status: ok, reason: "fill"` with `fill=0.992`. I viewed that frame: it is a **blank blurry purple wall**, no chair. `check_arrival` immediately contradicted it (`target_not_seen`), yet the mission still reported `outcome: completed`.
- `mission_8ea14fa12a60.json`: 8 pivots = **472°**, more than a full circle, target never found, zero forward motion.

Viewing the saved frames identified three root causes:

1. **The camera is aimed at the floor.** At gimbal-centre tilt every frame is ~75% floorboards (`ap_00_search.jpg`); furniture at seat height is simply out of frame.
2. **Frames are grabbed mid-rock and motion-blurred.** `pivot()` returns the instant the Twist is zeroed, with no settle before `get_frame()`; `ap_02_search.jpg` is an unreadable smear. `look()` computes `frames.analyse()` but never reads `.sharpness`.
3. **The VLM hallucinates a frame-filling box when it can't find the target** — signature `x_center ≈ 0.499`, `fill` 0.43–0.99 across both runs. `_arrived()` has no upper bound on `fill`, and arrival can fire on the very first approach iteration with no intervening motion.

Verified during planning:
- `ap_04_search.jpg` and `ap_05_arrived.jpg` are **byte-identical** (same MD5) — the "arrival" was decided on the search frame itself, no new observation.
- `_derive_loc` validates `x1 >= 0` but **not** `y1 >= 0` (`mission.py:705`).
- `_arrival_check` calls `center_gimbal()` (`mission.py:614`), which zeroes **tilt as well as pan** — it would undo any approach tilt on the exact frame that adjudicates arrival.
- The low-confidence "2 looks agree" gate is a single module-scope flag, so it would compare detections taken at different bearings once a sweep exists.

**Outcome wanted:** the camera finds its bearings by panning (cheap, chassis stationary); the chassis only turns once it knows where to turn; and a hallucinated box is *counted and reported* rather than believed.

## Decisions taken

- Camera tilts up to an `APPROACH_TILT_OFFSET` measured by hand (new rung F8a).
- Pan degrees-per-PWM-unit measured once (new rung F8b) so a pan offset converts deterministically to a chassis angle.
- The false-arrival bug is fixed in this same change.

---

## A. `rover_pi.py` — one new primitive

Add **`set_gimbal(pan: Optional[int] = None, tilt: Optional[int] = None, settle_s: Optional[float] = None, duration: float = 0.35) -> dict`**, placed directly above `nudge_gimbal` (~line 821) as its absolute counterpart.

- `pan`/`tilt` are **logical** offsets (negative pan = left, positive tilt = up), `None` = leave that axis alone — the sweep must not disturb tilt.
- Clamp through the existing `_offset_limit()`, apply via `_apply_gimbal(duration)`, then sleep `settle_s` (default `self.aim_settle_s`) inside the method.
- Return `_apply_gimbal`'s dict so callers log the **post-clamp** offsets.

Then refactor the two existing inline `self._pan_offset = N; self._apply_gimbal()` sites to call it: `aim()` (~590) and `_gimbal_sweep()` (~770). `aim()` keeps its `Optional[str]` direction-string return and all callers are unchanged.

**Leave `look()` completely alone** — it is the documented §8 raw-PWM exception the line mission depends on.

## B. `mission.py` — gimbal-first state machine

**`_sweep_offsets() -> List[int]`** — generates pan offsets for one sweep from the measured constants, ordered **centre-outward** (`[0, -u1, +u1, …]`) so the common "target ahead" case costs one VLM call. If `GIMBAL_DEG_PER_UNIT <= 0` (unmeasured), return `[0]` and log `gimbal_deg_per_unit_unmeasured` — never fabricate bearings from an unmeasured constant.

**`_grab_sharp(rover)`** — settle, grab, `frames.analyse()`, retry up to `APPROACH_FRAME_RETRIES` if `sharpness < APPROACH_MIN_SHARPNESS`. On failure the VLM is **never called**; the frame is still saved and recorded as `kind: "skipped"`, matching the existing `_skip` convention (~line 399) so `report.timings.checks_skipped` counts it for free. Sleep `APPROACH_SETTLE_S` after every chassis pivot/hop before calling it (the gimbal's own settle lives inside `set_gimbal`).

**`_plausible(loc, prev_fill, hops_done) -> Optional[str]`** — pure function (unit-testable, runnable offline over `tools/f3_frames/`), returns a reject reason or `None`:

| Rule | Reason |
|---|---|
| `fill > MAX_FILL` (0.60) | `degenerate_fill` |
| `w_frac` or `h_frac` > 0.95 | `degenerate_full_frame` |
| `fill < MIN_FILL` (~18×18 px) | `degenerate_tiny` |
| `prev_fill < NEAR_FILL` and `fill > prev_fill * FILL_JUMP_MAX` | `fill_jump` (a 30 cm hop from ≥1 m can grow fill at most ~2×; gated to the far field only, since inside 0.7 m a 3× jump is real) |
| `x_center` within 0.003 of 0.5 **and** `fill > 0.3` | `degenerate_centre_box` (the literal observed signature; separate counter because it is the most quotable RQ2 evidence) |

Low-confidence handling moves here, **keyed by pan offset**: a low-confidence hit at offset `P` triggers a second look at the *same* `P`, accepted only if also plausible with `|Δx_center| ≤ 0.15`. Replaces the contaminated module-scope flag.

**State machine** in `approach_controller`:

```
set_gimbal(tilt=APPROACH_TILT_OFFSET, pan=0)
SEARCH:
  for sweep in range(SEARCH_MAX_SWEEPS):
      for P in _sweep_offsets():            # chassis STATIONARY
          set_gimbal(pan=P); frame = _grab_sharp(); locate_target
          if visible and _plausible() is None:
              bearing = P*GIMBAL_DEG_PER_UNIT + (x_center-0.5)*CAMERA_HFOV_DEG
              goto LOCK
      set_gimbal(pan=0); pivot(SEARCH_DIR, SWEEP_ADVANCE_DEG/FREE_TURN_DEG_PER_S)   # ONE big pivot
  -> blocked, target_not_found
LOCK:
  set_gimbal(pan=0)                          # re-centre BEFORE pivoting
  if abs(bearing) >= MIN_PIVOT_DEG:
      pivot(sign(bearing), max(MIN_PIVOT_S, abs(bearing)/FREE_TURN_DEG_PER_S))
  sleep(APPROACH_SETTLE_S); verify = fresh sharp+plausible centred look
  if abs(verify.residual_e) > RESIDUAL_FLAG_E: flag bearing_residual_high
APPROACH: unchanged hop/centre logic, arrival via _arrival_ok() only
finally: center_gimbal()
```

Structural property that kills the `785c…` failure outright: **`_arrived()` is no longer reachable from a search frame** — every path into the approach loop goes through LOCK's fresh, settled, centred, plausibility-gated verify look.

`SEARCH_STEPS`/`SEARCH_PIVOT_S` become dead. Expected cost: 9–10 VLM calls and ≤232° of chassis spin worst case (vs. 472° today), **0°** in the common target-ahead case.

## C. Arrival hardening

**`_arrival_ok(...)`** replaces `_arrived()` (~line 683):

1. **Sonar** (`sonar_mm <= ARRIVE_MM` and centred) → `"sonar"`. Exempt from the hop requirement (a range reading is a physical measurement, not a model opinion), but flagged `arrival_without_motion` when `hops_done == 0`.
2. **Vision** (`fill >= ARRIVE_FILL` or `bottom >= ARRIVE_BOTTOM`) additionally requires `hops_done >= MIN_HOPS_BEFORE_ARRIVAL`, having passed `_plausible`, and a **non-shrinking** `fill` across two accepted sightings. (Non-shrinking, not a strict growth multiplier — `fill` saturates at close range and a strict demand would deadlock into the cycle budget.)
3. **Cross-check** against the existing `check_arrival` result: `target_visible is False` with no `_error` → hop back, re-look once, and if still absent return `blocked / arrival_contradicted` so `outcome` is **not** `completed`. A check that `_error`ed is recorded as `"failed"`, never read as contradiction (`report.py`'s founding principle). `APPROACH_ARRIVAL_CROSSCHECK` selects strict vs. lenient without a code edit.

**Required supporting change:** give `_arrival_check` a `keep_tilt: Optional[int] = None` parameter; when set, call `set_gimbal(pan=0, tilt=keep_tilt)` instead of `center_gimbal()`. Default `None` keeps line-mission behaviour byte-identical. Without this the cross-check adjudicates arrival on a floor-staring frame.

Also fix `_derive_loc`: add the missing `y1 >= 0`, return `w_frac`/`h_frac`, and carry `vlm_said_visible` through so the trace separates "the model said no" from "we rejected what the model said".

Add `"set_gimbal"` to the `use_free_approach` `hasattr` tuple (~line 951) so sim/virtual stay untouched.

## D. Instrumentation

`record()` must stop re-saving the frame (`_grab_sharp` saves once, returns the path) — that is why `ap_04`/`ap_05` were byte-identical.

New `approach_cycle` fields: `sweep`, `pan_offset`, `tilt_offset`, `pan_deg`, `bearing_deg`, `pivot_requested_deg`, `pivot_cmd_s`, `pivot_dir`, `residual_e`, `sharpness`, `frame_attempts`, `vlm_said_visible`, `accepted_as_visible`, `reject_reason`, `gate`, `hops_done`, `fill_prev`, `arrival_block`. **Rejected detections get a full record** — a silently dropped hallucination produces no RQ2 evidence.

`report.py::_approach_summary` gains `sweeps_used`, `gimbal_looks`, `chassis_pivots`, `chassis_spin_deg_total`, `search_vlm_calls`, `bearing_deg_at_lock`, `residual_e_at_lock`, `hops_done`, `frames_skipped_blurry`, `sharpness_min/median`, `detections_rejected` (per reason), `arrival_cross_check`, `gimbal_only_lock`. **Fix `motion_time_s`** — currently `span − vlm_time`, which is why it read `0`; compute it from logged `pivot_cmd_s` + `hop_cm / FREE_SPEED_CMPS`, and keep the old quantity as `wall_time_s`.

New `_uncertainties` rules: `hallucinated_box_rejected` (the RQ2 headline — fires even on successful runs), `false_arrival_rejected`, `arrival_unconfirmed`, `blurry_frames_skipped`, `bearing_residual_high` (an automatic sign/calibration-error alarm), `gimbal_deg_per_unit_unmeasured`, `tilt_offset_unmeasured`, `arrival_without_motion`. Adjust `low_confidence_steering` to key off `gate == "low_confirmed"` so a *rejected* low-confidence look no longer counts as steering evidence.

## E. Two calibration tools

Both in `tools/`, model-free, matching `f1_calibrate.py`'s style exactly (`sys.path.insert(...resolve().parent.parent)`, `from rover import get_rover`, interactive `input()` between reps, `rover.close()`), run as `venv/bin/python tools/<name>.py` from `Dashboard/brain/`.

**`f8a_tilt_offset.py` → `APPROACH_TILT_OFFSET`.** Chair ~1.5 m ahead; the tool sets a tilt, grabs a frame to `tools/sweeps/tilt_<offset>.jpg`, prints the clamped offset and sharpness; operator types `+`/`-`/step/`ok` while watching the image. At `ok` it sweeps pan to `0, ±PAN_SWEEP_UNITS` **at that tilt** and asks the operator to confirm the rover's own body isn't in frame at the extremes and the ceiling doesn't dominate. Prints the paste-ready line plus a **suggested `APPROACH_MIN_SHARPNESS` = 0.6 × median settled sharpness** (measured on this camera, not inherited from the streaming funnel's 25).

**`f8b_pan_deg_per_unit.py` → `GIMBAL_DEG_PER_UNIT` + sign verdict.** Must run **after** F8a, at the chosen tilt, so the constant absorbs any `cos(tilt)` factor. Two independent methods: (1) vision — put a distinctive object in frame, step pan through `0, ±200, ±400`, operator reads the object's x-fraction off each saved jpg, `deg_per_unit = (x(0) − x(P)) · HFOV / P`; (2) tape measure — mark where frame-centre lands on a wall at distance `D`, `deg = atan(L/D)`. Report both, their spread, and the paste-ready **positive magnitude**.

If the sign comes out negative, the tool must print a hard STOP: the fix is to flip `ROVER_PI_PAN_INVERT` in `.env` (and set `LOOK_LEFT_PAN=1900`), **not** to patch the approach loop — that keeps `_pwm()` as the single inversion point instead of creating a second one. Note this also changes the line mission's left look, so T5 must be re-run after.

> Related open item this will settle either way: `.env` still carries an unresolved ⚠ on `LOOK_LEFT_PAN=1100` versus `ROVER_PI_PAN_INVERT=true` — the two are mirror images about 1500 and can't both be right.

## `.env` additions

```dotenv
# -- gimbal-first approach search (amendment 2026-10-02; rung F8) -----------
APPROACH_TILT_OFFSET=0         # MEASURE with tools/f8a_tilt_offset.py (0 = shout in report)
GIMBAL_DEG_PER_UNIT=0          # MEASURE with tools/f8b_pan_deg_per_unit.py
PAN_SWEEP_UNITS=400            # = ROVER_PI_LOOK_OFFSET; hard-capped at 500 by _offset_limit()
SWEEP_OVERLAP_FRAC=0.15
SWEEP_MARGIN_DEG=20
SEARCH_MAX_SWEEPS=3
SEARCH_MAX_VLM_CALLS=18
APPROACH_SETTLE_S=0.8
APPROACH_MIN_SHARPNESS=25      # set from f8a's output
APPROACH_FRAME_RETRIES=2
MAX_FILL=0.60                  # tune from F3's true-positive max at 0.5 m
MAX_BOX_SIDE_FRAC=0.95
MIN_FILL=0.0008
MIN_HOPS_BEFORE_ARRIVAL=1
FILL_JUMP_MAX=3.0              # applied only while the previous fill < NEAR_FILL
MIN_PIVOT_DEG=6.0
MIN_PIVOT_S=0.12
APPROACH_ARRIVAL_CROSSCHECK=1
APPROACH_RESIDUAL_FLAG_E=0.30
# SEARCH_STEPS / SEARCH_PIVOT_S retired by the gimbal sweep.
```

## Verification sequence

Run strictly in order. Steps 1–3 and 7 are new; the rest re-runs the existing ladder against changed code.

| # | Step | Pass when |
|---|---|---|
| 0 | Pre-flight: container up, `ROVER=pi`, `NAV_MODE=free`, `tools/grab_frame.py` returns a frame | frame lands on disk |
| 1 | **F8a** → tilt offset, min sharpness, pan-extreme sanity | horizon mid-frame; chair fully in frame at 1.5 m; no rover body at pan extremes |
| 2 | **F8b** → deg/unit + sign | both methods agree within ~15%, **sign positive**. If negative: fix `ROVER_PI_PAN_INVERT`/`LOOK_LEFT_PAN`, re-run F8b, then re-run T5's look-left |
| 3 | **F3 re-score on a new corpus** — the existing `tools/f3_frames/` was shot at the floor-staring tilt, so 9/9 there is not evidence for the new config. Re-shoot 9 frames at the new tilt, re-run `f3_score_locate.py`, then run `_plausible()` offline over them | ≥8/9, **and zero true positives rejected**. Read the max `fill` of a true positive at 0.5 m; set `MAX_FILL` midway between it and 1.0, floored at 0.60 |
| 4 | **Sign test** (static, cheap, highest value): target ~2 m, chassis still, three placements — physically left / centre / right | left placement → **negative** `pan_offset`, negative `bearing_deg`, `pivot_dir = -1`, chassis turns **left**, `abs(residual_e) <= 0.30`. Mirror for right. `gimbal_only_lock: true` in all three |
| 5 | **F4** sonar stop — regression only (`hop()` unchanged) | returns `sonar_stop` |
| 6 | **F5** ×3, target 1.5 m in view | arrives; `hops_done >= 2`; `chassis_spin_deg_total == 0` during search; `arrival_cross_check == "confirmed"`; `cycles <= 8`; `motion_time_s > 0` |
| 7 | **Degenerate-box regression** (the `785c…` test): `approach` a target not in the room ("go to the fridge"), facing a blank wall | ends `blocked / target_not_found`; `outcome` **not** `completed`; `hallucinated_box_rejected` in `uncertainties` |
| 8 | **F6** voice missions ×5–6 per spec §4, plus (f) = step 7's absent target | per spec, and (b) resolves within `SEARCH_MAX_SWEEPS` at ≤232° spin |
| 9 | **F7** results table, new columns: gimbal looks vs chassis pivots, `chassis_spin_deg_total`, boxes rejected by reason, blurry frames skipped | table exists next to the line runs |

### If step 4 misbehaves

| Symptom | Cause | Fix |
|---|---|---|
| found at `pan_offset = -400`, chassis pivots **right** | pan sign inverted | flip `ROVER_PI_PAN_INVERT`, fix `LOOK_LEFT_PAN`, re-run F8b + T5 |
| found at `pan_offset = 0`, `e > 0`, chassis pivots **left** | `pivot()`'s direction sign regressed (flipped 2026-10-02) | restore `angular_z = direction * free_turn_z` |
| `residual_e` same sign, same magnitude as before the pivot | pivot didn't physically happen | `pivot_cmd_s` under the motor deadband → raise `MIN_PIVOT_S` |
| `residual_e` opposite sign, ~same magnitude | ~2× overshoot | one bearing term is sign-flipped, or `GIMBAL_DEG_PER_UNIT` ~2× too large |
| `residual_e` consistently ~0.6× the pre-pivot `e` | `FREE_TURN_DEG_PER_S` (measured over 1.0 s) too high for short ramp-dominated pivots | raise `MIN_PIVOT_S` or add a short-pivot scale; logged `pivot_requested_deg` vs `residual_e` pairs give the correction directly |

## Notes

- **Files touched:** `rover_pi.py` (one new method + two call-site refactors), `mission.py` (constants, `_derive_loc`, `_plausible`, `_grab_sharp`, `_arrival_ok`, `approach_controller`, `_arrival_check`'s `keep_tilt`, the gate tuple), `report.py` (`_approach_summary`, `_uncertainties`), `.env`, two new `tools/` scripts. **`planner.py` and `vlm.py` are not touched.**
- **Python 3.9:** `Optional[...]`/`List[...]`/`Tuple[...]` from `typing` throughout, never `int | None`.
- **Deliberately not changing `vlm.py`'s prompt** (e.g. "don't return a whole-image box"): it would change the F3 baseline mid-stream and muddy attribution. Land the code-side gate, log the rejection counts, then try the prompt change as a separate dated run — that attribution is itself RQ2 evidence.
- **Spec amendment:** add a dated block to `Progress/spec-free-roam-approach.md` in the style of the existing "Amendment (2026-10-01)", recording that §2's "keep the gimbal centred for the whole of approach" is superseded for the approach step, that §2's SEARCH pseudocode is replaced, and that `SEARCH_STEPS`/`SEARCH_PIVOT_S` are retired. Per CLAUDE.md, do not retrofit the older spec docs.
- **Scope guard (CLAUDE.md):** this is instrumentation, not perception research — no map, pose, depth or SLAM; every new number is a hand-measured `.env` constant, the same mechanism F1/F2 use. Resist any temptation to track targets across sweep frames or fuse detections: first accepted detection wins, the loop closes the error.
- **Benchmarking caveat:** `.env` is still on `PLANNER_PROVIDER=openai`/`VLM_PROVIDER=openai`. Fine for iteration, but every number intended as an RQ1 result — step 3's F3 re-score especially — must be re-run on local Ollama.
