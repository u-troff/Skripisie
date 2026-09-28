# Claude Code prompt — split planner, virtual navigation, local-vs-cloud sweep (v2, 2026-09-29)

**Supersedes** v1 of this file and `Progress/spec-virtual-follow-line.md` (line following inside
the virtual rover is **dropped**).

**Owner:** Utroff · **Machine:** Windows desktop (Titan X, Ryzen 5 3600, 16 GB), PowerShell
**Hand to:** Claude Code, working in `Dashboard/brain/` and `Dashboard/ui/`.
**Read first:** `WINDOWS-SETUP.md` (it replaces the macOS `CLAUDE.md` on this machine), then
`Progress/spec-virtual-rover-and-room-mapping.md` (the original design of the virtual rover).

**Permissions:**

- **Brain files you may edit:** `planner.py`, `rover.py` (only `VirtualRover` and its config
  getters), `room_map.py`, `rooms/`, `report.py`, `mission.py`, `mission_session.py`, `main.py`,
  `tools/plot_run.py`, `providers/` (only the usage ledger in §3F), and `.env`.
- **Files you may create:** `planner_pi.py`, `planner_virtual.py`, `nav.py`,
  `tools/snapshot_prompt.py`, `tools/virtual_sweep.py` and `tools/sweeps/`.
- **UI files you may edit:** `types.ts`, `store.ts`, `net.ts`, `App.tsx` and
  `components/ExecutionPanel.tsx`, and you may add components.
- **Do not touch:** `rover_pi.py`, the `PiRoverController` wiring, `vlm.py`, `revision.py`,
  `pipeline.py`.
- **Ask before:** adding any pip or npm dependency. Everything here needs only the stdlib plus
  what's installed; A* is about 60 lines of pure Python.
- **Python 3.9:** use `Optional[...]`, never `X | None`, and no `match`.

---

## 0. The design in one paragraph

**Real rover** (`ROVER=pi`): voice in, audio out on the TurboPi, line following. It's the
real-world end-to-end test with local and cloud models. **Virtual rover** (`ROVER=virtual`): typed
commands on the desktop, driving on a hand-drawn room map in a fixed metric reference frame. It's
the controlled **local-vs-cloud** experiment for the reasoning layer (clarifying dialogue,
planning, verification, grounding) and for **RQ2: how many instructions can be chained before the
plan breaks down**.

The virtual rover has two kinds of motion:

- **`approach X` is a navigation skill.** The rover plans its own collision-free route around
  obstacles on the map. So a failure there can only be a wrong plan (wrong target, wrong order,
  a target that doesn't exist).
- **`move` / `turn` are literal.** They're executed exactly as planned, with no avoidance, so a
  collision means the chain of instructions went wrong.

This is the same split as SayCan (in `Previous Research/`): the language model picks the skills,
and lower-level control executes them.

## 1. Locked decisions

| Decision | Choice |
|---|---|
| Reference frame | Everything uses the **room frame** in metres. The origin is the room corner, `x` runs along the width, `y` runs away from the start wall, and `theta` is degrees CCW from +x. Landmarks, obstacles, rover pose and paths are all expressed in it. **No pixels, no vision, no estimation.** The fixture is typed in from a sketch |
| Rover footprint | A rectangle, `VIRTUAL_ROBOT_LENGTH_M` × `VIRTUAL_ROBOT_WIDTH_M`. Clearance radius = half-diagonal + `VIRTUAL_SAFETY_MARGIN_M`. Use the half-diagonal because a mecanum base turns on the spot, so rotation is safe anywhere the centre is. **0.187 × 0.162 m: manufacturer spec, confirmed by Utroff (~18 × 16 cm measured)**. One inflated occupancy grid (walls + obstacles, 2 cm cells) is used for **both** navigation and collision checks, so they can never disagree |
| `approach` | A* on the inflated grid, 8-connected, then line-of-sight smoothing into waypoints. The goal is the landmark's standing spot itself: landmark coordinates *are* standing spots (see the `room_map.py` docstring). No drift. If there's no path, return `blocked` with `reason: no_path` |
| `move` / `turn` | Literal. Distance comes from the step's `distance_m`, then from metres or cm in the target text, then from `VIRTUAL_STEP_M`. Angle comes from `degrees`, then from the text, then from `VIRTUAL_TURN_DEG`. The path is sampled every 2 cm on the grid. On the first blocked sample the rover stops at the last free one and returns `blocked` with `reason: collision`, naming the obstacle or "the room boundary" |
| Motion parameters | `VIRTUAL_LINEAR_SPEED_MPS` and `VIRTUAL_TURN_RATE_DPS`. **Simulated** time = distance/speed + turn/rate, and every metric uses simulated time. Wall-clock dwell = simulated / `VIRTUAL_TIME_SCALE` (1 = real time, 20 = sweeps) |
| Drift | Off in sweeps (`VIRTUAL_DRIFT_FRAC=0`, `VIRTUAL_DRIFT_DEG=0`), so every collision comes from the plan. Drift stays implemented for **one** side experiment (§6 P9) |
| Planner files | `planner_pi.py` and `planner_virtual.py` each hold `PROFILE_NAME`, `ACTIONS`, `GUIDANCE` and `STEP_SCHEMA` (**prompt text only**). `planner.py` keeps the single `generate_plan` / `revise_plan` and picks the profile per call: `PLANNER_PROFILE` if set, else `pi` for `ROVER=pi`, else `virtual`. Two full copies of the planner would drift apart and contaminate the comparison |
| Pi prompt | **Byte-identical to today's**, proven by P1 |
| Virtual vocabulary | `approach, move, turn, scan, observe, stop, report`. **No `follow_line`.** If a model emits it anyway, the rover blocks with `unknown_action` and it's scored as an out-of-vocabulary error, not aliased |
| Input | **Typed text** in virtual runs (the sweep types it; the dashboard accepts typed text too). The voice loop (mic → STT → audio reply) is tested on the real TurboPi only |
| Perception | None in virtual runs. The VLM role gets text only (`SCENE_SOURCE=room`), and the browser frame source is Off. This must be stated in Ch4 |
| Confirmation | Sweeps auto-confirm with `"yes"`, recorded as `auto_confirmed: true` |

---

## 2. Step 0 — the desktop has the current code, and a snapshot of the Pi prompt

```powershell
cd $HOME\Desktop\Skripsie; git pull
Select-String -Path Dashboard\brain\planner.py -Pattern "follow_line"
Test-Path Dashboard\brain\report.py
```

Both must hit. If they don't, **stop and tell me** (the Mac's commits aren't pushed). Then, **before
any edit**, write `tools/snapshot_prompt.py`. It prints the exact `generate_plan` prompt for a fixed
command and scene **without calling a model**; factor out `build_plan_prompt(command, scene) -> str`
behaviour-neutrally if needed. Then run:

```powershell
cd Dashboard\brain
$env:ROVER="pi"; .\venv\Scripts\python.exe tools\snapshot_prompt.py > logs\pi_prompt_before.txt
```

---

## 3. Changes — brain

### A. Room frame and footprint (MUST) — `room_map.py`, `nav.py`

1. **`nav.py`** (new, stdlib only):
   - `OccupancyGrid(room, clearance_m, cell_m=0.02)` inflates the walls and every obstacle
     polygon by `clearance_m`. It provides `free(x, y) -> bool` and
     `blocker(x, y) -> Optional[str]`, which returns the obstacle name or "the room boundary".
   - `plan_path(grid, start_xy, goal_xy) -> Optional[List[Point]]`: A*, 8-connected, octile
     heuristic, then line-of-sight shortcutting (sampled on the grid) into a minimal list of
     waypoints.
   - `path_length(points)`.
   - `sweep_segment(grid, a, b, step_m=0.02) -> (last_free_point, blocker_or_None)`.
   - Build the grid once per `VirtualRover` (the room is static). A 5 × 4 m room at 2 cm is
     250 × 200 cells, fast enough in pure Python. Only fall back to 5 cm and tell me if A*
     takes more than 1 s.
2. **`room_map.py`**:
   - `_validate` gains a footprint check. For every landmark, report whether its standing spot
     is free on the inflated grid **and** reachable from the start pose.
   - Log an **error** for any that aren't, and **list them to me instead of silently moving
     them**. I expect `window` (y = 3.9, only 0.1 m from the back wall) to fail. Propose a
     corrected coordinate for each failure and wait for confirmation.
   - Optional `where` strings on landmarks and obstacles, printed in `digest_text()` when
     `VIRTUAL_DIGEST_WHERE=1`. A fixture without them must produce byte-identical output.
     Propose plain relative phrases (no numbers) for `room_tour1`'s 6 landmarks and 4
     obstacles and wait for confirmation. With navigation they matter for **ambiguity**
     ("the thing by the window"), not for collisions.

### B. `VirtualRover` (MUST) — `rover.py`

1. **Construction:** build the grid from the footprint config. Keep a lock around `pose`.
   Add `state_snapshot() -> {"pose", "path", "planned_path", "odometer_m", "sim_time_s"}`.
2. **`approach` and `observe`** (the latter still only turns to face the target):
   - resolve the target (keep the existing hallucination path, with `detail` as a dict);
   - `plan_path`, then execute each waypoint as turn-then-drive at the configured rates, honour
     `halt` every tick, and advance the pose along the path;
   - record `planned_path` (waypoints), `path_length_m`, `straight_line_m` (start→goal) and
     `sim_time_s` in the trace record's extras;
   - no path gives `blocked` with `{"reason": "no_path", "target": ...}`.
3. **`move` / `turn`**, literal:
   - parse the magnitude (step field, then text, then default). The metres regex must accept
     "1 m", "1.5 metres", "50 cm" and "half a metre", and `_is_backward` keeps its meaning;
   - `move` uses `sweep_segment`. On a blocker, set the pose to the last free point and return
     `blocked` with `{"reason": "collision", "obstacle": name, "commanded_m": d,
     "travelled_m": t}`;
   - `turn` is a rotation in place and can't collide, because the clearance is the
     half-diagonal;
   - drift applies only if the drift config is non-zero.
4. **Time:** replace `_dwell(step_seconds)` with a simulated-time dwell (sim / time scale). Keep
   `VIRTUAL_STEP_SECONDS` as the dwell for `scan`, `stop` and `report` only.
5. **Trace:** every step record carries `intended`, `pose`, `reason`, `planned_path` (for
   approach), `sim_time_s` and `odometer_m`, so `plot_run.py` can draw it all.
6. **`summary()`** additionally returns `odometer_m`, `sim_time_s`, `collisions`,
   `no_path_count` and `visits` (the ordered list of landmark nodes reached by `approach`).

### C. Planner split (MUST) — `planner.py`, `planner_pi.py`, `planner_virtual.py`

1. **`planner_pi.py`** holds today's `ACTIONS`, the follow_line sentence and today's step-schema
   string, **verbatim**.
2. **`planner_virtual.py`**:
   ```python
   ACTIONS = {
       "approach": "go to a named thing in the room; the rover finds its own way around obstacles",
       "move": "drive straight forward or backward by a distance, with no obstacle avoidance",
       "turn": "rotate on the spot, left or right, by an angle",
       "scan": "sweep the camera around without moving the base",
       "observe": "turn to face a named thing and look at it",
       "stop": "halt",
       "report": "say what was found",
   }
   GUIDANCE = ("To go to a thing, use approach with its exact name from the room list. "
               "Use move and turn only when the command gives explicit directions; then give "
               "every move a distance_m and every turn a degrees value, in the order given.")
   STEP_SCHEMA = ('{"steps": [{"id": 1, "action": "...", "target": "...", '
                  '"distance_m": <number, move only>, "degrees": <number, turn only>}], "notes": "..."}')
   ```
   **If you change this text, tell me.** It goes into Ch3 as the system under test.
3. **`planner.py`**:
   - `_profile()`;
   - the vocabulary block is built exactly as `_VOCABULARY` is today, from the profile;
   - `generate_plan` uses the profile's `STEP_SCHEMA`;
   - keep a module-level `ACTIONS` alias pointing at the pi profile;
   - add `profile_info() -> {"profile", "prompt_sha": sha256(vocab + schema)[:12], "actions"}`;
   - leave `revise_plan` unchanged.

### D. Mission and report (MUST) — `mission_session.py`, `mission.py`, `report.py`

1. **`MissionSession`:** add `rover_summary`, `grounding`, `dialogue_meta` and `usage`, all
   included in `snapshot()`. Read how the mission is built from the dialogue, and copy the
   dialogue's `turn_count`, `capped`, `verified`, `concerns` and replan count into
   `dialogue_meta`.
2. **`run_mission`:**
   - after every `step_done`, if `hasattr(rover, "state_snapshot")`, emit
     `{"type": "rover_state", ...}` for the live map (§4);
   - in the `finally`, before `_finish_report`:
     - `rover_summary = rover.summary()` if the rover has one;
     - `grounding`: every confirmed-plan step with a target, run through `rover.room.resolve`,
       as `{step_id, action, target, resolved, matched, how}`;
     - `usage = providers.usage.snapshot()`.
3. **`report.py`:**
   - `models.planner_profile = profile_info()`.
   - New blocks: `rover_summary`, `grounding`, `dialogue`, `usage_summary` (per role: calls,
     total and mean latency, tokens and USD cost if present), and `grounding_summary`
     (`targets`, `unresolved`, and `out_of_vocab_actions` = actions not in the active profile).
   - **Gate** `no_arrival_check` and `thin_evidence` so they fire only when the plan contains
     `follow_line`.
   - Add uncertainties `plan_target_unresolved`, `out_of_vocab_action`, `collision` (with the
     obstacle) and `no_path`.
   - Every existing Pi field stays.

### E. `main.py` (SHOULD)

- `GET /runtime` returns `{rover, planner_profile, planner{provider,model}, vlm{provider,model},
  scene_source, room_map, robot{length_m,width_m,clearance_m}, drift{frac,deg}, time_scale}`.
  Reuse `report._role_config`.
- `GET /room` returns `room.to_dict()` for the live map. It returns 404 unless the rover is
  virtual.

### F. Usage ledger (MUST) — `providers/usage.py`

- A module-level list behind a lock, with `reset()`, `record(dict)` and `snapshot()`.
- Hook it where `log_completion` gets `latency_s` and the token and cost values; read
  `providers/` to find those. Missing values become `None`.
- Call `usage.reset()` when `/ws/dialogue` creates a **new** session. That assumes one mission
  at a time, which holds for the UI and for the sweep; say so in a comment.
- Fill `OPENAI_COST_PER_1M_*` from OpenAI's **current** pricing page. Don't guess.

### G. `tools/plot_run.py` (SHOULD)

Also draw the inflated obstacles (light), each approach's `planned_path` (thin blue), the rover
footprint rectangle at the start and end poses, and collision points (red ×). The caption gains
odometer, simulated time and collisions.

---

## 4. UI (SHOULD — the Report card also helps the real rover)

1. **`types.ts` / `store.ts`:** add `check`, `warning`, `report` and `rover_state` to
   `MissionEvent`, with reducer state for `checks`, `warnings`, `report` and `roverState`.
   **Read the emitting code in `mission.py` for the exact keys.**
2. **`ExecutionPanel.tsx`:**
   - For virtual runs, a **live map**: an SVG built from `/api/room` showing obstacles, their
     inflated outline, landmarks, the planned path, the travelled path and the footprint at the
     current pose. It updates on each `rover_state`.
   - A **Report** card: outcome, `spoken_summary`, `uncertainties` (prominent), steps,
     `grounding_summary`, `rover_summary` (odometer, simulated time, collisions, visits),
     `usage_summary`, profile and models.
   - A **Checks** list (Pi runs): "check failed" on `_error`, never "not visible".
   - Default the frame source to Off when the rover isn't `pi`.
3. **`App.tsx`:** a header pill such as
   `virtual · profile virtual@<sha> · planner ollama:gemma4:e2b · vlm ollama:qwen2.5vl:3b · robot 0.19×0.16 m`.
4. `npm run typecheck` must be clean.

---

## 5. `.env` (add or confirm)

```
ROVER=virtual
SCENE_SOURCE=room                  # no stray full stop
ALLOW_TEXT_COMMANDS=1
VIRTUAL_DIGEST_WHERE=1
VIRTUAL_ROBOT_LENGTH_M=0.187       # manufacturer spec 187 x 162 mm, confirmed by Utroff
VIRTUAL_ROBOT_WIDTH_M=0.162
VIRTUAL_SAFETY_MARGIN_M=0.05
VIRTUAL_LINEAR_SPEED_MPS=0.17      # matches the real follower's ~17 cm/s placeholder
VIRTUAL_TURN_RATE_DPS=90
VIRTUAL_TIME_SCALE=1               # the sweep overrides this to 20
VIRTUAL_DRIFT_FRAC=0
VIRTUAL_DRIFT_DEG=0
```

