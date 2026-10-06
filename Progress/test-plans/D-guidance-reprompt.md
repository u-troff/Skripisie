# Test plan D — guidance reprompt ("I have stopped, what should I do next?")

Spec §D. Files: `mission_session.py` (new phase + fields), `planner.py` (`build_replan_prompt`, `replan_from_state`, `validate_steps`), `mission.py` (`_guidance_loop`, `handle_guidance`), `main.py` (route replies), `report.py` (outcome classes), `tools/virtual_sweep.py` (`--guidance-script`).
Scope guard: the reprompt fires **only on a `blocked` physical/search failure**, never to resolve ambiguity. Reported as failure recovery.

Switches: `GUIDANCE_ENABLED` (1/0), `MAX_GUIDANCE_TURNS` (3), `GUIDANCE_TIMEOUT_S` (120). `virtual_sweep.py` forces `GUIDANCE_ENABLED=0` unless `--guidance-script` is given, so the existing RQ2 sweeps and probe chains behave exactly as before.

## D0. Unit-level, no model (10 min)
☐ `_declines` table (from a shell: `python -c "import mission; …"`):

| answer | `mission._declines(...)` |
| --- | --- |
| `stop` / `cancel` / `never mind` / `give up` | True |
| `no` | True (≤ 3 words and a REJECT keyword) |
| `it is to your left` | False |
| `there is no box on the left, try the couch` | False (long utterance, not a bare "no") |
| `forget it` | True |
☐ `planner.validate_steps([])` → `"no steps"`; `[{"id":1,"action":"fly","target":"x"}]` → vocabulary error; `[{"id":1,"action":"approach","target":"couch"}]` → `None` (with `PLANNER_PROFILE=virtual`).
☐ `planner.build_replan_prompt(...)` snapshot: print it once and read it. It must contain the original command, completed steps, the failed step + reason, the guidance conversation, ≤ `PERCEPTION_DIGEST_KEEP` digest lines, and the vocabulary.

## D1. Virtual, scripted (no hardware) — the bulk of the testing
Make a script `tools\sweeps\guidance_script.json` (command → ordered answers; the answer list is consumed one per question; when empty the harness answers "stop"):
```
{
  "Go to the fridge.": ["go to the couch instead"],
  "Go to the fridge, then the desk.": ["I do not know", "it is not here", "still nothing"],
  "Go to the toaster.": ["stop"]
}
```
and a one-command-per-line suite `tools\sweeps\guidance_suite.json` with those three commands (`kind: "probe"`, no `expected_visits` needed):
```
[{"command":"Go to the fridge.","depth":1,"kind":"probe"},
 {"command":"Go to the fridge, then the desk.","depth":2,"kind":"probe"},
 {"command":"Go to the toaster.","depth":1,"kind":"probe"}]
```
☐ `venv\Scripts\python.exe tools\virtual_sweep.py --conditions local_e4b --commands tools\sweeps\guidance_suite.json --rooms room_tour1 --repeats 3 --guidance-script tools\sweeps\guidance_script.json --out tools\sweeps\guidance_test.csv`
Expected (tick; the planner is stochastic, so count over 3 repeats):

| Command | Expected outcome | Check in CSV |
| --- | --- | --- |
| fridge + "go to the couch instead" | `completed_after_guidance`, `guidance_turns=1`, `recovered=True` | visits include `couch` |
| fridge/desk + 3 non-answers | `halted_after_guidance`, `guidance_turns=3`, `halt_reason=guidance_exhausted` | exactly 3 questions in `guidance_log` |
| toaster + "stop" | `halted_after_guidance`, `halt_reason=guidance_declined`, `guidance_turns=1` | no replan call after the decline |
☐ A replanned plan that names a non-existent target is blocked again and asks again (does not loop silently); a replan that returns unparseable/invalid JSON counts as a used turn and re-asks ("I could not turn that into a plan").
☐ `GUIDANCE_ENABLED=0` regression: run the stock suite for one room, `--depth-max 6 --repeats 1`, **without** `--guidance-script`: results equal an older CSV for the same rows (same `outcome` strings: `completed`/`halted`; `guidance_turns` blank/0). Probe chains still stop at the first blocked step.
☐ Context cost: re-run `tools\context_budget.py` and read `per_guidance_turn` (now computed from `build_replan_prompt`).

## D2. Live, no rover motion (backend + dashboard, `ROVER=virtual`)
☐ `GUIDANCE_ENABLED=1`. Give the dashboard "go to the fridge". Expected: blocked → the question is spoken/shown (`awaiting_guidance` event), phase `awaiting_guidance`. Reply by typing (needs `ALLOW_TEXT_COMMANDS=1`; the client sends `guidance_text`) or by voice (the client sends `revision_audio`; the backend routes it to guidance while the phase is `awaiting_guidance`). Answer "go to the couch" → `plan_revised` event, mission continues, ends `completed_after_guidance`.
☐ Silence: no reply for `GUIDANCE_TIMEOUT_S` (set 10 for the test) → `halted`, `halt_reason=guidance_timeout`, and the report/summary is still produced.
☐ Abort during guidance (dashboard abort) → final phase `aborted`, **not** `halted`; no replan call after it.
☐ While waiting, frames keep arriving on `/ws/execution`: no revision proposals fire (`ingest_frame` only revises in `executing`). Confirm no `awaiting_revision` events during guidance.

## D3. Real rover (ask first; Tue 6 after virtual passes)
☐ Free-roam, `NAV_MODE=free`. Command "go to the orange box" with the box **removed** from the room.
Expect: search sweeps → `blocked target_not_found` → light `awaiting_guidance` (B) → the rover speaks "I stopped because I could not find the orange box after searching the room. What should I do next?" → rover **does not move** while waiting (check `hop`/`pivot` aren't called: `brain.log` quiet).
☐ Put the box back on the floor to the rover's left, answer by voice "it is to your left". Expected: a validated new plan (`plan_revised`), the search runs again and arrives; final outcome `completed_after_guidance`; report has `guidance_log` with the question, answer, `replanned=true`.
☐ Repeat 3×, note STT transcripts (they are in the log) and any case where the replan ignored the hint. That is evidence, not a bug.
☐ The Pi `_halted` latch (README risk #1): after the recovery the rover hops normally (no `halted mid-hop` result). If you see it, `halt()` has been called somewhere in the guidance path: remove it.
☐ `MAX_GUIDANCE_TURNS` on hardware: give 3 useless answers → clean `halted_after_guidance`, light `blocked`.

## D4. Reporting checks
☐ Run log contains: `outcome` (taxonomy: `completed`, `completed_after_guidance`, `halted_after_guidance`, `halted`), `guidance_turns`, `guidance_log`, `recovered`, `halt_reason`.
☐ The dashboard Logs page still lists the run (it reads `outcome` as a plain string).
☐ Never merged: confirm a recovered run is counted separately from `completed` in your analysis script.

## Results to record
| Item | Value |
| --- | --- |
| Virtual: recovered / attempted (fridge→couch) | |
| Virtual: cap and decline paths clean | |
| Live voice: question wording OK? | |
| Real: recovered / attempted | |
| STT failure cases | |
| Replan failures (invalid/unparseable) | |
| `per_guidance_turn` tokens | |
