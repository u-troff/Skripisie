# Test plan C — sonar 300 mm, VLM distance, variable hop

Spec §C. Files: `.env`, `rover.py` (default only), `vlm.py` (prompt + `distance_cm_from`), `mission.py` (`_hop_for`, sonar_stop branch, records), `report.py` (`_approach_summary` additions), new `tools/f9_distance_calib.py`, new `tools/f10_sonar_bench.py`.
Floor-level targets only. Raised targets are out of scope (Level 5 safe-fail test, §C8).
**Priority if time runs short: cut C4 (variable hop) first. Keep the sonar stop, A, B, D.**

Setup for every physical session: box (the same one each time), tape marks on the floor at 20/30/50/75/100/150 cm from a start line, tape measure, rover battery charged. Ask Utroff before the first run.

## C1. Sonar threshold (Mon 5)

`.env` changes: `ROVER_PI_SONAR_STOP_MM=300`, **`ROVER_PI_SONAR_CLEAR_MM=400`** (must exceed the stop value), `ARRIVE_MM=300` (already). Restart the backend after editing.
☐ `brain.log` after start shows no config warning; `rover.sonar_stop_mm == 300` (`python -c "import config; print(config.get_int('ROVER_PI_SONAR_STOP_MM',200))"` → 300).

## C5. Sonar bench test (Mon 5) — `tools/f10_sonar_bench.py`

Run: `venv\Scripts\python.exe tools\f10_sonar_bench.py` (interactive; each case asks you to place the obstacle, hops 80 cm toward it, then asks the measured gap in cm).
Cases (3 reps each, listed in the tool): box square-on from 50 / 30 / 20 cm; box 30° off-axis; soft/cloth surface; chair leg; wall.
Per rep record: `result` (`sonar_stop` or `ok`), `sonar_mm`, `moved_s`, the **measured gap (cm)**.
Pass (spec): stops ≥ 25 cm from the box and from a wall in 10/10 trials. Tick:

- ☐ box square-on: 9/9 stopped ≥ 25 cm
- ☐ wall:3/3
- ☐ cases where the sonar **missed** (hop returned `ok` and the rover hit or nearly hit the obstacle): ______ (these are exactly where vision arrival must stay the fallback; write them in the report)
  Risk (see README #2): expected stops are ~25–29 cm. If any rep is < 25 cm, set `ROVER_PI_SONAR_STOP_MM=330`, restart, and repeat the failing cases. Do not change the two-reading rule.
  Also run `tools\f4_sonar_stop.py` once with a box dropped into the path mid-hop (regression: still stops).

### C1 consequence — sonar_stop branch (Mon 5, with a small box)

With stop = 300, the rover usually stops *inside* the hop. The patched branch in `approach_controller`: (a) old fast path if the last look was trusted, `fill > NEAR_FILL` and centred; (b) else if the last look was visible and centred and this is the **first** consecutive sonar stop, fall through to a re-look so the normal `sonar_hit` + VLM cross-check decides arrival; (c) else `blocked: obstacle`.
☐ Box on the floor 1.2 m ahead, centred, free-roam command "go to the box". Expect `arrived (sonar)` (or the fast path), not `obstacle`. Run 5×: ___/5 arrivals, ___ false blocks.
☐ Wall/chair test: "go to the box" with a chair between rover and box. Expect `blocked obstacle` within 2 hops (not 20 cycles of repeated sonar stops).

## C2. VLM distance go/no-go (Tue 6, ≈ 20 min, no rover needed)

Capture on the rover (gimbal at `APPROACH_TILT_OFFSET` is set by the tool): the tool creates `logs\f9\<timestamp>\` itself and names the files `d030cm_1.jpg` etc. Box on each tape mark (30/50/75/100/150 cm), Enter per photo.
☐ `venv\Scripts\python.exe tools\f9_distance_calib.py --capture-only --reps 2` (change marks with `--distances 30,50,...`).
Then score without the rover as often as you like (adjust the box name):
☐ `venv\Scripts\python.exe tools\f9_distance_calib.py --from-dir latest --models qwen2.5vl:3b,gemma4:e4b --target "orange box"`
(`--from-dir` also accepts any folder of your own photos named `…<N>cm…jpg`.) Results CSV is written beside the frames.
Decision rule (printed by the tool):

- median relative error ≤ 30 % **and** medians rise with true distance → keep `VLM_DIST_MODE=numeric`;
- else set `VLM_DIST_MODE=bucket` and rerun; bucket midpoints are used;
- else still noisy → keep `HOP_MODE=fixed`, report VLM distance accuracy as a finding.
  Record the verdict per model in the table. Choose the **model that will run on the real robot** (qwen2.5vl:3b today) for the decision.
  Check the prompt did not damage localisation: ☐ `visible` and bbox still returned for ≥ 90 % of these frames, compared with the same frames before the prompt change (use `tools\f3_score_locate.py` or eyeball `visible` counts).

## C3. Calibration on the rover (Tue 6) — `tools/f9_distance_calib.py`

☐ `venv\Scripts\python.exe tools\f9_distance_calib.py --models qwen2.5vl:3b,gemma4:e4b --target "orange box"` (interactive: place the box at each tape mark, Enter; 3 reps × 5 distances; one frame per rep is shared by all models).
Prints per model: median relative error, bias (signed mean), worst case per range, and the decision-rule verdict; saves frames to a new `logs/f9/<timestamp>/` and writes `results_<time>.csv` there. Use `--capture-only` to capture now and score later.
Passive dataset: after any run, analyse approach cycles with `accepted_as_visible`, `abs(x_center-0.5) <= CENTER_TOL`, `sonar_mm` not null: compare `d_vlm_cm` with `sonar_mm/10` (the box is on the floor, so sonar ≈ ground truth). Quick script (paste in a shell):

```
import json,glob,statistics as st
rows=[]
for p in glob.glob("logs/mission_*.json"):
    for c in json.load(open(p,encoding="utf-8")).get("checks",[]):
        if c.get("kind")=="approach_cycle" and c.get("d_vlm_cm") and c.get("sonar_mm") and c.get("centred"):
            rows.append((c["d_vlm_cm"], c["sonar_mm"]/10.0))
err=[abs(a-b)/b for a,b in rows if b>0]
print(len(rows), "pairs; median rel err", st.median(err) if err else None)
```

(The `centred` flag is written by the new record fields.)

## C4. Variable hop (Wed 7) — only if C2 says numeric/bucket is usable

Offline first (no rover): ☐ `venv\Scripts\python.exe -c "import mission; print(mission._hop_for({'fill':0.02,'distance_cm':100.0}, 1000, True, True))"` → with `HOP_MODE=variable`, `STOP_CM=30`, `HOP_FRAC=0.6` expect `(42.0, 'vlm')`. Table (set `HOP_MODE=variable` in the shell for this check):

| call                                                               | expect                                                                     |
| ------------------------------------------------------------------ | -------------------------------------------------------------------------- |
| `_hop_for({'fill':0.02,'distance_cm':100.0}, 1000, True, True)`  | `(42.0, 'vlm')`                                                          |
| `_hop_for({'fill':0.02,'distance_cm':100.0}, 500, True, True)`   | `(20.0, 'sonar_cap')` (clearance 50−30 = 20)                            |
| same,`centred=False`                                             | `(42.0, 'vlm')` (no sonar cap off-beam)                                  |
| `_hop_for({'fill':0.02,'distance_cm':300.0}, None, True, True)`  | `(50.0, 'vlm')` (clamped to `HOP_MAX_CM`)                              |
| `_hop_for({'fill':0.02,'distance_cm':35.0}, None, True, True)`   | `(8.0, 'vlm')` (clamped to `HOP_MIN_CM`)                               |
| `_hop_for({'fill':0.02,'distance_cm':100.0}, 1000, False, True)` | `(<fixed near hop>, 'fallback')` (untrusted gate ignores the VLM number) |
| `_hop_for({'fill':0.02}, None, True, True)`                      | `(<fixed>, 'fallback')`                                                  |
| `HOP_MODE=fixed`, anything                                       | `(<fixed>, 'fixed')`                                                     |
| ☐ all rows match.                                                 |                                                                            |

On the rover (ask first). **A/B**: same box placements, `HOP_MODE=fixed` vs `variable`, ≥ 5 runs each, alternating A,B,A,B to cancel battery drift. Use ≥ 3 distinct start distances (e.g. 1.0 / 1.5 / 2.0 m) and a fixed start heading. Record from the run log `approach_summary` (`hops_done`, `vlm_calls`, `approach_wall_time_s`, `hop_src` counts) and whether arrival was confirmed.

| Run | Mode | Start dist | hops | VLM calls | time (s) | arrival confirmed | hop_src counts |
| --- | ---- | ---------- | ---- | --------- | -------- | ----------------- | -------------- |

Claim made only if variable ≤ fixed on hops/VLM calls **and** equal arrival rate. Otherwise report as future work and **ship `HOP_MODE=fixed`** (hard rule: if not stable by Wed 7 Oct).
Acceptance (spec): `HOP_MODE=variable` completes the same placements as `fixed` (no new blocks, no collisions).

## C6. Safety sweep for variable hop (Wed 7)

☐ Off-beam target (box 40° to the side, hop is gated by `centred`): no sonar cap applies. Confirm the hop stays ≤ `HOP_MAX_CM` and the in-hop watchdog stops at 30 cm if something is in the path.
☐ Overestimate injection: temporarily force `distance_cm=400` for a box 80 cm away (edit the loc in a test run). Hop should be ≤ 50 cm (`HOP_MAX_CM`) and the sonar cap/stop should prevent contact.

## C8. Level-5 raised-target case (Thu–Sat batch, listed here for completeness)

☐ "Go to the lamp on the desk": the rover searches, reports it cannot find the object (with §D: stops and asks), and does **not** drive to the desk. Score as evidence for the stated limitation.

## Results to record

| Item                                                     | Value |
| -------------------------------------------------------- | ----- |
| Sonar stop value used                                    |       |
| Stop distances (box / wall), min                         |       |
| Sonar misses (cases)                                     |       |
| VLM distance verdict per model (numeric / bucket / none) |       |
| Median rel. error / bias per model                       |       |
| Passive VLM-vs-sonar median rel. error                   |       |
| A/B summary (hops, VLM calls, time)                      |       |
| Final`HOP_MODE` shipped                                |       |
