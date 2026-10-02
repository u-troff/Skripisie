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
import scene as scene_mod
import vlm
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


def _names(values) -> List[str]:
    out = []
    for value in values or []:
        name = str(value).strip()
        if name:
            out.append(name)
    return out


def _room_grounding(checks: List[dict], arrival: Optional[dict],
                    known: List[str]) -> dict:
    """What the drive checks saw, scored against the room video's catalogue.

    The room video used to stop mattering the moment the rover moved — it
    shaped the clarification and the plan, then went unused. Threading it into
    the drive checks makes two things measurable that were previously a matter
    of opinion:

    * `never_seen` — catalogued things no check ever reported. Weak evidence on
      its own (the camera points one way), so it is recorded but NOT flagged.
    * `unexpected` — things the rover reported that the room does not contain.
      This is the cleanest RQ2 hallucination signal in the run, because the
      ground truth is a list we filmed rather than a human's recollection.

    Matching is case-insensitive and exact-after-trim. Deliberately not fuzzy:
    a substring match would score "chair" against "wheelchair" and quietly
    inflate the hit rate, and an inflated hit rate is worse than no number.
    """
    catalogue = {name.lower(): name for name in known}
    seen: Dict[str, str] = {}
    unexpected: Dict[str, str] = {}

    for check in list(checks) + ([arrival] if isinstance(arrival, dict) else []):
        result = _result(check)
        if not result or "_error" in result:
            continue
        for name in _names(result.get("seen")):
            key = name.lower()
            if key in catalogue:
                seen[key] = catalogue[key]
            else:
                # Claimed as catalogued but is not in the catalogue — the model
                # invented a list entry. Counts as unexpected, not as a match.
                unexpected[key] = name
        for name in _names(result.get("unexpected")):
            unexpected.setdefault(name.lower(), name)

    return {
        "known": list(known),
        "seen": sorted(seen.values()),
        "never_seen": sorted(v for k, v in catalogue.items() if k not in seen),
        "unexpected": sorted(unexpected.values()),
        "grounded": bool(known),
    }


def _known_for(mission) -> List[str]:
    """The same catalogue the checks were handed — Scene.vocabulary, not a
    second opinion about what is in the room."""
    scene_id = getattr(mission, "scene_id", None)
    if not scene_id:
        return []
    room = scene_mod.store.get(scene_id)
    return room.vocabulary(vlm.KNOWN_MAX) if room is not None else []


def _looked(checks: List[dict], arrival: Optional[dict]) -> Dict[str, int]:
    """Completed checks per physical camera direction (spec §8: directions, not
    PWM values). Skips and failures are not counted — they saw nothing."""
    counts: Dict[str, int] = {}
    for check in list(checks) + ([arrival] if isinstance(arrival, dict) else []):
        if check.get("kind") == "skipped" or check.get("kind") == "bend":
            continue
        if _errored(check):
            continue
        where = str(check.get("aimed") or "centre")
        counts[where] = counts.get(where, 0) + 1
    return counts


def _route_summary(bends: List[dict]) -> str:
    """"took 1 left bend" — the physical half of what the run proves.

    Bends come off the IR sensor that did the steering, not off a model, so
    this is the one line in the report that says where the rover actually went
    and cannot have been hallucinated. Cheap evidence, deliberately kept.
    """
    if not bends:
        return "no bends taken; the route was a single straight run"
    counts = {"left": 0, "right": 0}
    for bend in bends:
        side = str(bend.get("side") or "").lower()
        if side in counts:
            counts[side] += 1
    parts = ["%d %s bend%s" % (n, side, "" if n == 1 else "s")
             for side, n in counts.items() if n]
    return "took " + " and ".join(parts)

def _approach_summary(mission) -> Optional[dict]:
    cycles = [c for c in mission.checks if c.get("kind") == "approach_cycle"]
    if not cycles:
        return None
    vlm_time = sum(c.get("vlm_latency_s") or 0 for c in cycles)
    span = (cycles[-1].get("t_rel_s") or 0) - (cycles[0].get("t_rel_s") or 0)
    motion_time_s = sum((c.get("pivot_cmd_s") or 0) + (c.get("hop_moved_s") or 0) for c in cycles)

    search_cycles = [c for c in cycles if c.get("phase") == "search"]
    sweeps_used = (max((c.get("sweep") or 0) for c in cycles) + 1) if cycles else 0
    chassis_pivots = sum(1 for c in cycles if c.get("pivot_cmd_s"))
    chassis_spin_deg_total = sum(c.get("pivot_requested_deg") or 0 for c in cycles)

    lock_cycle = next((c for c in cycles if c.get("phase") == "lock"), None)

    reject_counts: Dict[str, int] = {}
    for c in cycles:
        reason = c.get("reject_reason")
        if reason:
            reject_counts[reason] = reject_counts.get(reason, 0) + 1

    sharpness_values = sorted(c.get("sharpness") for c in cycles
                              if isinstance(c.get("sharpness"), (int, float)))
    frames_skipped_blurry = sum(1 for c in mission.checks
                                if c.get("kind") == "skipped" and c.get("reason") == "blurry")
    hops_done = sum(1 for c in cycles if c.get("hop_moved_s"))

    approach_result = next(
        (r for r in reversed(mission.results)
         if str((r.get("step") or {}).get("action") or "").lower() == "approach"),
        None,
    )
    detail = (approach_result or {}).get("detail") or {}

    return {
        "cycles_used": detail.get("cycles", len(cycles)),
        "sweeps_used": sweeps_used,
        "gimbal_looks": len(search_cycles),
        "chassis_pivots": chassis_pivots,
        "chassis_spin_deg_total": round(chassis_spin_deg_total, 1),
        "search_vlm_calls": len(search_cycles),
        "bearing_deg_at_lock": (lock_cycle or {}).get("bearing_deg"),
        "residual_e_at_lock": (lock_cycle or {}).get("residual_e"),
        "hops_done": hops_done,
        "frames_skipped_blurry": frames_skipped_blurry,
        "sharpness_min": round(sharpness_values[0], 1) if sharpness_values else None,
        "sharpness_median": round(sharpness_values[len(sharpness_values) // 2], 1) if sharpness_values else None,
        "detections_rejected": reject_counts,
        "arrival_cross_check": detail.get("arrival_cross_check"),
        "gimbal_only_lock": detail.get("gimbal_only_lock"),
        "vlm_time_s": round(vlm_time, 2),
        "motion_time_s": round(motion_time_s, 2),
        "wall_time_s": round(max(span - vlm_time, 0), 2),
        "sonar_stops": sum(1 for c in cycles if "sonar_stop" in str(c.get("action") or "")),
        "arrival_reason": detail.get("reason"),
    }


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
    bends = [c for c in checks if c.get("kind") == "bend"]
    room_grounding = _room_grounding(checks, mission.arrival, _known_for(mission))

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
        "target_confirmed"  :mission.target_confirmed,
        "bends": [{"side": b.get("side"), "t_rel_s": b.get("t_rel_s"),
                   "est_distance_cm": b.get("est_distance_cm")} for b in bends],
        "route_summary": _route_summary(bends),
        "approach_summary": _approach_summary(mission),
        # Which way the camera was pointing, counted. A run whose checks were
        # all centre frames saw a corridor; one that swept saw a room, and the
        # difference matters when reading what `never_seen` means.
        "looked": _looked(checks, mission.arrival),
        "scene_id": mission.scene_id,
        "room_grounding": room_grounding,
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

    report["uncertainties"] = _uncertainties(mission, progress, completed, failed,
                                             outcome, bends, room_grounding)
    report["spoken_summary"] = (
        _spoken_summary(report) if spoken else _fallback_summary(report)
    )
    return report


def _uncertainties(mission, progress: List[dict], completed: List[dict],
                   failed: List[dict], outcome: str,
                   bends: Optional[List[dict]] = None,
                   room_grounding: Optional[dict] = None) -> List[dict]:
    """Rule-based, no model. Each entry is something the run can prove."""
    out: List[dict] = []
    bends = bends or []
    room_grounding = room_grounding or {}

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

    # Spec §7's accepted limitation, made explicit instead of left for the
    # reader to guess: the sonar's obstacle stop does not respect the corner
    # guard, so a stop that lands inside a pivot cancels the turn and the rover
    # resumes straight off the tape — which then times out.
    timed_out = any(
        isinstance(r.get("detail"), dict) and r["detail"].get("reason") == "line_timeout"
        for r in mission.results
    )
    if timed_out and bends:
        flag("line_timeout_after_bend",
             "the drive timed out on a route with %d bend(s) — a stop landing "
             "during a pivot turn cancels it and the rover resumes off the "
             "tape (known limitation, not a planning failure)" % len(bends))

    if has_follow_line and len(completed) < 2:
        flag("thin_evidence",
             "only %d progress check(s) completed during the drive — too few to "
             "say much about what was passed on the way" % len(completed))

    # -- room-video grounding ----------------------------------------------
    if room_grounding.get("grounded"):
        unexpected = room_grounding.get("unexpected") or []
        if unexpected:
            flag("unexpected_objects",
                 "the rover reported %d thing(s) the filmed room does not contain: %s "
                 "— either the room changed or the model invented them"
                 % (len(unexpected), ", ".join(unexpected)))
        # Gated on a check having actually succeeded. If every check errored,
        # "recognised nothing" is a statement about the model being down, not
        # about the room — and checks_failed already says that. Reporting both
        # would double-count one fault as two kinds of doubt.
        if completed and not (room_grounding.get("seen") or []):
            flag("room_not_recognised",
                 "no catalogued object from the room video was recognised in any "
                 "completed check, so nothing visually confirms the rover was in "
                 "the room the plan was built for")
    elif has_follow_line:
        flag("ungrounded_checks",
             "no room video was loaded, so the camera checks had nothing to be "
             "scored against — an empty 'unexpected' here is not evidence that "
             "nothing was hallucinated")

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

    # -- free-roam approach additions (spec-free-roam-approach.md §E / F8) --
    approach_cycles = [c for c in mission.checks if c.get("kind") == "approach_cycle"]
    if approach_cycles:
        approach_result = next(
            (r for r in reversed(mission.results)
             if str((r.get("step") or {}).get("action") or "").lower() == "approach"),
            None,
        )
        a_detail = (approach_result or {}).get("detail") or {}
        reason = a_detail.get("reason")
        if reason in ("fill", "bottom"):
            flag("soft_target_arrival",
                 "arrival was decided by vision only (%s) — sonar did not confirm" % reason)

        lost_events = sum(
            1 for c in approach_cycles
            if "hop_back" in str(c.get("action") or "") or "relocate_look" in str(c.get("action") or "")
        )
        if lost_events >= 2:
            flag("target_lost_repeatedly",
                 "the target was lost and re-acquired %d time(s) during the approach" % lost_events)

        cycles_used = a_detail.get("cycles", len(approach_cycles))
        budget = config.get_int("APPROACH_MAX_CYCLES", 20)
        if budget > 0 and cycles_used / budget > 0.5:
            flag("approach_budget_overrun",
                 "used %d of %d available cycles (>50%%)" % (cycles_used, budget))

        if any(c.get("gate") == "low_confirmed" for c in approach_cycles):
            flag("low_confidence_steering",
                 "at least one low-confidence detection was used to steer during the approach")

        rejected = [c for c in approach_cycles if c.get("reject_reason")]
        if rejected:
            flag("hallucinated_box_rejected",
                 "%d detection(s) were rejected as implausible (%s) — counted, not believed"
                 % (len(rejected), ", ".join(sorted({c["reject_reason"] for c in rejected}))))

        if any(c.get("arrival_block") == "arrival_contradicted" for c in approach_cycles):
            flag("false_arrival_rejected",
                 "an apparent arrival was rejected because the arrival check contradicted it")

        if reason in ("arrival_contradicted", "target_not_found") and "arrived" not in str(reason):
            flag("arrival_unconfirmed",
                 "the approach ended without a confirmed arrival")

        blurry = sum(1 for c in mission.checks
                     if c.get("kind") == "skipped" and c.get("reason") == "blurry")
        if blurry:
            flag("blurry_frames_skipped",
                 "%d frame(s) were too blurry to check and were skipped" % blurry)

        residual_limit = config.get_float("APPROACH_RESIDUAL_FLAG_E", 0.30)
        residual_hits = [c for c in approach_cycles if c.get("phase") == "lock"
                        and isinstance(c.get("residual_e"), (int, float))
                        and abs(c["residual_e"]) > residual_limit]
        if residual_hits:
            flag("bearing_residual_high",
                 "the pivot toward the target left a residual offset of %.2f — check pan/turn calibration"
                 % residual_hits[0]["residual_e"])

        if config.get_float("GIMBAL_DEG_PER_UNIT", 0.0) <= 0:
            flag("gimbal_deg_per_unit_unmeasured",
                 "GIMBAL_DEG_PER_UNIT is unmeasured — the search fell back to centre-only looks")

        if config.get_int("APPROACH_TILT_OFFSET", 0) == 0:
            flag("tilt_offset_unmeasured",
                 "APPROACH_TILT_OFFSET is 0 — the approach gimbal tilt has likely not been measured")

        if any(c.get("arrival_block") == "arrival_without_motion" for c in approach_cycles):
            flag("arrival_without_motion",
                 "arrival fired on sonar with zero prior hops")

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
                 "rover_summary", "route_summary", "room_grounding", "looked")


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
