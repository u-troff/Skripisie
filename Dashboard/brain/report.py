"""End-of-mission report: what happened, and what we are still unsure about.

Two halves, deliberately unequal in trust:

* A **deterministic block** assembled in plain Python from what the run already
  recorded — plan, per-step results, every VLM check with its latency and the
  frame it was taken from, the sonar's obstacle count, and which provider and
  model each role actually used. No model touches any of it, so a benchmarking
  run on `PLANNER_PROVIDER=openai` can never be mistaken for a local one.

* **`uncertainties[]`**, also rule-based. This is the "flag residual
  uncertainty rather than assert" moment the report cites. Every entry comes
  from a comparison the run can prove: a check that errored is recorded as a
  *failed check*, never as "the target was not visible" — the whole difference
  between the system saying "I could not see it" and the system hallucinating
  that it did.

Only `spoken_summary` involves a model, and it is a single planner call
constrained to the JSON above with an offline template fallback. If it fails,
the report is still complete; the rover just reads out the template.

See Progress/spec-grounded-line-mission.md §F and
Progress/spec-planner-profiles-and-virtual-sweep.md §3D (rover_summary,
grounding, dialogue, usage_summary, grounding_summary and the RQ2-flavoured
uncertainty codes below).
"""

import json
import time
from typing import Any, Dict, List, Optional

import config
import planner
from log_setup import get_logger
from providers import ProviderError, get_provider, log_completion, user_message

log = get_logger("report")

# Mirrors providers.factory._resolve rather than importing it: this is a
# *record* of what was configured, and it must keep working even when the
# provider itself cannot be constructed (no API key, Ollama down).
def _role_config(role: str) -> dict:
    provider = config.get(role.upper() + "_PROVIDER", "ollama").lower()
    model = config.get("%s_MODEL_%s" % (role.upper(), provider.upper()), "")
    return {"provider": provider, "model": model, "local": provider == "ollama"}


def _result(check: Optional[dict]) -> dict:
    if not isinstance(check, dict):
        return {}
    inner = check.get("result")
    return inner if isinstance(inner, dict) else {}


def _errored(check: Optional[dict]) -> bool:
    return "_error" in _result(check)


def _visible(check: Optional[dict]) -> Optional[bool]:
    """Tri-state on purpose. None means "we do not know", which is exactly what
    a failed or absent check tells us — collapsing it to False would invent a
    negative observation the rover never made."""
    inner = _result(check)
    if not inner or "_error" in inner or "target_visible" not in inner:
        return None
    return bool(inner.get("target_visible"))


def _usage_summary(usage_records: List[dict]) -> Dict[str, dict]:
    """Per role: calls, total/mean latency, tokens and USD cost if present.
    Built straight from providers.usage.snapshot() — no model touches this."""
    by_role: Dict[str, List[dict]] = {}
    for record in usage_records:
        by_role.setdefault(str(record.get("role") or "unknown"), []).append(record)

    def _sum(records: List[dict], key: str) -> Optional[float]:
        values = [r[key] for r in records if isinstance(r.get(key), (int, float))]
        return sum(values) if values else None

    summary: Dict[str, dict] = {}
    for role, records in by_role.items():
        latencies = [r["latency_s"] for r in records if isinstance(r.get("latency_s"), (int, float))]
        prompt_total = _sum(records, "prompt_tokens")
        completion_total = _sum(records, "completion_tokens")
        cost_total = _sum(records, "cost_usd")
        summary[role] = {
            "calls": len(records),
            "latency_total_s": round(sum(latencies), 3) if latencies else None,
            "latency_mean_s": round(sum(latencies) / len(latencies), 3) if latencies else None,
            "prompt_tokens_total": int(prompt_total) if prompt_total is not None else None,
            "completion_tokens_total": int(completion_total) if completion_total is not None else None,
            "cost_usd_total": round(cost_total, 6) if cost_total is not None else None,
        }
    return summary


def _grounding_summary(grounding: List[dict], plan: dict, active_actions: dict) -> dict:
    """targets, unresolved and out_of_vocab_actions — the RQ2 roll-up over
    mission.grounding and the confirmed plan's actions."""
    targets = [g.get("target") for g in grounding]
    unresolved = [g.get("target") for g in grounding if not g.get("resolved")]
    plan_actions = {str(s.get("action") or "").strip().lower()
                    for s in (plan.get("steps") or [])}
    out_of_vocab = sorted(a for a in plan_actions if a and a not in active_actions)
    return {"targets": targets, "unresolved": unresolved, "out_of_vocab_actions": out_of_vocab}


def build_report(mission, spoken: bool = True) -> dict:
    """Assemble the run's report. `spoken=False` skips the one planner call,
    for the abort path where no network round-trip is allowed."""
    # mission.checks is appended in completion order: a skip lands immediately
    # while a real check lands seconds later, when the VLM answers. Sort by
    # t_rel_s so the run log reads as the drive actually happened.
    checks = sorted(mission.checks, key=lambda c: c.get("t_rel_s") or 0.0)
    progress = [c for c in checks if c.get("kind") == "progress"]
    skipped = [c for c in checks if c.get("kind") == "skipped"]
    completed = [c for c in progress if not _errored(c)]
    failed = [c for c in checks if _errored(c)]

    latencies = [float(c["latency_s"]) for c in checks
                 if isinstance(c.get("latency_s"), (int, float))]
    if isinstance(mission.arrival, dict) and isinstance(mission.arrival.get("latency_s"), (int, float)):
        latencies.append(float(mission.arrival["latency_s"]))

    telemetry = mission.rover_telemetry or {}
    outcome = mission.phase.value
    profile = planner.profile_info()

    report: Dict[str, Any] = {
        "session_id": mission.session_id,
        "outcome": outcome,
        "rover": config.get("ROVER", "sim"),
        "command": mission.original_command or mission.command,
        "resolved_command": mission.command,
        "plan": mission.active_plan,
        "confirmed_plan": mission.confirmed_plan,
        "plan_was_revised": mission.active_plan != mission.confirmed_plan,
        "steps": [
            {
                "index": i,
                "action": (r.get("step") or {}).get("action"),
                "target": (r.get("step") or {}).get("target"),
                "status": r.get("status"),
                "detail": r.get("detail"),
                "elapsed_s": r.get("elapsed_s"),
            }
            for i, r in enumerate(mission.results)
        ],
        "checks": checks,
        "look_left": mission.look_left,
        "arrival": mission.arrival,
        "sonar_obstacle_events": telemetry.get("obstacle_events", 0),
        "rover_telemetry": telemetry,
        "digest": list(mission.digest),
        "revisions": [
            {"at": r.at, "kind": r.kind, "reason": r.reason, "applied": r.applied}
            for r in mission.revision_log
        ],
        "timings": {
            "total_s": round(max(mission.updated_at, time.time()) - mission.started_at, 2),
            "per_step_s": [r.get("elapsed_s") for r in mission.results],
            "vlm_latency_mean_s": round(sum(latencies) / len(latencies), 2) if latencies else None,
            "vlm_latency_max_s": round(max(latencies), 2) if latencies else None,
            "checks_completed": len(completed),
            "checks_skipped": len(skipped),
            "checks_failed": len(failed),
        },
        "models": {"planner": _role_config("planner"), "vlm": _role_config("vlm")},
        # -- planner-profiles / virtual-sweep additions (§3D) ---------------
        "rover_summary": mission.rover_summary,
        "grounding": mission.grounding,
        "grounding_summary": _grounding_summary(mission.grounding, mission.active_plan,
                                                profile["actions"]),
        "dialogue": mission.dialogue_meta,
        "usage_summary": _usage_summary(mission.usage),
    }
    report["models"]["planner_profile"] = profile

    report["uncertainties"] = _uncertainties(mission, progress, completed, failed, outcome)
    report["spoken_summary"] = (
        _spoken_summary(report) if spoken else _fallback_summary(report)
    )
    return report


def _uncertainties(mission, progress: List[dict], completed: List[dict],
                   failed: List[dict], outcome: str) -> List[dict]:
    """Rule-based, no model. Each entry is something the run can prove."""
    out: List[dict] = []

    def flag(code: str, detail: str) -> None:
        out.append({"code": code, "detail": detail})

    # no_arrival_check and thin_evidence are about the line-follower's keyframe
    # checks — meaningless for a virtual run, which has no follow_line step and
    # no camera. Gated to fire only when the plan actually has one.
    has_follow_line = any(
        str(s.get("action") or "").strip().lower() == "follow_line"
        for s in (mission.active_plan.get("steps") or [])
    )

    arrival = mission.arrival if isinstance(mission.arrival, dict) else None
    arrival_result = _result(arrival)
    arrival_visible = _visible(arrival)

    if arrival is None:
        if has_follow_line:
            flag("no_arrival_check",
                 "the mission never reached an arrival check, so nothing confirms "
                 "what the rover was looking at when it stopped")
    elif _errored(arrival):
        flag("arrival_check_failed",
             "the arrival check failed (%s) — this is not evidence that the "
             "target was absent" % arrival_result.get("_error"))
    else:
        if arrival_visible is False:
            flag("target_not_seen", "the target was not visible at arrival")
        confidence = str(arrival_result.get("confidence") or "").lower()
        if confidence == "low":
            flag("low_confidence", "the arrival check reported low confidence")
        missing = [str(m) for m in (arrival_result.get("missing") or [])]
        if missing:
            flag("expected_not_seen",
                 "expected but not visible at arrival: " + ", ".join(missing))

    if failed:
        kinds = sorted({str(c.get("kind")) for c in failed})
        flag("checks_failed",
             "%d check(s) failed to return a usable answer (%s); they say "
             "nothing either way about what was there"
             % (len(failed), ", ".join(kinds)))

    # Mid-route vs arrival disagreement, in both directions. Either way the
    # rover's own evidence is inconsistent and that belongs in the report.
    mid_visible = [v for v in (_visible(c) for c in completed) if v is not None]
    if mid_visible and arrival_visible is not None:
        if any(mid_visible) and not arrival_visible:
            flag("visibility_inconsistent",
                 "the target was visible on the way but not at arrival")
        elif not any(mid_visible) and arrival_visible:
            flag("visibility_inconsistent",
                 "the target was not visible on the way but was at arrival")

    unclear = [c for c in completed if _result(c).get("path_clear") is False]
    if unclear:
        flag("path_not_clear",
             "%d check(s) reported the path ahead was not clear" % len(unclear))

    if outcome == "halted":
        blocked = [r for r in mission.results if r.get("status") == "blocked"]
        reason = ""
        if blocked:
            detail = blocked[-1].get("detail")
            reason = detail.get("reason", "") if isinstance(detail, dict) else str(detail or "")
        flag("mission_blocked",
             "the mission stopped before finishing" + (" (%s)" % reason if reason else ""))
    elif outcome == "aborted":
        flag("mission_aborted", "the mission was aborted by the operator")

    if has_follow_line and len(completed) < 2:
        flag("thin_evidence",
             "only %d progress check(s) completed during the drive — too few to "
             "say much about what was passed on the way" % len(completed))

    # -- planner-profiles / virtual-sweep additions (§3D) -------------------
    unresolved_targets = [g.get("target") for g in mission.grounding if not g.get("resolved")]
    if unresolved_targets:
        flag("plan_target_unresolved",
             "the plan named a target the room doesn't have: "
             + ", ".join(str(t) for t in unresolved_targets))

    active_actions = planner.profile_info()["actions"]
    plan_actions = {str(s.get("action") or "").strip().lower()
                    for s in (mission.active_plan.get("steps") or [])}
    out_of_vocab = sorted(a for a in plan_actions if a and a not in active_actions)
    if out_of_vocab:
        flag("out_of_vocab_action",
             "the plan used action(s) outside the active vocabulary: "
             + ", ".join(out_of_vocab))

    for r in mission.results:
        detail = r.get("detail")
        if not isinstance(detail, dict):
            continue
        if detail.get("reason") == "collision":
            obstacle = detail.get("obstacle")
            flag("collision",
                 "the rover collided with %s" % obstacle if obstacle
                 else "the rover collided with something")
        elif detail.get("reason") == "no_path":
            target = detail.get("target")
            flag("no_path",
                 "no collision-free route to %s" % target if target
                 else "no collision-free route was found")

    return out


_SUMMARY_PROMPT = (
    "Write at most 3 short sentences for a spoken report from a small rover.\n"
    "Use ONLY facts in this JSON. Do not add anything not present in it.\n"
    "If uncertainties is non-empty, say the most important one.\n"
    "Report JSON:\n%s\n"
    'Respond ONLY with JSON: {"summary": "..."}'
)

# Fields the summariser needs. The full report carries base64-free but still
# bulky check records; trimming keeps the prompt inside the planner's num_ctx.
_SUMMARY_KEYS = ("outcome", "command", "resolved_command", "uncertainties",
                 "arrival", "look_left", "sonar_obstacle_events", "timings",
                 "rover_summary")


def _spoken_summary(report: dict) -> str:
    trimmed = {k: report.get(k) for k in _SUMMARY_KEYS}
    try:
        provider = get_provider("planner")
        completion = provider.complete(
            [user_message(_SUMMARY_PROMPT % json.dumps(trimmed, default=str))],
            json_mode=True,
        )
    except ProviderError as exc:
        log.warning("[report] spoken summary unavailable (%s) — using template", exc)
        return _fallback_summary(report)

    log_completion(log, "planner", "spoken_summary", completion)
    try:
        text = str(json.loads(completion.text).get("summary") or "").strip()
    except (json.JSONDecodeError, TypeError, AttributeError):
        log.warning("[report] unparseable summary: %r", completion.text[:200])
        return _fallback_summary(report)
    return text or _fallback_summary(report)


def _fallback_summary(report: dict) -> str:
    seen = _visible(report.get("arrival"))
    if seen is True:
        target = "Target seen."
    elif seen is False:
        target = "Target not seen."
    else:
        target = "Target unconfirmed."
    return "Mission %s. %s %d uncertainties flagged." % (
        report.get("outcome", "ended"), target, len(report.get("uncertainties") or []),
    )
