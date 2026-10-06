# Test plans — supervisor feedback build (5–7 Oct 2026)

Spec: [../spec-supervisor-feedback-2026-10-05.md](../spec-supervisor-feedback-2026-10-05.md)
Status: **code was written out in chat, not applied to the repo.** Apply it first (see "Apply order"), then use these plans.

| Plan | Covers | Needs hardware? | When |
| --- | --- | --- | --- |
| [A-context-budget.md](A-context-budget.md) | `tools/context_budget.py`, Ollama overflow behaviour, reduced-`num_ctx` sweep | No | Mon 5 |
| [B-status-lights.md](B-status-lights.md) | `status_light()`, 8 states, light log | Yes (rover on, wheels off the ground ok) | Mon 5 |
| [C-distance-and-hop.md](C-distance-and-hop.md) | C1 sonar 300 mm, C2 go/no-go, C3 `f9`, C4 `_hop_for`, C5 sonar bench, A/B | Yes | Mon 5 (C1/C5) · Tue 6 (C2/C3) · Wed 7 (C4) |
| [D-guidance-reprompt.md](D-guidance-reprompt.md) | `AWAITING_GUIDANCE`, replan, scripted sweeps, real run | Virtual first, then yes | Tue 6 |

## Conventions used in every plan
- Run from `Dashboard/brain/`. Windows: `venv\Scripts\python.exe`. Mac/Pi host: `venv/bin/python`.
- Python 3.9: no `str | None`.
- **Ask Utroff before the first physical run of a session**, and before `docker restart`/`stop`.
- Record results in the table at the bottom of each plan; copy the table into the Ch5 notes.
- ☐ = not done, ☑ = done. Edit in place.

## Apply order (avoids breaking the sweep and the live rover)
1. A (new files only, plus `--num-ctx` in `virtual_sweep.py`). Zero risk to the rover.
2. B (`rover.py`, `rover_pi.py`, `mission.py`, `mission_session.py`, `main.py`, `report.py`). `RGB_ENABLED=0` is the kill switch.
3. D virtual (`planner.py`, `mission.py`, `mission_session.py`, `report.py`, `virtual_sweep.py`) with `GUIDANCE_ENABLED=0` in `.env` until the virtual tests pass.
4. C (`vlm.py`, `mission.py`, `.env`, `tools/f9…`, `tools/f10…`). Keep `HOP_MODE=fixed` until C4 tests pass.

## What was already checked, and what was not
Checked on 2026-10-05 against a **scratch copy** of `Dashboard/brain/` (the repo itself was not touched), Python 3.9, no Ollama, no rover:
- All patched files compile; `tools/context_budget.py`, `f9`, `f10` compile.
- 60+ assertions pass in a mocked run: `_declines`, guidance recovered / declined / exhausted / disabled / timeout / abort-during-wait, `plan_revised` emitted, `rover.halt` not called, light-state timeline, `replan_from_state` on good/bad/non-JSON output, `validate_steps`, `_hop_for` (9 cases), `distance_cm_from` (numeric + bucket + junk), approach loop with the sonar-stop relook (arrives; second consecutive stop blocks; `hops_done`/`hop_src_counts`/`sonar_stops` counted once), the Pi light patterns on fake topics (steady, thinking blink, pre-emption, arrived ×3, awaiting_guidance blink, disabled).
- `virtual_sweep.py --dry-run` accepts `--num-ctx` and `--guidance-script` and rejects `--num-ctx` with cloud.

**Not checked** (this is what the plans are for): anything that needs Ollama (token counts, the overflow probe, the sweeps), anything that needs the rover (LED indices, sonar stop distances, VLM distance accuracy), the dashboard front-end's handling of the new `awaiting_guidance` event / `awaiting_guidance` phase string, and `get_rover()` behaviour from the dialogue path with a real Pi. A first probe of `gemma4:e4b` crashed in CUDA init (see risk #7), so no Ollama numbers exist yet.

## How to apply the code from chat
The chat message has one unified diff per existing file plus three new files. Save each diff block as `<name>.patch` at the repo root, then for each: `git apply --ignore-whitespace --check <name>.patch` then `git apply --ignore-whitespace <name>.patch` (the repo is CRLF, the diffs are not, hence `--ignore-whitespace`). New files go to `Dashboard/brain/tools/`. Add the `.env` block last.

## Known risks found while reading the code (read before the first run)
1. **`rover.halt()` latches `_halted` on the Pi** (`rover_pi.py`). Only `execute_step` clears it. The free-roam approach calls `hop`/`pivot` directly, so after a `halt()` every later hop returns `halted mid-hop`. The §D code therefore does **not** call `halt()` on a blocked step (the base is already stopped: hops and pivots are timed). Do not add it back without clearing `_halted`.
2. **Sonar stop at 300 mm is tight against the ≥ 25 cm acceptance.** Sonar is throttled to 100 ms and two consecutive new readings are required, so worst-case detection lag is ~0.25 s ≈ 5 cm at 20.7 cm/s. Expect stops at ~25–29 cm. If C5 shows < 25 cm, raise `ROVER_PI_SONAR_STOP_MM` to 330, not the lag.
3. **`ROVER_PI_SONAR_CLEAR_MM` must be above the stop value.** It is 300 today; with stop = 300 there is no hysteresis in the line-follow obstacle wait. Set it to 400.
4. **`confirmation.classify` is the wrong tool for "stop/cancel" in guidance.** Its REJECT list contains "no", "wait", "change", "wrong", so "there is no box on the left" would be read as a decline. The §D code uses a narrow `_declines()` instead (a deliberate deviation from the spec text).
5. There is **no existing capability-vocabulary gate** on plans; vocabulary is only checked after the fact in `report._grounding_summary`. §D adds `planner.validate_steps`.
6. `.env` currently has `HOP_CM=50`, `HOP_NEAR_CM=20` (the spec's 30/15 are only code defaults). Record the real values in the A/B table.
7. `gemma4:e4b` can crash on a cold CUDA load (`llama-server process has terminated`). It happened while probing on 2026-10-05. `OllamaProvider` retries; the new tools do too, but if it persists use `gemma4:e2b` for A and say so.
