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
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
import time
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
CONDITIONS: Dict[str, Dict[str, str]] = {
    "cloud": {"PLANNER_PROVIDER": "openai", "VLM_PROVIDER": "openai"},
    "local": {"PLANNER_PROVIDER": "ollama", "VLM_PROVIDER": "ollama"},
    "deepseek": {"PLANNER_PROVIDER": "deepseek", "VLM_PROVIDER": "ollama"},
}

# A small RQ2-flavoured suite: increasing chained-instruction length, a
# literal move/turn chain, and one deliberately out-of-vocabulary target to
# exercise the hallucination/grounding path. Override with --commands.
DEFAULT_COMMANDS = [
    "go to the couch",
    "go to the couch then the coffee table",
    "go to the couch, then the coffee table, then the desk",
    "go to the couch, then the coffee table, then the desk, then the bookshelf",
    "turn left 90 degrees then move forward one metre",
    "go to the door and then turn around",
    "go to the lamp on the desk",
]


def _load_commands(path: Optional[str]) -> List[str]:
    if not path:
        return list(DEFAULT_COMMANDS)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(c, str) for c in data):
        sys.exit("%s must be a JSON array of strings" % path)
    return data


def _usage_field(usage_summary: dict, role: str, field: str):
    return (usage_summary.get(role) or {}).get(field)


def _row_from_report(condition: str, command: str, repeat: int, wall_clock_s: float,
                     report: dict, auto_confirmed: bool, error: Optional[str] = None) -> Dict[str, Any]:
    models = report.get("models") or {}
    planner_cfg = models.get("planner") or {}
    vlm_cfg = models.get("vlm") or {}
    profile = models.get("planner_profile") or {}
    rover_summary = report.get("rover_summary") or {}
    grounding_summary = report.get("grounding_summary") or {}
    dialogue = report.get("dialogue") or {}
    usage_summary = report.get("usage_summary") or {}
    uncertainties = report.get("uncertainties") or []

    planner_cost = _usage_field(usage_summary, "planner", "cost_usd_total")
    vlm_cost = _usage_field(usage_summary, "vlm", "cost_usd_total")
    total_cost = None
    if planner_cost is not None or vlm_cost is not None:
        total_cost = (planner_cost or 0.0) + (vlm_cost or 0.0)

    return {
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
        "error": error,
    }


ROW_FIELDS = list(_row_from_report("", "", 0, 0.0, {}, False).keys())


async def _run_one(command: str, condition: str, repeat: int, room, shared_rover,
                   max_clarify_guard: int) -> Dict[str, Any]:
    # Imported here, not at module scope: config/providers must see this
    # run's env overrides (applied by the caller) before anything reads them.
    import dialogue_session
    import mission as mission_mod
    import mission_session
    import pipeline
    import providers
    import report as report_mod
    from dialogue_session import Phase

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
                    "planner_profile": __import__("planner").profile_info(),
                },
                "usage_summary": report_mod._usage_summary(providers.usage.snapshot()),
            }
            return _row_from_report(condition, command, repeat, wall_clock_s, fake_report,
                                    auto_confirmed, error="dialogue never reached confirmation")

        msn = mission_session.store.create(session)

        events: List[dict] = []

        async def emit(event: dict) -> None:
            events.append(event)

        await mission_mod.run_mission(msn, shared_rover, emit)
        wall_clock_s = time.perf_counter() - started

        report_event = next((e for e in reversed(events) if e.get("type") == "report"), None)
        report = report_event["report"] if report_event else report_mod.build_report(msn, spoken=False)
        return _row_from_report(condition, command, repeat, wall_clock_s, report, auto_confirmed)

    except Exception as exc:  # one bad run must not kill the whole sweep
        wall_clock_s = time.perf_counter() - started
        return _row_from_report(condition, command, repeat, wall_clock_s, {}, False, error=str(exc))
    finally:
        dialogue_session.store.drop(session.session_id)


async def _run_sweep(conditions: List[str], commands: List[str], repeats: int,
                     out_path: Path) -> None:
    import config
    import room_map
    import rover

    room = room_map.load_room(config.get("ROOM_MAP", "rooms/room_tour1.json"))
    shared_rover = rover.get_rover()  # built once; reused for every run below
    max_clarify_guard = config.get_int("MAX_CLARIFYING_TURNS", 5) + 1

    total = len(conditions) * len(commands) * repeats
    done = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ROW_FIELDS)
        writer.writeheader()

        for condition in conditions:
            overrides = CONDITIONS[condition]
            for key, value in overrides.items():
                os.environ[key] = value

            for command in commands:
                for repeat in range(1, repeats + 1):
                    done += 1
                    print("[%d/%d] %-8s repeat %d: %s" % (done, total, condition, repeat, command),
                          end=" ", flush=True)
                    row = await _run_one(command, condition, repeat, room, shared_rover,
                                         max_clarify_guard)
                    writer.writerow(row)
                    handle.flush()
                    status = row.get("error") or row.get("outcome")
                    print("-> %s (%.1fs)" % (status, row.get("wall_clock_s") or 0.0))

    print("\nwrote %s (%d rows)" % (out_path, total))


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
    parser.add_argument("--repeats", type=int, default=3, help="runs per (condition, command)")
    parser.add_argument("--time-scale", type=float, default=20.0,
                        help="VIRTUAL_TIME_SCALE override for the whole sweep (default 20)")
    parser.add_argument("--allow-drift", action="store_true",
                        help="keep .env's VIRTUAL_DRIFT_FRAC/DEG instead of forcing 0")
    parser.add_argument("--commands", default=None,
                        help="path to a JSON array of command strings (default: built-in suite)")
    parser.add_argument("--out", default=None,
                        help="output CSV path (default: tools/sweeps/sweep_<timestamp>.csv)")
    parser.add_argument("--verbose", action="store_true",
                        help="show the app's own INFO-level logging on console too (noisy)")
    args = parser.parse_args()

    conditions = [c.strip() for c in args.conditions.split(",") if c.strip()]
    for condition in conditions:
        if condition not in CONDITIONS:
            sys.exit("unknown condition %r — choose from: %s" % (condition, ", ".join(CONDITIONS)))

    _setup_console_logging(args.verbose)

    # Must be set before the first rover.get_rover() call (below), which
    # constructs the process-wide singleton these values are baked into.
    os.environ["VIRTUAL_TIME_SCALE"] = str(args.time_scale)
    if not args.allow_drift:
        os.environ["VIRTUAL_DRIFT_FRAC"] = "0"
        os.environ["VIRTUAL_DRIFT_DEG"] = "0"

    commands = _load_commands(args.commands)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = Path(args.out) if args.out else BRAIN_DIR / "tools" / "sweeps" / ("sweep_%s.csv" % stamp)

    try:
        asyncio.run(_run_sweep(conditions, commands, args.repeats, out_path))
    except KeyboardInterrupt:
        print("\ninterrupted — rows completed so far are already saved in %s" % out_path)
        sys.exit(130)


if __name__ == "__main__":
    main()
