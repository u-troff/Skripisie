#!/usr/bin/env python3
"""Context budget (spec-supervisor-feedback-2026-10-05.md section A). Offline: no rover.

Run from Dashboard/brain/ (Windows: venv\\Scripts\\python.exe, Mac: venv/bin/python):

    python tools/context_budget.py --models gemma4:e4b,gemma4:e2b --vlm-model qwen2.5vl:3b
    python tools/context_budget.py --probe-overflow --models gemma4:e2b --probe-ctx 1024
    python tools/context_budget.py --write-suite tools/sweeps/commands_ctx_room_tour1.json --room room_tour1
    python tools/context_budget.py --analyse tools/sweeps/ctx_1024.csv --model gemma4:e4b --room room_tour1

Token counts come from Ollama (prompt_eval_count, num_predict=1), so they include
the chat-template overhead. Cloud counts use tiktoken if installed and are
labelled APPROXIMATE. Native windows are read from the local GGUF metadata; the
model-card value and its source must be written in by hand (NATIVE_CARD).
"""

import argparse
import csv
import glob
import json
import os
import statistics
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BRAIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BRAIN_DIR))
os.environ["ROVER"] = "virtual"
os.environ["PLANNER_PROFILE"] = "virtual"  # the prompt planner_virtual.py defines

import config  # noqa: E402
import planner  # noqa: E402
import room_map  # noqa: E402
import vlm  # noqa: E402
from providers.base import Completion  # noqa: E402

DEPTHS = (1, 5, 10, 15, 20, 25, 30)
WINDOWS = (1024, 2048, 4096, 8192)
LOGS = BRAIN_DIR / "logs"
BUDGET_CSV = LOGS / "context_budget.csv"
LIVE_CSV = LOGS / "context_budget_live.csv"

# model tag -> (tokens, "source URL / card section"). Fill from the model cards;
# do not guess. gpt-5.6-luna has no local source at all.
NATIVE_CARD: Dict[str, Tuple[int, str]] = {}

SAMPLE_DIGEST_LINE = "A grey couch against the left wall with a low table in front of it; the floor ahead is clear."
SAMPLE_Q = "Which one do you mean, the red box or the blue box?"
SAMPLE_A = "The red box, the one nearest the window."
SAMPLE_GUIDANCE_Q = ("I stopped because I could not find the orange box after searching the room. "
                     "What should I do next?")
SAMPLE_GUIDANCE_A = "It is on the floor to your left, next to the couch."

FIELDS = ["model", "counter", "approx", "room", "depth", "static", "room_digest",
          "per_instruction_in", "per_step_out_ideal", "per_step_out_logs",
          "per_clarification_turn", "per_guidance_turn",
          "measured_prompt_tokens", "measured_ideal_out_tokens", "projected_tokens",
          "pct_of_8192"]


# -- token counters ----------------------------------------------------------
def _optional_bool(name: str) -> Optional[bool]:
    raw = (config.get(name) or "").strip().lower()
    return None if not raw else raw in ("1", "true", "yes", "on")


class OllamaCounter:
    approx = False

    def __init__(self, model: str, num_ctx: int = 4096, no_cache: bool = False):
        import ollama
        self.model, self.num_ctx, self.no_cache = model, num_ctx, no_cache
        self.label = "ollama:" + model
        self._client = ollama.Client(host=config.get("OLLAMA_HOST") or None)
        self._think = _optional_bool("OLLAMA_THINK") if model.startswith("gemma") else None

    def chat(self, text: str, images: Optional[list] = None, num_predict: int = 1):
        message: Dict[str, Any] = {"role": "user", "content": text}
        if images:
            message["images"] = images
        kwargs: Dict[str, Any] = {
            "model": self.model, "messages": [message],
            "options": {"num_ctx": self.num_ctx, "num_predict": num_predict},
            # keep_alive=0 unloads after every call: slow, but defeats prompt-cache under-reporting.
            "keep_alive": 0 if self.no_cache else (config.get("OLLAMA_KEEP_ALIVE") or "10m"),
        }
        if self._think is not None:
            kwargs["think"] = self._think
        for attempt in range(3):
            try:
                return self._client.chat(**kwargs)
            except Exception as exc:  # gemma4:e4b can die on a cold CUDA init
                if attempt < 2 and "llama-server" in str(exc):
                    time.sleep(2)
                    continue
                raise

    def count(self, text: str, images: Optional[list] = None) -> int:
        n = getattr(self.chat(text, images), "prompt_eval_count", None)
        if n is None:
            raise RuntimeError("ollama returned no prompt_eval_count")
        if n >= self.num_ctx - 2:
            raise RuntimeError("prompt reached num_ctx=%d, so the count is truncated; raise --count-ctx"
                               % self.num_ctx)
        return int(n)


class TiktokenCounter:
    approx = True

    def __init__(self, encoding: str):
        import tiktoken
        self._enc = tiktoken.get_encoding(encoding)
        self.label = "tiktoken:%s (APPROXIMATE, no chat-template overhead)" % encoding

    def count(self, text: str, images: Optional[list] = None) -> int:
        return len(self._enc.encode(text))


# -- synthetic chains --------------------------------------------------------
def chain_command(names: List[str], n: int) -> str:
    """'Go to the A, then the B, ..., and finally the Z.' (repeats get 'again')."""
    picks = [names[i % len(names)] for i in range(n)]
    if n == 1:
        return "Go to the %s." % picks[0]
    seen = set()
    parts = []
    for i, name in enumerate(picks):
        text = "the %s" % name + (" again" if name in seen else "")
        seen.add(name)
        parts.append("Go to " + text if i == 0 else ("and finally " + text if i == n - 1 else "then " + text))
    return ", ".join(parts) + "."


def ideal_output(names: List[str], n: int) -> str:
    steps = [{"id": i + 1, "action": "approach", "target": names[i % len(names)]} for i in range(n)]
    return json.dumps({"steps": steps, "notes": ""})


def landmark_names(room) -> List[str]:
    return [l.name for l in room.landmarks]


# -- capturing real prompts without refactoring the app ----------------------
class _FakePlanner:
    name = "fake"
    model = "fake"

    def __init__(self):
        self.prompts: List[str] = []

    def complete(self, messages, image=None, json_mode=False):
        self.prompts.append(messages[-1]["content"])
        return Completion(text='{"change": false, "reason": "", "steps": []}',
                          provider="fake", model="fake", latency_s=0.0)


def capture_revise_prompt(plan: dict, remaining: list, digest: list, command: str) -> str:
    fake, real = _FakePlanner(), planner.get_provider
    planner.get_provider = lambda role: fake
    try:
        planner.revise_plan(plan, remaining, digest, command)
    finally:
        planner.get_provider = real
    return fake.prompts[-1]


def capture_vlm_prompt(fn, *args) -> str:
    got: List[str] = []
    real = vlm._ask
    vlm._ask = lambda stage, prompt, image: (got.append(prompt) or {})
    try:
        fn(*args)
    finally:
        vlm._ask = real
    return got[0]


# -- measurement -------------------------------------------------------------
def measure_room(counter, room, depths: List[int]) -> Dict[str, Any]:
    names, digest = landmark_names(room), room.digest_text()
    static = counter.count(planner.build_plan_prompt("", ""))
    room_digest = counter.count(planner.build_plan_prompt("", digest)) - static
    out0 = counter.count(ideal_output(names, 0))
    rows = []
    for n in depths:
        rows.append({
            "depth": n,
            "prompt": counter.count(planner.build_plan_prompt(chain_command(names, n), digest)),
            "out": counter.count(ideal_output(names, n)) - out0,
        })
    span = max(1, rows[-1]["depth"] - rows[0]["depth"])
    cmd5 = chain_command(names, 5)
    base5 = counter.count(planner.build_plan_prompt(cmd5, digest))
    clar = counter.count(planner.build_plan_prompt(
        cmd5 + "\nClarification \u2014 %s %s" % (SAMPLE_Q, SAMPLE_A), digest)) - base5
    return {
        "static": static, "room_digest": room_digest,
        "per_instruction_in": round((rows[-1]["prompt"] - rows[0]["prompt"]) / span, 2),
        "per_step_out_ideal": round((rows[-1]["out"] - rows[0]["out"]) / span, 2),
        "per_clarification_turn": clar, "rows": rows,
    }


def _guidance_log(k: int) -> List[dict]:
    return [{"reason": "target_not_found", "question": SAMPLE_GUIDANCE_Q,
             "answer": SAMPLE_GUIDANCE_A}] * k


def per_guidance_turn(counter, room) -> Optional[int]:
    build = getattr(planner, "build_replan_prompt", None)  # exists once section D is applied
    if build is None:
        return None
    names = landmark_names(room)
    cmd, step = chain_command(names, 5), {"id": 1, "action": "approach", "target": names[0]}

    def tokens(k: int) -> int:
        return counter.count(build(cmd, [], step, "target_not_found", _guidance_log(k), [],
                                   room.digest_text()))
    return tokens(2) - tokens(1)


def measure_live(counter, room, depths: List[int]) -> List[dict]:
    """Prompts that exist during a mission: revise_plan, and the section-D replan."""
    names, scene = landmark_names(room), room.digest_text()
    digest = [SAMPLE_DIGEST_LINE] * config.get_int("PERCEPTION_DIGEST_KEEP", 6)
    build = getattr(planner, "build_replan_prompt", None)
    max_turns = config.get_int("MAX_GUIDANCE_TURNS", 3)
    out = []
    for n in depths:
        steps = [{"id": i + 1, "action": "approach", "target": names[i % len(names)]} for i in range(n)]
        cmd = chain_command(names, n)
        row: Dict[str, Any] = {"room": room.name, "depth": n, "digest_lines": len(digest),
                               "revise_tokens": counter.count(
                                   capture_revise_prompt({"steps": steps}, steps, digest, cmd))}
        if build is not None:
            failed = steps[min(n // 2, n - 1)]
            for g in range(max_turns + 1):
                row["replan_g%d" % g] = counter.count(
                    build(cmd, steps[:n // 2], failed, "target_not_found", _guidance_log(g), digest, scene))
        out.append(row)
    return out


def measure_vlm_frame(model: str, frame: Path, count_ctx: int, no_cache: bool) -> Dict[str, int]:
    import providers
    os.environ["VLM_PROVIDER"], os.environ["VLM_MODEL_OLLAMA"] = "ollama", model
    providers.reset_cache()
    prompt = capture_vlm_prompt(vlm.locate_target, b"", "orange box")
    counter = OllamaCounter(model, num_ctx=count_ctx, no_cache=no_cache)
    text_only = counter.count(prompt)
    with_image = counter.count(prompt, images=[frame.read_bytes()])
    return {"text_only": text_only, "with_image": with_image, "image": with_image - text_only}


def logs_per_step_out(model: str) -> Tuple[Optional[float], int]:
    """Completion tokens per plan step from past runs (single planner call, so no revisions)."""
    vals = []
    for path in glob.glob(str(LOGS / "mission_*.json")):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        usage = ((data.get("usage_summary") or {}).get("planner")) or {}
        used = ((data.get("models") or {}).get("planner") or {}).get("model")
        steps = len((data.get("confirmed_plan") or {}).get("steps") or [])
        if used == model and usage.get("calls") == 1 and steps and usage.get("completion_tokens_total"):
            vals.append(usage["completion_tokens_total"] / steps)
    return (round(statistics.median(vals), 2) if vals else None), len(vals)


def native_window_local(model: str) -> Optional[int]:
    try:
        import ollama
        info = ollama.Client(host=config.get("OLLAMA_HOST") or None).show(model)
        mi = getattr(info, "modelinfo", None) or (info.get("modelinfo") if isinstance(info, dict) else None) or {}
        for key, value in mi.items():
            if str(key).endswith(".context_length"):
                return int(value)
    except Exception:
        pass
    return None


def depth_at(window: int, static: float, digest: float, per_in: float, per_out: float) -> Optional[int]:
    """tokens(N) = static + digest + N*(per_in + per_out); the N where it reaches `window`."""
    per = per_in + per_out
    return max(0, int((window - static - digest) // per)) if per > 0 else None


# -- modes -------------------------------------------------------------------
def _rooms(arg: str) -> list:
    names = (sorted(Path(p).stem for p in glob.glob(str(BRAIN_DIR / "rooms" / "*.json")))
             if arg == "all" else [r.strip() for r in arg.split(",") if r.strip()])
    return [room_map.load_room("rooms/%s.json" % n) for n in names]


def run_budget(args) -> None:
    depths = [int(d) for d in args.depths.split(",")]
    if len(depths) < 2:
        sys.exit("--depths needs at least two values (the per-step slopes are differences)")
    rooms = _rooms(args.rooms)
    counters: List[Tuple[Any, str]] = []
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        counters.append((OllamaCounter(model, num_ctx=args.count_ctx, no_cache=args.no_cache), model))
    if args.cloud_tokenizer:
        try:
            counters.append((TiktokenCounter(args.cloud_tokenizer), "gpt-5.6-luna"))
        except Exception as exc:
            print("cloud tokenizer unavailable (%s) - skipping" % exc)

    LOGS.mkdir(exist_ok=True)
    out_rows, live_rows = [], []
    for counter, model in counters:
        logs_out, n_logs = logs_per_step_out(model)
        native = native_window_local(model) if not counter.approx else None
        card = NATIVE_CARD.get(model)
        windows = list(WINDOWS) + ([native] if native else []) + ([card[0]] if card else [])
        print("\n=== %s  (%s)%s" % (model, counter.label, "  APPROXIMATE" if counter.approx else ""))
        print("native window: local GGUF=%s  model card=%s" % (
            native or "n/a", ("%d (%s)" % card) if card else "NOT FILLED IN - add to NATIVE_CARD with its source"))
        print("per_step_out from logs: %s (n=%d runs)" % (logs_out, n_logs))
        for room in rooms:
            try:
                comp = measure_room(counter, room, depths)
                guid = per_guidance_turn(counter, room) if not counter.approx else None
                if not counter.approx:
                    live_rows.extend(dict(r, model=model) for r in measure_live(counter, room, depths))
            except Exception as exc:
                print("  %s: FAILED (%s) - skipping this room/model" % (room.name, exc))
                continue
            per_out = logs_out if logs_out else comp["per_step_out_ideal"]
            for r in comp["rows"]:
                projected = comp["static"] + comp["room_digest"] + r["depth"] * (comp["per_instruction_in"] + per_out)
                out_rows.append({
                    "model": model, "counter": counter.label, "approx": counter.approx, "room": room.name,
                    "depth": r["depth"], "static": comp["static"], "room_digest": comp["room_digest"],
                    "per_instruction_in": comp["per_instruction_in"],
                    "per_step_out_ideal": comp["per_step_out_ideal"], "per_step_out_logs": logs_out,
                    "per_clarification_turn": comp["per_clarification_turn"], "per_guidance_turn": guid,
                    "measured_prompt_tokens": r["prompt"], "measured_ideal_out_tokens": r["out"],
                    "projected_tokens": round(projected), "pct_of_8192": round(100.0 * projected / 8192, 1)})
            dstar = ", ".join("%d->%s" % (w, depth_at(w, comp["static"], comp["room_digest"],
                                                       comp["per_instruction_in"], per_out)) for w in windows)
            print("  %-15s static=%d digest=%d in/step=%s out/step=%s(ideal %s) clar=%d guid=%s | D*: %s" % (
                room.name, comp["static"], comp["room_digest"], comp["per_instruction_in"], per_out,
                comp["per_step_out_ideal"], comp["per_clarification_turn"], guid, dstar))

    with BUDGET_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(out_rows)
    print("\nwrote %s (%d rows)" % (BUDGET_CSV, len(out_rows)))
    if live_rows:
        keys = sorted({k for r in live_rows for k in r}, key=lambda k: (k not in ("model", "room", "depth"), k))
        with LIVE_CSV.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=keys)
            writer.writeheader()
            writer.writerows(live_rows)
        print("wrote %s (%d rows)" % (LIVE_CSV, len(live_rows)))

    if args.vlm_model:
        frames = glob.glob(str(LOGS / "frames" / "**" / "*.jpg"), recursive=True)
        frame = Path(args.frame) if args.frame else (Path(frames[0]) if frames else None)
        if frame is None:
            print("no frame found for per_vlm_frame; pass --frame")
        else:
            res = measure_vlm_frame(args.vlm_model, frame, 8192, args.no_cache)
            print("\nper_vlm_frame (%s, %s): total=%d (text %d + image %d)" % (
                args.vlm_model, frame.name, res["with_image"], res["text_only"], res["image"]))
            print("Live missions: each VLM call is independent (vlm._ask builds a fresh one-message "
                  "request), so the VLM cost is FLAT per call and never accumulates.")


def probe_overflow(args) -> None:
    """What does Ollama do when prompt > num_ctx? Evidence for spec open question G3."""
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        small = OllamaCounter(model, num_ctx=args.probe_ctx, no_cache=args.no_cache)
        big = OllamaCounter(model, num_ctx=args.probe_ctx * 4, no_cache=args.no_cache)
        lines = ["L%04d: the quick brown fox jumps over the lazy dog" % i
                 for i in range(1, args.probe_ctx * 2 // 12 + 1)]
        text = ("\n".join(lines) + "\nAbove are numbered lines. Reply ONLY with JSON "
                '{"first_line": <number of the FIRST L-line you can see>, '
                '"last_line": <number of the LAST L-line you can see>}.')
        true_tokens = big.count(text)
        resp = small.chat(text, num_predict=48)
        print("\n=== %s  num_ctx=%d" % (model, args.probe_ctx))
        print("prompt really has        : %d tokens (%d lines, first=L0001 last=L%04d)" % (
            true_tokens, len(lines), len(lines)))
        print("prompt_eval_count @ctx   : %s" % getattr(resp, "prompt_eval_count", None))
        print("done_reason              : %s" % getattr(resp, "done_reason", None))
        print("model reply              : %r" % resp["message"]["content"][:200])
        print("Read it: first_line > 1 and a sensible JSON reply = oldest tokens silently dropped; "
              "no reply to the question = the tail was cut. Also grep Ollama's server.log for 'truncating'.")


def write_suite(args) -> None:
    room = _rooms(args.room)[0]
    names = landmark_names(room)
    entries = [{"command": chain_command(names, n), "depth": n, "kind": "approach",
                "expected_visits": [names[i % len(names)] for i in range(n)],
                "expected_final": None, "probe": None}
               for n in [int(d) for d in args.suite_depths.split(",")]]
    Path(args.write_suite).write_text(json.dumps(entries, indent=1), encoding="utf-8")
    print("wrote %s (%d entries, room %s)" % (args.write_suite, len(entries), room.name))


def classify(row: Dict[str, str]) -> str:
    notes = row.get("plan_notes") or ""
    if "unparseable" in notes:
        return "malformed_json"
    if "planner unavailable" in notes or "planner failed" in notes:
        return "planner_error"
    if row.get("error"):
        return "run_error"
    try:
        actual = json.loads(row.get("visits_actual") or "[]")
        expected = json.loads(row.get("expected_visits") or "[]")
    except ValueError:
        return "unscored"
    if actual == expected:
        return "ok"
    if not actual:
        return "no_visits"
    if len(actual) < len(expected):
        if actual == expected[:len(actual)]:
            return "dropped_tail"
        if actual == expected[len(expected) - len(actual):]:
            return "dropped_head"
        return "dropped_middle_or_wrong"
    return "wrong_or_extra"


def analyse(args) -> None:
    with open(args.analyse, newline="", encoding="utf-8") as handle:
        rows = [r for r in csv.DictReader(handle) if r.get("depth") and r.get("room") == args.room]
    comp = None
    if BUDGET_CSV.exists():
        with BUDGET_CSV.open(newline="", encoding="utf-8") as handle:
            comp = next((r for r in csv.DictReader(handle)
                         if r["model"] == args.model and r["room"] == args.room), None)
    by_ctx: Dict[str, List[dict]] = {}
    for r in rows:
        by_ctx.setdefault(r.get("planner_num_ctx") or "?", []).append(r)
    for ctx, group in sorted(by_ctx.items()):
        print("\n== planner num_ctx=%s  (%d runs)" % (ctx, len(group)))
        by_depth: Dict[int, List[dict]] = {}
        for r in group:
            by_depth.setdefault(int(r["depth"]), []).append(r)
        broke = None
        for depth in sorted(by_depth):
            modes = [classify(r) for r in by_depth[depth]]
            ok = modes.count("ok") / len(modes)
            print("  depth %2d  runs %2d  ok %3.0f%%  %s" % (depth, len(modes), 100 * ok, dict(Counter(modes))))
            if broke is None and ok < 0.5:
                broke = depth
        predicted = None
        if comp and ctx.isdigit():
            per_out = float(comp["per_step_out_logs"] or comp["per_step_out_ideal"])
            predicted = depth_at(int(ctx), float(comp["static"]), float(comp["room_digest"]),
                                 float(comp["per_instruction_in"]), per_out)
        print("  observed breakdown depth (<50%% ok): %s   predicted D*: %s" % (broke, predicted))


def main() -> None:
    os.chdir(str(BRAIN_DIR))
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--models", default=config.get("PLANNER_MODEL_OLLAMA", "gemma4:e4b") + ",gemma4:e2b")
    p.add_argument("--vlm-model", default="")
    p.add_argument("--frame", default="")
    p.add_argument("--rooms", default="all")
    p.add_argument("--depths", default=",".join(str(d) for d in DEPTHS))
    p.add_argument("--count-ctx", type=int, default=4096)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--cloud-tokenizer", default="")
    p.add_argument("--probe-overflow", action="store_true")
    p.add_argument("--probe-ctx", type=int, default=1024)
    p.add_argument("--write-suite", default="")
    p.add_argument("--room", default="room_tour1")
    p.add_argument("--suite-depths", default="4,8,12,16,20,24,28,32,36,40")
    p.add_argument("--analyse", default="")
    p.add_argument("--model", default="gemma4:e4b")
    # commands copied out of chat can carry an invisible zero-width space on the last
    # argument (-> 'invalid model name' / int('40' + U+200B)); drop them
    args = p.parse_args([re.sub('[\u200b-\u200d\u2060\ufeff]', '', a) for a in sys.argv[1:]])
    args.models = ",".join(dict.fromkeys(m for m in args.models.split(",") if m))  # de-dup, keep order
    if args.probe_overflow:
        probe_overflow(args)
    elif args.write_suite:
        write_suite(args)
    elif args.analyse:
        analyse(args)
    else:
        run_budget(args)


if __name__ == "__main__":
    main()
