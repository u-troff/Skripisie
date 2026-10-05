# Spec — supervisor feedback: context budget, status lights, distance-scaled hops, guidance reprompt (drafted 2026-10-05)

**Owner:** Utroff · **Build window:** Mon 5 – Wed 7 Oct 2026, then **freeze** (see §F).
**Hand to:** Claude Code. Builds on `Progress/spec-free-roam-approach.md` (gimbal-first search, `approach_controller`,
`hop`, `check_arrival`) and `Progress/spec-planner-profiles-and-virtual-sweep.md` (`tools/virtual_sweep.py`).

**Permissions:** edits to `rover_pi.py`, `rover.py`, `mission.py`, `mission_session.py`, `planner.py`, `planner_pi.py`,
`report.py`, `main.py`, `config.py`, `.env`, and new files under `tools/`. Ask before the first physical run of a session,
and before `docker restart`/`stop` on the container. **Python 3.9** — use `Optional[str]`, never `str | None`.

Origin: supervisor meeting, 2026-10-05. Four requests, plus one reframing of RQ1 (§E).

---

## 0. Starting point (read from the code, 2026-10-05)

Much of request 1 already exists. Do not rebuild it:

| Fact | Where |
| --- | --- |
| Hard sonar stop inside every forward hop: two consecutive NEW readings `< sonar_stop_mm` zero the Twist and return `{"status": "sonar_stop", "sonar_mm", "moved_s"}`. Default `sonar_stop_mm=200`. | `rover_pi.py` `hop()` (~l.722), ctor (~l.127) |
| Arrival by sonar: `sonar_mm <= ARRIVE_MM (250)` **and** `abs(e) <= CENTER_TOL`; vision arrival is a fallback and is vetoed if sonar reads `> ARRIVE_SONAR_SANITY_MM (800)`. | `mission.py` ~l.74–82, ~l.1115 |
| Hop size is **binary**: `HOP_NEAR_CM (15)` if untrusted or `fill > NEAR_FILL`, else `HOP_CM (30)`. | `mission.py` ~l.1176 |
| A `blocked` step result sets `MissionPhase.HALTED`, speaks "I am blocked and have stopped.", and ends the mission. | `run_mission`, `if result.get("status") == "blocked"` |
| Plan swap mid-mission already exists: `pending_swap` → `active_plan`, `cursor = 0`. | `run_mission` top of loop, `mission_session.py` |
| Clarification history already exists: `DialogueSession.turns`, `history()`, `effective_command()`. | `dialogue_session.py` |
| Voice reply path mid-mission already exists for revision confirmation. | `main.py` `/ws/execution` → `handle_revision_confirmation` |
| Local models run with `num_ctx = 8192` (planner and VLM). Digest kept in prompts is capped by `PERCEPTION_DIGEST_KEEP = 6`. | `providers/factory.py`, `mission_session.py` |
| RGB status lights are **not wired**; guide §4 documents the `RGBStates` topics. | `Progress/turbopi-ros2-programming-guide.md` §4 |

Measured from `logs/mission_*.json` (virtual runs, planner role): prompt ≈ 430 tokens at depth 1 → ≈ 500 at depth 12
(≈ +7 per step); completion ≈ 30 tokens per step. VLM prompt ≈ 1,100–1,200 tokens per call (image included).

---

## 1. Goal and locked decisions

| Item | Decision |
| --- | --- |
| A. Context budget | Offline analysis tool + one validation experiment. **No hardware.** Do first. |
| B. Status lights | Thin `status_light(state)` on the rover; no-op on sim/virtual. ≤ 2 h. |
| C. Distance + hops | **VLM estimates distance to floor targets**; the estimate scales the hop (fraction, clamped). Sonar caps the hop and hard-stops at 30 cm. Raised targets out of scope. |
| D. Guidance reprompt | On `blocked` / `target_not_found`, stop, ask "what next?", keep context, re-plan from current state. **Failure recovery only — never an ambiguity halt.** |
| Priority if time runs short | Cut C's variable hop first (efficiency only). Keep the 30 cm stop, A, B, D. |
| Environment | **Unconstrained**: no fiducial markers, tape or beacons in free-roam evaluation (§E). |

**Out of scope:** SLAM, mapping, metric localisation, object-size databases, markers.

---

## A. Context budget (virtual side, offline)

**Purpose:** turn "how much context does a command + plan + room use" into a number, per room and per depth, and say at
what depth a given context window is exhausted.

**New file `tools/context_budget.py`**, run as `venv/bin/python tools/context_budget.py` from `Dashboard/brain/`.

1. For each room file in `rooms/` (`room_tour1`, `room_studio`, `room_cluttered`, `room_loft`) build the **real** planner
   prompt through the same code path `planner_virtual.py` uses, for synthetic chains of depth `N ∈ {1, 5, 10, 15, 20, 25, 30}`.
2. Count prompt tokens. Preferred: ask Ollama (`num_predict=1`, read `prompt_eval_count`) for the local models. For the cloud
   model, use the provider's reported usage from one call, or `tiktoken` as an approximation and **label it approximate**.
3. Measure the components separately and print them:
   `static` (system prompt + rules) · `room_digest(room)` · `per_instruction_in` · `per_step_out` (from logs: completion
   tokens ÷ steps) · `per_clarification_turn` · `per_guidance_turn` (§D) · `per_vlm_frame` (~1,150).
4. Project: `tokens(N) = static + room_digest + N·per_instruction_in + N·per_step_out` and the depth at which it crosses
   each window: the configured `num_ctx` (8192), and each model's **native** window. **Look native windows up from the model
   cards and write the source next to the number — do not assume them.** Note the cloud run is `gpt-5.6-luna`, not GPT-4.
5. Write `logs/context_budget.csv` and a one-line-per-room summary.

**Live-mission budget (separate table):** `static + digest(≤ PERCEPTION_DIGEST_KEEP lines) + guidance history`; confirm from
code whether VLM frames accumulate across calls (logs suggest each VLM call is independent, ~1,141 prompt tokens flat) and
state the answer. This is where the window can actually fill, not in single-shot planning.

**Validation experiment (this is what turns theory into evidence):** the projection says single-shot planning at depth 30
uses well under 8k, so overflow will not appear at `num_ctx = 8192`. Predict instead the depth `D*` at which a **reduced**
window overflows (e.g. `num_ctx ∈ {1024, 2048}`), run the depth sweep (`tools/virtual_sweep.py`) at those settings, and report
whether failures begin near `D*`. Record in the log how the model fails (dropped early steps, dropped tail, malformed JSON).
Confirm Ollama's overflow behaviour (silent truncation of the oldest tokens?) before relying on it.

**Acceptance:** CSV exists for all 4 rooms × 7 depths; projected `D*` printed per window; at least one reduced-`num_ctx`
sweep run with the observed breakdown depth next to the predicted one.

**Report mapping:** Ch5 RQ2 — "context overflow is a calculable hard limit; forgetting *before* the limit is empirical".

---

## B. Status lights (RGB)

**API:** add `status_light(self, state: str) -> None` to the `RoverController` ABC in `rover.py` as a **no-op default**, so
`SimulatedRover` and `VirtualRover` are untouched. Implement in `PiRoverController` (`rover_pi.py`): publish an `RGBStates`
message to **both** `ros_robot_controller/set_rgb` and `sonar_controller/set_rgb` (stock code publishes to both; message
shape per guide §4). Never block: `roslibpy` publish on an open connection returns immediately. Wrap in try/except and log —
a light failure must never affect a mission. Config: `RGB_ENABLED` (default 1).

**State map (starting point — adjust colours on the real unit):**

| State | When | Light |
| --- | --- | --- |
| `listening` | waiting for the command | blue, steady |
| `thinking` | STT / ambiguity check / planning / verifying | red↔green alternating (user's idea) |
| `confirm` | plan read back, waiting for yes | amber, steady |
| `executing` | driving / hopping | green, steady |
| `searching` | search sweeps / re-lock | cyan, steady |
| `awaiting_guidance` | §D, waiting for the human | magenta, slow blink |
| `arrived` | success | green, 3 flashes then steady |
| `blocked` | blocked / halted | red, steady |

**Wiring points:** phase transitions in `run_mission`, `approach_controller` (search vs approach), `_speak`, and the
dialogue phases in `main.py`. **Open question for Claude Code:** the pre-departure dialogue (`/ws/dialogue`) may not hold a
rover handle — check how `rover.get_rover()` is cached before wiring `thinking`/`listening`; if no handle exists, create a
lazily-connected status-only handle rather than opening a second mission connection.

**Also log:** every state change with a timestamp into the run log (`light_states: [{t_rel_s, state}]`) — it doubles as a
state-timeline figure for Ch4.

**Acceptance:** all 8 states visible on the real unit; mission unaffected when `RGB_ENABLED=0` or the topic is down.

---

## C. VLM distance estimate, variable hop, 30 cm stop

**Scope: floor-level targets only.** The camera is low, so a target on a desk or shelf is usually out of frame and has no
floor contact point to estimate from. Raised targets are **out of scope** (stated limitation / future work) and are covered by
one fail-safe test in Level 5 (§F). The vision model estimates the distance; no geometry or calibrated camera model is used.

### C1. Sonar threshold
Make the hard stop configurable (`ROVER_PI_SONAR_STOP_MM`, passed to the ctor) and set it to **300**. Align `ARRIVE_MM = 300`.
Consequence to handle: with the stop at 300 the rover will usually stop inside the hop (the `sonar_stop` branch) rather than
reach the `sonar_hit` test, and that branch currently only accepts arrival if the last look was trusted, centred and
`fill > NEAR_FILL`. Bench-test with a small box; if it false-blocks, relax that branch to "last accepted look showed the
target visible and centred". Do not remove the centred requirement — sonar alone also stops for a wall or chair.

### C2. VLM distance estimate (one extra JSON field, no extra call)
Add a field to the existing `locate_target` JSON in `vlm.py` (prompt + parser). Suggested wording:

`"distance_m": number or null — your best estimate, in metres, of the horizontal distance from the camera to where the object touches the floor; null if you cannot tell`

In `_derive_loc` (`mission.py` ~l.739) pass it through as `loc["distance_cm"] = distance_m * 100`. Accept only numbers with
`0 < d <= VLM_DIST_MAX_CM (400)`; anything else becomes `None`. Unknown extra keys must not break parsing — check this in
`vlm.py` first.

**Go/no-go test before building the hop (≈ 20 min, LM Studio or a short script):** photograph the box on the floor at
30, 50, 75, 100 and 150 cm, ask `qwen2.5vl:3b` and `gemma4:e4b` for the distance. Decision rule:
- median relative error ≤ 30 % **and** estimates rise with true distance → use the numeric estimate;
- otherwise ask for coarse buckets (`<0.3`, `0.3–0.6`, `0.6–1`, `1–2`, `>2` m) and use the bucket midpoint;
- if still noise → keep `HOP_MODE=fixed` and report VLM distance accuracy as a finding.

### C3. Measuring VLM distance accuracy (report evidence)
- **Active:** `tools/f9_distance_calib.py`, interactive, same style as `f1_calibrate.py`. Box on tape marks at 30/50/75/100/150 cm,
  3 reps each. For every configured VLM, grab a frame, call `locate_target`, log `d_true, d_vlm`. Print per-model median
  relative error, bias, and worst case per range. This is the stated accuracy of vision-only distance (a limitation, not a
  hidden assumption).
- **Passive:** every approach cycle logs `d_vlm_cm` next to `sonar_mm` (§C4). In analysis, use only cycles where the target is
  centred (`abs(e) <= CENTER_TOL`), sonar is readable and the target is on the floor, to get a VLM-vs-sonar error dataset from
  normal runs with no extra effort.

### C4. Hop policy — replaces the binary choice at `mission.py` ~l.1176
Constants (next to the other hop constants):

```python
HOP_MODE = config.get("HOP_MODE", "fixed").strip().lower()   # fixed | variable
STOP_CM = config.get_float("STOP_CM", 30.0)
HOP_FRAC = config.get_float("HOP_FRAC", 0.6)
HOP_MIN_CM = config.get_float("HOP_MIN_CM", 8.0)
HOP_MAX_CM = config.get_float("HOP_MAX_CM", 50.0)
VLM_DIST_MAX_CM = config.get_float("VLM_DIST_MAX_CM", 400.0)
```

New pure function above `approach_controller`:

```python
def _hop_for(loc: dict, sonar_mm: Optional[int], trusted: bool, centred: bool) -> Tuple[float, str]:
    fixed = HOP_NEAR_CM if (not trusted or loc.get("fill", 0) > NEAR_FILL) else HOP_CM
    if HOP_MODE != "variable":
        return fixed, "fixed"
    hop, source = fixed, "fallback"
    d_vlm = loc.get("distance_cm")
    if trusted and isinstance(d_vlm, (int, float)) and 0 < d_vlm <= VLM_DIST_MAX_CM:
        hop, source = HOP_FRAC * (d_vlm - STOP_CM), "vlm"
    if sonar_mm is not None and centred:
        clearance = sonar_mm / 10.0 - STOP_CM
        if clearance < hop:
            hop, source = clearance, "sonar_cap"
    return max(HOP_MIN_CM, min(HOP_MAX_CM, hop)), source
```

Call site — replace the `hop_cm = HOP_NEAR_CM if ...` line:

```python
        hop_cm, hop_src = _hop_for(loc, sonar_mm, trusted, abs(e) <= CENTER_TOL)
```

(`sonar_mm` is already read earlier in the same cycle.) Add `"hop_src": hop_src, "d_vlm_cm": loc.get("distance_cm"),
"sonar_mm": sonar_mm` to the dict passed to `record(...)`.

**Why a fraction, not the full distance:** the VLM number is uncalibrated, and long open-loop hops drift on mecanum wheels. A
60 % hop converges over a few cycles. **Why the sonar cap:** it limits the hop to the free distance when the target is centred
(the "nothing in its path" case); the in-hop watchdog (C1) remains the last line of defence. The dangerous failure is an
overestimate when the target is **off-beam**, which is why the hop is a fraction and capped at `HOP_MAX_CM`.

Approach summary additions: `hops_done`, `vlm_calls`, `approach_wall_time_s`, `hop_src` counts.

**A/B (report evidence):** same placements, `HOP_MODE=fixed` vs `variable`, ≥ 5 runs each; compare hops, VLM calls, time, and
whether arrival was confirmed. Variable hop is claimed only as an efficiency gain, because the fixed hop already succeeded on
the real robot. **If `HOP_MODE=variable` is not stable by Wed 7 Oct, ship `fixed` and report variable hop as future work.**

### C5. Sonar limits to bench-test before trusting it
Narrow cone, specular/soft surfaces and off-axis objects. Test with the box on the floor: square-on at 50/30/20 cm, 30° off-axis,
a soft/cloth surface, a chair leg, a wall. Record which cases the sonar misses; those are exactly where vision arrival must
remain the fallback.

**Acceptance:** rover stops ≥ 25 cm from a box and from a wall in 10/10 bench trials; the go/no-go test (C2) is recorded;
`f9` per-model error printed; `HOP_MODE=variable` completes the same placements as `fixed`.

---

## D. Guidance reprompt — "I have stopped, what should I do next?"

**Behaviour:** when a step returns `blocked` (reasons seen in the logs: `target_not_found`, `obstacle`,
`arrival_contradicted`, `cycle_budget_exhausted`), the rover halts, **asks** instead of ending the mission, keeps the full
conversation context, and re-plans from the current state when the human answers.

**New phase `MissionPhase.AWAITING_GUIDANCE`** in `mission_session.py` (not terminal). New fields on `MissionSession`:
`guidance_log: List[dict]` (`reason`, `question`, `answer`, `t_rel_s`, `replanned`), `guidance_turns: int`.

**Flow in `run_mission`**, replacing the `blocked → HALTED → break` branch:

1. `rover.halt()`; set `AWAITING_GUIDANCE`; `status_light("awaiting_guidance")`.
2. Build a short, factual question from the failure detail via `_speak` (e.g. "I stopped because I could not find the
   orange box after searching the room. What should I do next?"). Include the reason code in the log, not necessarily in speech.
3. Wait for the answer on the existing execution WebSocket: extend the audio/text handling in `main.py` `/ws/execution`
   (currently `handle_revision_confirmation`) with `handle_guidance(mission, audio_or_text, emit)`; transcribe with the same
   STT path.
4. **Re-plan from state** (new `planner.replan_from_state(...)` / profile equivalent in `planner_pi.py`): prompt =
   original command + all clarification turns + steps completed so far + the failure reason + the human's answer + current
   `digest` (≤ `PERCEPTION_DIGEST_KEEP`). Output must still pass the existing capability-vocabulary validation. Apply through
   the **existing** `pending_swap` mechanism so cursor reset and the `plan_revised` event are reused.
5. Back to `EXECUTING`. Cap with `MAX_GUIDANCE_TURNS = 3`; on the cap, or on a reply classified as "stop"/"cancel" by
   `confirmation.classify`, end as `HALTED` with `reason = guidance_exhausted | guidance_declined`.

**Outcome taxonomy (do not merge these into "completed"):** `completed`, `completed_after_guidance`,
`halted_after_guidance`, `halted`. Report fields: `guidance_turns`, `guidance_log`, `recovered: bool`.

**Why this stays inside the research framing:** the three-moment framework says ambiguity is asked **before departure** and
residual doubt is reported at the end, with **no mid-mission human halt for ambiguity**. That is the claimed departure from
KnowNo/IntroPlan. This feature must not blur it: the reprompt fires only on a *physical/search failure*, never to resolve
ambiguity, and is reported as **failure recovery** (Ch3 design, Ch5 as a separate outcome class).

**Virtual harness:** `VirtualRover` already produces `blocked` (collision, unresolved target). Add a scripted-guidance map in
`tools/virtual_sweep.py` (command → list of answers) so batches run unattended and are reproducible; log whether the
re-plan succeeded. Without this the sweeps cannot exercise §D.

**Context cost:** each guidance turn adds tokens to the next planner call. Feed `per_guidance_turn` into §A's table.

**Acceptance:** a forced `target_not_found` (box removed) triggers the question; a spoken/typed answer ("it is to your left")
produces a validated new plan and a `completed_after_guidance` run; `MAX_GUIDANCE_TURNS` ends cleanly; sim/virtual behaviour
unchanged when guidance is disabled (`GUIDANCE_ENABLED=0`).

---

## E. RQ1 reframing (documentation only — no code)

**Proposed RQ1 wording:**
> In an unconstrained indoor environment, with no fiducial markers, tape or beacons added, can a fully local AI pipeline turn
> a vague spoken command into a plan the rover can execute and follow?

**Examiner defence (Ch3 design rationale):** markers require modifying and pre-mapping the room, which defeats the premise of
finding an unknown object from a vague command; distance therefore comes from vision plus an ultrasonic range sensor, which
is coarser — measured in `f9` and reported as a limitation. Because there are no markers the rover cannot drive blind, so it
works in stop-look-hop cycles.

**Scope caveat (targets):** targets are floor-level objects visible from the rover's low camera; raised targets (on desks or shelves) are out of scope and listed as future work.

**Scope caveat (line mission):** the taped-line mission is instrumentation. If the report cites it, describe it as a baseline/calibration
experiment and scope the "no instrumentation" claim to the free-roam evaluation.

---

## F. Order, freeze and report hooks

| Day | Work |
| --- | --- |
| Mon 5 | **A** (offline) · **B** (lights) · C5 sonar bench test + C1 threshold |
| Tue 6 | **D** (virtual first, then real) · C2/C3 calibration |
| Wed 7 | C4 variable hop + A/B · integration pass · **freeze tag Wed night** |

Freeze rule: after the tag nothing in code, prompts or `.env` changes; real-robot RQ1 batch (Levels 0–5, E4B) runs Thu–Sat
against the tag. Record model tags, `.env` and `planner_profile.prompt_sha` with the tag. If variable hop is not stable by
Wed, ship with `HOP_MODE=fixed` and report variable hop as future work.

**Report hooks:** Ch3 — control split (sonar reactive / VLM stationary), failure-recovery loop, status-light state map.
Ch4 — `_hop_for`, calibration, `AWAITING_GUIDANCE`. Ch5 — context-budget table + reduced-`num_ctx` validation (RQ2),
recovered-run outcome class and level-0 control (RQ1), distance MAE and fixed-vs-variable A/B.

**Level 5 raised-target case:** include one raised-target command (e.g. "go to the lamp on the desk") in the real-robot Level 5 tests. Expected behaviour is a safe failure: the rover searches, reports it cannot find the object (with §D: stops and asks), and does **not** drive up to the desk. Score it as evidence for the stated limitation.

## G. Open questions for Claude Code to check and report back (do not guess)

1. Does the dialogue path hold a rover handle for `thinking`/`listening` lights (§B)?
2. Do VLM frames accumulate across calls anywhere, or is each call independent (§A live budget)?
3. What does Ollama do on prompt overflow at the configured `num_ctx` (§A validation)?
4. Native context windows of `gemma4:e2b`, `gemma4:e4b`, `qwen2.5vl:3b` and `gpt-5.6-luna` — from model cards, with sources.
5. Does `ROVER_PI_SONAR_STOP_MM` already exist as an env var, or must it be added to the ctor wiring?
6. Where are the `locate_target` prompt and parser in `vlm.py`, and does `_derive_loc` tolerate an added `distance_m` key (and a `null` value) without breaking the existing gates?
