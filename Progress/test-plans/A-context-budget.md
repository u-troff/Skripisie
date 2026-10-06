# Test plan A — context budget (offline, no hardware)

Spec §A. Files: new `tools/context_budget.py`; one new `--num-ctx` option in `tools/virtual_sweep.py`.
Needs: Ollama running with `gemma4:e4b` and/or `gemma4:e2b` and `qwen2.5vl:3b` pulled. Nothing else (the rover can be off).

## A0. Smoke test (5 min)

☐ `venv\Scripts\python.exe tools\context_budget.py --models gemma4:e2b --rooms room_tour1 --depths 1,10`
Expect: no traceback; a component line (`static`, `room_digest`, `per_instruction_in`, `per_step_out`); two CSV rows.
☐ Run it twice. Numbers must be **identical**. If the second run is lower, Ollama's prompt cache is under-reporting `prompt_eval_count`. Re-run with `--no-cache` (unloads the model each call, slow) and use those numbers.

## A1. Full budget table

☐ `venv\Scripts\python.exe tools\context_budget.py --models gemma4:e4b,gemma4:e2b --vlm-model qwen2.5vl:3b`
Expect:

- `logs/context_budget.csv` with 4 rooms × 7 depths × each model (28 rows per model).
- `logs/context_budget_live.csv` (live-mission table; §A "Live-mission budget").
- Printed per room: components, projected tokens at depth 30, and `D*` for windows 1024/2048/4096/8192 and each model's native window.
  Sanity checks (tick each):
- ☐ `tokens(30) < 8192` for every room (single-shot planning does not overflow at 8192).
- ☐ `per_instruction_in` is roughly 6–10 tokens, `per_step_out` roughly 15–35 (logs-measured ≈ 30; the "ideal JSON" figure will be lower; both are printed).
- ☐ `room_cluttered` digest > `room_studio` digest (more landmarks).
- ☐ Native windows: the tool prints the value read from Ollama (GGUF metadata). **Look the same models up on their model cards and write the source in the table below.** Not assumed. `gpt-5.6-luna` has no local source: fill it in by hand.

## A2. Cloud (approximate)

☐ `venv\Scripts\python.exe -m pip install tiktoken` (optional, dev-only), then re-run A1 with `--cloud-tokenizer o200k_base`.
Expect rows labelled `approx`. Label them approximate in the report. Skip this step if you do not want the extra package; local rows are enough for RQ2.

## A3. Answer the open questions (write the answers in the table)

- **G2 — do VLM frames accumulate?** Read `vlm._ask`: it builds `[user_message(prompt)]` fresh each call, with no history → each VLM call is independent and flat (~1,150 tokens measured by the tool as `per_vlm_frame`). ☐ Confirm the tool's number is within ±15 % of the logs (~1,141).
- **G3 — Ollama overflow behaviour.** ☐ `venv\Scripts\python.exe tools\context_budget.py --probe-overflow --models gemma4:e2b --probe-ctx 1024`
  It sends ~2× `num_ctx` of numbered lines, then asks which first/last line numbers it can see. Record: `prompt_eval_count` (retained tokens, should be ≤ 1024), `done_reason`, and the reply. Interpret:
  - first line reported > 1 and the question is answered → **oldest tokens silently dropped** (head truncation);
  - reply ignores the question → tail was cut;
  - an HTTP error → Ollama refuses (then there is no silent failure mode to look for).
    Also check `%LOCALAPPDATA%\Ollama\server.log` for a `truncating input prompt` line and paste it in the table.

## A4. Validation experiment — reduced `num_ctx`

Goal: predict the depth `D*` where a reduced window overflows, then see if failures begin near it.

1. ☐ Read the predicted `D*` for 1024 and 2048 from the A1 output (use the `gemma4:e4b` row; note whether you used the logs- or the ideal-based `per_step_out`).
2. ☐ Build a deep suite (the stock one stops at depth 12):
   `venv\Scripts\python.exe tools\context_budget.py --write-suite tools\sweeps\commands_ctx_room_tour1.json --room room_tour1 --suite-depths 4,8,12,16,20,24,28,32,36,40`
3. ☐ Dry run: `venv\Scripts\python.exe tools\virtual_sweep.py --conditions local_e4b --commands tools\sweeps\commands_ctx_room_tour1.json --rooms room_tour1 --num-ctx 1024 --repeats 1 --dry-run` (prints the estimate only).
4. ☐ Real runs (each is slow; start with 1024):
   - `… --num-ctx 1024 --repeats 3 --out tools\sweeps\ctx_1024.csv`
   - `… --num-ctx 2048 --repeats 3 --out tools\sweeps\ctx_2048.csv`
   - control: `… --num-ctx 8192 --repeats 3 --out tools\sweeps\ctx_8192.csv` (expected: no breakdown up to depth 40; if there is one, it is not a window effect).
5. ☐ Analyse: `venv\Scripts\python.exe tools\context_budget.py --analyse tools\sweeps\ctx_1024.csv --model gemma4:e4b --room room_tour1` (repeat for 2048, 8192).
   Prints per depth: runs, match rate, failure modes (`malformed_json`, `dropped_head`, `dropped_tail`, `dropped_middle_or_wrong`, `no_visits`, `planner_error`), the **observed** breakdown depth (first depth with match rate < 50 %) next to the **predicted** `D*`.
   Pass: at least one reduced-`num_ctx` sweep with observed vs predicted written down. A mismatch is a valid finding; report the direction and a reason (e.g. the model emits more tokens per step than the ideal JSON; the ambiguity/verify calls share the reduced window).
   Caveat to state in Ch5: both roles are set to the same reduced `num_ctx` (otherwise Ollama reloads the model between calls), so the ambiguity check and plan verification also run in the small window; `plan_notes` shows whether the planner itself failed.

## Results to record

| Item                                                         | Value | Source / note                    |
| ------------------------------------------------------------ | ----- | -------------------------------- |
| static tokens (virtual profile)                              |       |                                  |
| room digest tokens (tour1 / studio / cluttered / loft)       |       |                                  |
| per_instruction_in                                           |       |                                  |
| per_step_out (ideal / logs)                                  |       |                                  |
| per_clarification_turn                                       |       |                                  |
| per_guidance_turn                                            |       | needs §D`build_replan_prompt` |
| per_vlm_frame                                                |       |                                  |
| native window gemma4:e2b / e4b / qwen2.5vl:3b / gpt-5.6-luna |       | model card URL                   |
| Ollama overflow behaviour (G3)                               |       |                                  |
| D* predicted vs observed @1024 / @2048                       |       |                                  |
| failure mode observed                                        |       |                                  |
