#!/usr/bin/env python3
"""Local-vs-cloud sweep over the virtual rover: the same test commands, run
once per provider condition, driven through the real production pipeline
(pipeline.py -> mission.py -> report.py) instead of a reimplementation, so
the numbers are exactly what a live dashboard run would have produced.

    venv\\Scripts\\python.exe tools\\virtual_sweep.py
    venv\\Scripts\\python.exe tools\\virtual_sweep.py --conditions cloud,local --repeats 3
    venv\\Scripts\\python.exe tools\\virtual_sweep.py --commands tools\\sweeps\\my_commands.json

Writes one row per (condition, command, repeat) to a timestamped CSV in
tools/sweeps/, flushed after every row so a crash partway through doesn't
lose what already ran. See
Progress/spec-planner-profiles-and-virtual-sweep.md (RQ2: how many chained
instructions before the plan breaks down; local-vs-cloud for the reasoning
layer).

Confirmation: every run auto-confirms with "yes" (recorded as
auto_confirmed=True in the CSV) — there is no human in the loop.
Drift: forced to 0 regardless of .env, so every collision/no_path in the
sweep comes from the plan, not from simulated wheel slip. Pass --allow-drift
to keep .env's VIRTUAL_DRIFT_FRAC/DEG instead (the one drift side-experiment,
§6 P9 in the spec).

RQ2 scoring (tools/sweeps/commands_depth.json, built by
tools/gen_commands_depth.py): each suite entry may be a plain string or an
object with depth, kind, expected_visits, expected_final and probe. The
expected_* ground truth comes from executing the ideal steps on the same
VirtualRover, so a perfect planner scores 100%. Scoring columns are appended
after the original ones: visits_match is an exact, ordered comparison of
rover.summary()["visits"] with expected_visits; first_divergence is the
0-based index of the first wrong or missing visit. Probe chains expect only the
visits made BEFORE the bad step, because mission.py stops on the first blocked
step.

VLM role: in virtual runs it is text-only (SCENE_SOURCE=room, no image), so
pointing both roles at one model (local_e4b / local_e2b) changes only the
planner's behaviour here; the VLM role merely rereads the same text digest.
The sweep sets ROVER=virtual and SCENE_SOURCE=room itself.

Sampling: Ollama temperature/seed are NOT pinned by the provider (only
num_ctx, repeat_penalty and num_predict are), so repeats are genuinely
stochastic; the temperature/seed columns are blank for Ollama for that reason.
"""

import argparse
import asyncio
import csv
import json
import logging
import math
import os
import statistics
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

BRAIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BRAIN_DIR))

# tts.py imports piper at module level, and mission.py imports tts.py at
# module level, so merely `import mission` fails if piper-tts isn't
# installed. That's a pre-existing gap in this venv unrelated to the sweep —
# the sweep has no use for synthesized audio — so stub piper out rather than
# requiring an install just to import mission.py. A real piper install, if
# present, is used untouched.
try:
    import piper  # noqa: F401
except ImportError:
    import types

    _fake_piper = types.ModuleType("piper")

    class _NoVoice:
        @classmethod
        def load(cls, *_args, **_kwargs):
            return cls()

        def synthesize(self, *_args, **_kwargs):
            return iter([])

    _fake_piper.PiperVoice = _NoVoice
    sys.modules["piper"] = _fake_piper


# -- conditions --------------------------------------------------------------
# name -> env overrides applied for every run under that condition. Roles are
# independent (see .env's own comment), so "deepseek" is planner-only cloud
# with VLM staying local, since DeepSeek has no vision.
#
# The real model env vars (providers/factory.py::_resolve) are
# PLANNER_MODEL_<PROVIDER> / VLM_MODEL_<PROVIDER>, e.g. PLANNER_MODEL_OLLAMA;
# the legacy PLANNER_MODEL / VLM_MODEL are only a fallback. OLLAMA_KEEP_ALIVE is
# read by factory.py and passed to every ollama chat call so the one resident
# model is not unloaded between planner and VLM calls (and equal num_ctx on both
# roles means Ollama has no reason to reload it either).
_LOCAL_COMMON = {"OLLAMA_KEEP_ALIVE": "30m", "PLANNER_NUM_CTX": "8192", "VLM_NUM_CTX": "8192"}


def _single_model(model: str) -> Dict[str, str]:
    env = {"PLANNER_PROVIDER": "ollama", "VLM_PROVIDER": "ollama",
           "PLANNER_MODEL_OLLAMA": model, "VLM_MODEL_OLLAMA": model}
    env.update(_LOCAL_COMMON)
    return env


CONDITIONS: Dict[str, Dict[str, str]] = {
    "cloud": {"PLANNER_PROVIDER": "openai", "VLM_PROVIDER": "openai"},
    "local": {"PLANNER_PROVIDER": "ollama", "VLM_PROVIDER": "ollama"},
    "local_e4b": _single_model("gemma4:e4b"),
    "local_e2b": _single_model("gemma4:e2b"),
    "deepseek": {"PLANNER_PROVIDER": "deepseek", "VLM_PROVIDER": "ollama"},
}
# Every key any condition touches, so one condition's overrides can't leak into
# the next (local_e4b's model tags must not survive into "local").
_MANAGED_KEYS = sorted({k for env in CONDITIONS.values() for k in env})

SUITE_PATH = Path(__file__).parent / "sweeps" / "commands_depth.json"

# command -> ordered answers to "what should I do next?" (section D). Empty = guidance off.
GUIDANCE_SCRIPT: Dict[str, List[str]] = {}

# Fallback only, used when tools/sweeps/commands_depth.json is missing.
DEFAULT_COMMANDS = [
    "go to the couch",
    "go to the couch then the coffee table",
    "go to the couch, then the coffee table, then the desk",
    "go to the couch, then the coffee table, then the desk, then the bookshelf",
    "turn left 90 degrees then move forward one metre",
    "go to the door and then turn around",
    "go to the lamp on the desk",
]

KINDS = ("approach", "literal", "mixed", "probe")


def _normalise_entry(raw: Any, where: str) -> Dict[str, Any]:
    """A suite entry is a plain string (old format) or an object."""
    if isinstance(raw, str):
        return {"command": raw, "depth": None, "kind": None, "expected_visits": None,
                "expected_final": None, "probe": None}
    if not isinstance(raw, dict) or not isinstance(raw.get("command"), str):
        sys.exit("%s: every entry must be a string or an object with a \"command\" string" % where)
    kind = raw.get("kind")
    if kind is not None and kind not in KINDS:
        sys.exit("%s: bad kind %r for %r" % (where, kind, raw["command"]))
    final = raw.get("expected_final")
    if final is not None and not (isinstance(final, dict) and "x" in final and "y" in final):
        sys.exit("%s: expected_final needs x and y for %r" % (where, raw["command"]))
    return {"command": raw["command"], "depth": raw.get("depth"), "kind": kind,
            "expected_visits": raw.get("expected_visits"), "expected_final": final,
            "probe": raw.get("probe")}


def _suite_for_room(room_name: str) -> Path:
    """room_tour1's suite is the original commands_depth.json; every other room
    has its own commands_depth_<room>.json (tools/gen_room_suites.py), because
    expected visits and end poses are specific to a room's layout."""
    if room_name == "room_tour1":
        return SUITE_PATH
    return SUITE_PATH.with_name("commands_depth_%s.json" % room_name)


def _load_commands(path: Optional[str], depth_max: Optional[int],
                   room_name: str = "room_tour1") -> List[Dict[str, Any]]:
    suite = _suite_for_room(room_name)
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            sys.exit("%s must be a JSON array" % path)
        entries = [_normalise_entry(c, path) for c in data]
    elif suite.exists():
        entries = [_normalise_entry(c, str(suite))
                   for c in json.loads(suite.read_text(encoding="utf-8"))]
    elif room_name != "room_tour1":
        sys.exit("no suite for %s: run tools\gen_room_suites.py (expected %s)" % (room_name, suite))
    else:
        entries = [_normalise_entry(c, "DEFAULT_COMMANDS") for c in DEFAULT_COMMANDS]
    if depth_max is not None:
        # Plain-string entries have no depth, so they are not filtered out.
        entries = [e for e in entries if e["depth"] is None or e["depth"] <= depth_max]
    for e in entries:
        e["room"] = room_name
    return entries


def _usage_field(usage_summary: dict, role: str, field: str):
    return (usage_summary.get(role) or {}).get(field)


def _first_divergence(actual: List[str], expected: List[str]) -> Optional[int]:
    """0-based index of the first wrong or missing visit; None if identical.
    An extra visit beyond the expected list also counts (the first one past
    the end), so doing too much is scored like doing the wrong thing."""
    for i in range(max(len(actual), len(expected))):
        if i >= len(actual) or i >= len(expected) or actual[i] != expected[i]:
            return i
    return None


def _score(entry: Optional[dict], rover_summary: dict, grounding_summary: dict,
           plan_steps: Optional[int], executed_this_run: bool,
           start_xy: Optional[tuple]) -> Dict[str, Any]:
    entry = entry or {}
    visits = list(rover_summary.get("visits") or []) if executed_this_run else []
    expected = entry.get("expected_visits")
    out: Dict[str, Any] = {
        "depth": entry.get("depth"),
        "kind": entry.get("kind"),
        "probe": entry.get("probe"),
        "expected_visits": json.dumps(expected) if expected is not None else None,
        "visits_actual": json.dumps(visits),
        "visits_match": None,
        "first_divergence": None,
        "plan_steps_vs_depth": None,
        "final_pose_error_m": None,
        "probe_logged": None,
    }
    if expected is not None:
        out["visits_match"] = visits == list(expected)
        out["first_divergence"] = _first_divergence(visits, list(expected))
    if entry.get("depth") is not None and plan_steps is not None:
        out["plan_steps_vs_depth"] = plan_steps - int(entry["depth"])
    final = entry.get("expected_final")
    if final is not None and entry.get("kind") in ("literal", "mixed"):
        pose = rover_summary.get("final_pose") if executed_this_run else None
        if isinstance(pose, dict):
            actual_xy = (pose.get("x"), pose.get("y"))
        else:
            actual_xy = start_xy  # nothing executed: the rover never left the start
        if actual_xy and None not in actual_xy:
            out["final_pose_error_m"] = round(
                math.hypot(actual_xy[0] - final["x"], actual_xy[1] - final["y"]), 3)
    if entry.get("kind") == "probe":
        out["probe_logged"] = bool(rover_summary.get("unresolved_targets")
                                   or grounding_summary.get("out_of_vocab_actions"))
    return out


def _row_from_report(condition: str, command: str, repeat: int, wall_clock_s: float,
                     report: dict, auto_confirmed: bool, error: Optional[str] = None,
                     entry: Optional[dict] = None, plan: Optional[dict] = None,
                     executed_this_run: bool = False, start_xy: Optional[tuple] = None,
                     sampling: Optional[dict] = None) -> Dict[str, Any]:
    models = report.get("models") or {}
    planner_cfg = models.get("planner") or {}
    vlm_cfg = models.get("vlm") or {}
    profile = models.get("planner_profile") or {}
    rover_summary = report.get("rover_summary") or {}
    grounding_summary = report.get("grounding_summary") or {}
    dialogue = report.get("dialogue") or {}
    usage_summary = report.get("usage_summary") or {}
    uncertainties = report.get("uncertainties") or []
    sampling = sampling or {}

    planner_cost = _usage_field(usage_summary, "planner", "cost_usd_total")
    vlm_cost = _usage_field(usage_summary, "vlm", "cost_usd_total")
    total_cost = None
    if planner_cost is not None or vlm_cost is not None:
        total_cost = (planner_cost or 0.0) + (vlm_cost or 0.0)

    plan_steps = None
    if isinstance(plan, dict) and isinstance(plan.get("steps"), list):
        plan_steps = len(plan["steps"])
    notes = plan.get("notes") if isinstance(plan, dict) else None

    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "condition": condition,
        "planner_provider": planner_cfg.get("provider"),
        "planner_model": planner_cfg.get("model"),
        "vlm_provider": vlm_cfg.get("provider"),
        "vlm_model": vlm_cfg.get("model"),
        "prompt_sha": profile.get("prompt_sha"),
        "command": command,
        "repeat_index": repeat,
        "session_id": report.get("session_id"),
        "test_level": os.environ.get("TEST_LEVEL") or None,
        "outcome": report.get("outcome"),
        "auto_confirmed": auto_confirmed,
        "turn_count": dialogue.get("turn_count"),
        "capped": dialogue.get("capped"),
        "verified": dialogue.get("verified"),
        "replan_count": dialogue.get("replan_count"),
        "step_count": len(report.get("steps") or []),
        "blocked_count": rover_summary.get("blocked"),
        "collisions": rover_summary.get("collisions"),
        "no_path_count": rover_summary.get("no_path_count"),
        "hallucinations": rover_summary.get("hallucinations"),
        "unresolved_targets": "; ".join(str(t) for t in (rover_summary.get("unresolved_targets") or [])),
        "out_of_vocab_actions": "; ".join(grounding_summary.get("out_of_vocab_actions") or []),
        "odometer_m": rover_summary.get("odometer_m"),
        "sim_time_s": rover_summary.get("sim_time_s"),
        "wall_clock_s": round(wall_clock_s, 3),
        "uncertainty_count": len(uncertainties),
        "uncertainty_codes": "; ".join(str(u.get("code")) for u in uncertainties),
        "planner_calls": _usage_field(usage_summary, "planner", "calls"),
        "planner_latency_mean_s": _usage_field(usage_summary, "planner", "latency_mean_s"),
        "planner_cost_usd": planner_cost,
        "vlm_calls": _usage_field(usage_summary, "vlm", "calls"),
        "vlm_latency_mean_s": _usage_field(usage_summary, "vlm", "latency_mean_s"),
        "vlm_cost_usd": vlm_cost,
        "total_cost_usd": total_cost,
        # Full traceback when a repeat raised (blank otherwise); the row is
        # still written, with its scoring columns filled as a failed run.
        "error": error,
        # -- RQ2 scoring (appended; none of the columns above are renamed) --
        # planner_latency_s is the TOTAL planner time for the run (all calls,
        # replans included); planner_latency_mean_s above is per call.
        "planner_latency_s": _usage_field(usage_summary, "planner", "latency_total_s"),
        "room": (entry or {}).get("room"),
        "trace_file": (Path(rover_summary['trace']).name if executed_this_run and rover_summary.get("trace") else None),
        "run_index": rover_summary.get("run") if executed_this_run else None,
        # -- section D (appended; none of the columns above are renamed)
        "guidance_turns": report.get("guidance_turns"),
        "halt_reason": report.get("halt_reason"),
        "recovered": report.get("recovered"),
    }
    scored = _score(entry, rover_summary, grounding_summary, plan_steps, executed_this_run, start_xy)
    row.update(scored)
    row.update({
        "plan_steps": plan_steps,
        "plan_notes": (str(notes)[:300] if notes else None),
        "planner_num_ctx": sampling.get("num_ctx"),
        "planner_temperature": sampling.get("temperature"),
        "planner_seed": sampling.get("seed"),
        "planner_repeat_penalty": sampling.get("repeat_penalty"),
        "planner_keep_alive": sampling.get("keep_alive"),
    })
    return row


ROW_FIELDS = list(_row_from_report("", "", 0, 0.0, {}, False).keys())


def _sampling_params(role: str = "planner") -> dict:
    """num_ctx/temperature/seed/repeat_penalty/keep_alive, for whatever the
    provider exposes; absent ones are None (Ollama has no temperature/seed
    attribute — it runs at the model's own default)."""
    import providers
    try:
        provider = providers.get_provider(role)
    except Exception:
        return {}
    return {k: getattr(provider, k, None)
            for k in ("num_ctx", "temperature", "seed", "repeat_penalty", "keep_alive")}


async def _run_one(entry: dict, condition: str, repeat: int, room, shared_rover,
                   max_clarify_guard: int, sampling: dict) -> Dict[str, Any]:
    # Imported here, not at module scope: config/providers must see this
    # run's env overrides (applied by the caller) before anything reads them.
    import dialogue_session
    import mission as mission_mod
    import mission_session
    import pipeline
    import planner
    import providers
    import report as report_mod
    from dialogue_session import Phase

    command = entry["command"]
    start_xy = (room.start_x, room.start_y)
    run_before = getattr(shared_rover, "_run_index", None)
    extras = dict(entry=entry, start_xy=start_xy, sampling=sampling)

    providers.usage.reset()
    started = time.perf_counter()

    session = dialogue_session.store.create(language="en")
    session.scene_id = "room:" + room.name
    session.scene_text = room.digest_text()
    # Text-only grounding, on purpose: SCENE_SOURCE=room never gives the VLM
    # an image, so session.image stays None — matching main.py's own branch.

    try:
        pipeline.handle_dialogue_text(session, command)

        # No human to answer a clarifying question. A neutral non-answer lets
        # the dialogue's own MAX_CLARIFYING_TURNS cap force it into planning
        # (recorded as capped=True) rather than hanging the sweep.
        guard = 0
        while session.phase is Phase.CLARIFYING and guard < max_clarify_guard:
            pipeline.handle_dialogue_text(session, "use your best judgement")
            guard += 1

        auto_confirmed = False
        if session.phase is Phase.AWAITING_CONFIRMATION:
            pipeline.handle_confirmation_text(session, "yes")
            auto_confirmed = session.phase is Phase.EXECUTING

        if session.phase is not Phase.EXECUTING:
            wall_clock_s = time.perf_counter() - started
            fake_report = {
                "session_id": session.session_id,
                "outcome": "never_confirmed",
                "models": {
                    "planner": report_mod._role_config("planner"),
                    "vlm": report_mod._role_config("vlm"),
                    "planner_profile": planner.profile_info(),
                },
                "usage_summary": report_mod._usage_summary(providers.usage.snapshot()),
            }
            return _row_from_report(condition, command, repeat, wall_clock_s, fake_report,
                                    auto_confirmed, error="dialogue never reached confirmation",
                                    plan=session.plan, **extras)

        msn = mission_session.store.create(session)
        if GUIDANCE_SCRIPT:   # unattended: an unscripted command, or a spent script, answers "stop"
            answers = list(GUIDANCE_SCRIPT.get(command, []))
            msn.guidance_provider = lambda entry, _a=answers: _a.pop(0) if _a else None

        events: List[dict] = []

        async def emit(event: dict) -> None:
            events.append(event)

        await mission_mod.run_mission(msn, shared_rover, emit)
        wall_clock_s = time.perf_counter() - started

        report_event = next((e for e in reversed(events) if e.get("type") == "report"), None)
        report = report_event["report"] if report_event else report_mod.build_report(msn, spoken=False)

        # The rover only starts a new run on a plan's first step, so a plan
        # that executed nothing leaves the PREVIOUS repeat's visits/pose in
        # summary(). Compare run counters to avoid scoring stale data.
        run_after = (report.get("rover_summary") or {}).get("run")
        executed = run_before is None or run_after != run_before
        return _row_from_report(condition, command, repeat, wall_clock_s, report, auto_confirmed,
                                plan=getattr(msn, "active_plan", None) or session.plan,
                                executed_this_run=executed, **extras)

    except Exception:  # one bad run must not kill the whole sweep
        wall_clock_s = time.perf_counter() - started
        return _row_from_report(condition, command, repeat, wall_clock_s, {}, False,
                                error=traceback.format_exc(), **extras)
    finally:
        dialogue_session.store.drop(session.session_id)


def _apply_condition(condition: str, base_env: Dict[str, Optional[str]]) -> None:
    """Reset every key any condition manages to its pre-sweep value, then apply
    this condition's overrides, so nothing leaks between conditions."""
    import providers
    for key in _MANAGED_KEYS:
        if base_env.get(key) is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = base_env[key]
    for key, value in CONDITIONS[condition].items():
        os.environ[key] = value
    providers.reset_cache()  # providers bake num_ctx/keep_alive in at construction


def _ollama_models_needed() -> List[str]:
    from providers import factory
    needed = []
    for role in ("planner", "vlm"):
        provider, model = factory._resolve(role)
        if provider == "ollama" and model and model not in needed:
            needed.append(model)
    return needed


def _installed_ollama_models() -> Optional[List[str]]:
    import config
    try:
        import ollama
        listing = ollama.Client(host=config.get("OLLAMA_HOST") or None).list()
        rows = listing["models"] if isinstance(listing, dict) else listing.models
        names = []
        for row in rows:
            name = row.get("model") if isinstance(row, dict) else getattr(row, "model", None)
            if name:
                names.append(name)
        return names
    except Exception as exc:
        print("warning: could not query Ollama (%s) — skipping the installed-model check" % exc)
        return None


def _preflight(conditions: List[str], base_env: Dict[str, Optional[str]]) -> None:
    """Fail before the sweep starts, not 40 rows in, if a condition names an
    Ollama tag that isn't pulled."""
    installed = _installed_ollama_models()
    if installed is None:
        return
    have = set(installed) | {n[:-7] for n in installed if n.endswith(":latest")}
    missing = []
    for condition in conditions:
        _apply_condition(condition, base_env)
        for model in _ollama_models_needed():
            if model not in have:
                missing.append((condition, model))
    if missing:
        lines = ["  %-10s needs %s  ->  ollama pull %s" % (c, m, m) for c, m in missing]
        sys.exit("Ollama models not installed (installed: %s):\n%s"
                 % (", ".join(sorted(installed)) or "none", "\n".join(lines)))


def _warm_up() -> None:
    """Load the condition's Ollama model(s) before the first timed row, so the
    cold-load doesn't land in the first repeat's latency."""
    import config
    models = _ollama_models_needed()
    if not models:
        return
    import ollama
    client = ollama.Client(host=config.get("OLLAMA_HOST") or None)
    for model in models:
        t0 = time.perf_counter()
        try:
            client.generate(model=model, prompt="", keep_alive=config.get("OLLAMA_KEEP_ALIVE") or "30m")
            print("  warmed %s in %.1fs" % (model, time.perf_counter() - t0))
        except Exception as exc:
            print("  warm-up of %s failed (%s) — first row will include load time" % (model, exc))


def _repeats_for(condition: str, override: Optional[int]) -> int:
    if override:
        return override
    return 5 if condition.startswith("local") else 3


def _history_mean_wall(condition: str) -> Optional[float]:
    """Mean wall_clock_s for this condition across earlier sweep CSVs."""
    values = []
    for path in (Path(__file__).parent / "sweeps").glob("sweep_*.csv"):
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    if row.get("condition") == condition and not row.get("error") and row.get("wall_clock_s"):
                        values.append(float(row["wall_clock_s"]))
        except (OSError, ValueError):
            continue
    return statistics.mean(values) if values else None


def _estimate(conditions: List[str], entries: List[dict], repeats: Optional[int]) -> None:
    """Rough up-front estimate. Per-run time = the condition's historical mean
    wall time (earlier sweep CSVs; assumed 50 s local / 11 s cloud if there is
    none), scaled by (1 + 0.08 x depth) for longer plans and drives, relative
    to the ~2.3 mean depth of those old runs. A guess, not a measurement — the
    depth-12 rows will be the slow ones."""
    mean_depth = statistics.mean([e["depth"] for e in entries if e["depth"] is not None] or [3])
    total_runs, total_s = 0, 0.0
    print("estimate (%d commands, mean depth %.1f):" % (len(entries), mean_depth))
    for condition in conditions:
        n = len(entries) * _repeats_for(condition, repeats)
        hist = _history_mean_wall(condition)
        base = hist if hist is not None else (50.0 if condition.startswith("local") else 11.0)
        per_run = base * (1 + 0.08 * mean_depth) / (1 + 0.08 * 2.3)
        total_runs += n
        total_s += n * per_run
        print("  %-10s %4d runs x ~%3.0fs = ~%.0f min  (%s)" % (
            condition, n, per_run, n * per_run / 60.0,
            "history mean %.0fs" % hist if hist is not None else "no history, assumed default"))
    print("  total      %4d runs, ~%.1f h wall time\n" % (total_runs, total_s / 3600.0))


async def _run_sweep(conditions: List[str], entries: List[dict], repeats: Optional[int],
                     out_path: Path, base_env: Dict[str, Optional[str]]) -> None:
    import config
    import room_map
    import rover

    room_names: List[str] = []
    for e in entries:
        if e["room"] not in room_names:
            room_names.append(e["room"])
    max_clarify_guard = config.get_int("MAX_CLARIFYING_TURNS", 5) + 1

    total = sum(len(entries) * _repeats_for(c, repeats) for c in conditions)
    done = 0
    shas = set()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ROW_FIELDS)
        writer.writeheader()

        for condition in conditions:
            _apply_condition(condition, base_env)
            sampling = _sampling_params("planner")
            print("\n== %s: %s | sampling %s" % (
                condition, " / ".join(_ollama_models_needed()) or "cloud", sampling))
            _warm_up()

            for room_name in room_names:
                # One VirtualRover (and its grid) per room; get_rover() reads
                # ROOM_MAP when it builds the singleton, so drop the cache.
                os.environ["ROOM_MAP"] = "rooms/%s.json" % room_name
                rover._cache = None
                shared_rover = rover.get_rover()
                room = room_map.load_room(os.environ["ROOM_MAP"])
                print("-- room %s" % room_name)
                for entry in [e for e in entries if e["room"] == room_name]:
                    for repeat in range(1, _repeats_for(condition, repeats) + 1):
                        done += 1
                        print("[%d/%d] %-9s d=%-4s r%d: %.70s" % (
                            done, total, condition, entry["depth"], repeat, entry["command"]),
                              end=" ", flush=True)
                        try:
                            row = await _run_one(entry, condition, repeat, room, shared_rover,
                                                 max_clarify_guard, sampling)
                        except Exception:  # never lose a row, even to a harness bug
                            row = {k: None for k in ROW_FIELDS}
                            row.update(timestamp=datetime.now().isoformat(timespec="seconds"),
                                       condition=condition, command=entry["command"],
                                       repeat_index=repeat, depth=entry["depth"], kind=entry["kind"],
                                       probe=entry["probe"], room=entry.get("room"), visits_actual="[]",
                                       error="harness: " + traceback.format_exc())
                        writer.writerow(row)
                        handle.flush()
                        if row.get("prompt_sha"):
                            shas.add(row["prompt_sha"])
                        err_lines = (row.get("error") or "").strip().splitlines()
                        status = ("ERROR " + err_lines[-1]) if err_lines else row.get("outcome")
                        match = row.get("visits_match")
                        print("-> %s%s (%.1fs)" % (status, "" if match is None else " match=%s" % match,
                                                   row.get("wall_clock_s") or 0.0))

    print("\nwrote %s (%d rows)" % (out_path, done))
    if len(shas) > 1:
        print("WARNING: more than one prompt_sha in this sweep: %s — conditions are not comparable"
              % sorted(shas))
    elif shas:
        print("prompt_sha identical across all conditions: %s" % next(iter(shas)))


def _setup_console_logging(verbose: bool) -> None:
    """The dashboard's own logging (log_setup.setup_logging) puts INFO+ on
    the console — fine for one live run, but across dozens of sweep rows it
    drowns this script's own progress lines in per-call detail, including a
    warning (with full traceback) that fires on *every* run: TTS falling
    back to text-only, because Piper's voice files aren't fully set up. That
    fallback is harmless and by design (mission.py/pipeline.py's _speak()
    already catch it) — it just has nothing to do with what this sweep
    measures.

    Full detail always still lands in logs/brain.log at DEBUG. --verbose
    restores the normal INFO-level console output, e.g. to see exactly what
    a specific failing run did.
    """
    import log_setup
    log_setup.setup_logging()
    if verbose:
        return
    root = logging.getLogger("brain")
    for handler in root.handlers:
        if isinstance(handler, logging.StreamHandler) and handler.stream is sys.stdout:
            handler.setLevel(logging.ERROR)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--conditions", default="cloud,local",
                        help="comma-separated, from: %s (default: cloud,local)"
                             % ", ".join(CONDITIONS))
    parser.add_argument("--repeats", type=int, default=None,
                        help="runs per (condition, command); default 5 for local*, 3 otherwise")
    parser.add_argument("--time-scale", type=float, default=20.0,
                        help="VIRTUAL_TIME_SCALE override for the whole sweep (default 20)")
    parser.add_argument("--allow-drift", action="store_true",
                        help="keep .env's VIRTUAL_DRIFT_FRAC/DEG instead of forcing 0")
    parser.add_argument("--commands", default=None,
                        help="path to a JSON array of command strings and/or objects "
                             "(default: tools/sweeps/commands_depth.json)")
    parser.add_argument("--rooms", default="room_tour1",
                        help="comma-separated room names from rooms/, or 'all' (default: room_tour1). "
                             "Each room needs its own suite (tools/gen_room_suites.py)")
    parser.add_argument("--num-ctx", type=int, default=None,
                        help="override PLANNER_NUM_CTX and VLM_NUM_CTX (local_e4b/local_e2b only; both roles, "
                             "so Ollama does not reload the model between calls) - the section A experiment")
    parser.add_argument("--guidance-script", default=None,
                        help="JSON {command: [answers]}: enables the section D guidance reprompt with "
                             "scripted answers (otherwise GUIDANCE_ENABLED is forced to 0)")
    parser.add_argument("--depth-max", type=int, default=None,
                        help="drop suite entries deeper than this (quick smoke test)")
    parser.add_argument("--level", default=None,
                        help="test level label (e.g. L3) written to every run log and the CSV test_level column")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the run count / time estimate and exit")
    parser.add_argument("--out", default=None,
                        help="output CSV path (default: tools/sweeps/sweep_<timestamp>.csv)")
    parser.add_argument("--verbose", action="store_true",
                        help="show the app's own INFO-level logging on console too (noisy)")
    # commands copied out of chat can carry an invisible zero-width space on the last
    # argument (-> 'invalid model name' / int('40' + U+200B)); drop them
    args = parser.parse_args([re.sub('[\u200b-\u200d\u2060\ufeff]', '', a) for a in sys.argv[1:]])

    conditions = [c.strip() for c in args.conditions.split(",") if c.strip()]
    for condition in conditions:
        if condition not in CONDITIONS:
            sys.exit("unknown condition %r — choose from: %s" % (condition, ", ".join(CONDITIONS)))

    if args.num_ctx:
        if any(c not in ("local_e4b", "local_e2b") for c in conditions):
            sys.exit("--num-ctx only applies to the local_e4b / local_e2b conditions")
        for condition in conditions:
            CONDITIONS[condition]["PLANNER_NUM_CTX"] = CONDITIONS[condition]["VLM_NUM_CTX"] = str(args.num_ctx)

    _setup_console_logging(args.verbose)

    # Virtual runs only: .env may say ROVER=pi / SCENE_SOURCE=video for the
    # real rover, and a real env var beats .env, so force both here.
    os.environ["ROVER"] = "virtual"
    os.environ["SCENE_SOURCE"] = "room"
    # Existing RQ2 sweeps and probe chains must keep stopping at the first blocked step.
    os.environ["GUIDANCE_ENABLED"] = "1" if args.guidance_script else "0"
    if args.guidance_script:
        GUIDANCE_SCRIPT.update(json.loads(Path(args.guidance_script).read_text(encoding="utf-8")))
    # Must be set before the first rover.get_rover() call (below), which
    # constructs the process-wide singleton these values are baked into.
    os.environ["VIRTUAL_TIME_SCALE"] = str(args.time_scale)
    if not args.allow_drift:
        os.environ["VIRTUAL_DRIFT_FRAC"] = "0"
        os.environ["VIRTUAL_DRIFT_DEG"] = "0"

    if args.rooms.strip().lower() == "all":
        rooms = sorted(p.stem for p in (BRAIN_DIR / "rooms").glob("*.json"))
    else:
        rooms = [r.strip() for r in args.rooms.split(",") if r.strip()]
    for room_name in rooms:
        if not (BRAIN_DIR / "rooms" / ("%s.json" % room_name)).exists():
            sys.exit("no such room: rooms/%s.json" % room_name)
    if args.commands and len(rooms) > 1:
        sys.exit("--commands is a single suite; use it with exactly one room")
    entries = []
    for room_name in rooms:
        entries.extend(_load_commands(args.commands, args.depth_max, room_name))
    if not entries:
        sys.exit("no commands left after --depth-max %s" % args.depth_max)
    base_env = {k: os.environ.get(k) for k in _MANAGED_KEYS}
    if args.level:
        os.environ["TEST_LEVEL"] = args.level

    print("NOTE: virtual runs are text-only for the VLM role (SCENE_SOURCE=room), so a single-model "
          "condition (local_e4b/local_e2b) changes only the planner here.")
    _estimate(conditions, entries, args.repeats)
    if args.dry_run:
        return
    _preflight(conditions, base_env)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = Path(args.out) if args.out else BRAIN_DIR / "tools" / "sweeps" / ("sweep_%s.csv" % stamp)

    try:
        asyncio.run(_run_sweep(conditions, entries, args.repeats, out_path, base_env))
    except KeyboardInterrupt:
        print("\ninterrupted — rows completed so far are already saved in %s" % out_path)
        sys.exit(130)


if __name__ == "__main__":
    main()
