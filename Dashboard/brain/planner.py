import hashlib
import json
import re
import time
from typing import Optional

import config
import planner_pi
import planner_virtual
from log_setup import get_logger
from providers import ProviderError, get_provider, log_completion, user_message

log = get_logger("planner")

# Backward-compat alias: the Pi vocabulary, unchanged in shape from before the
# profile split. Nothing in this codebase imports it any more (generate_plan
# reads the active profile instead), but it's kept so nothing external that
# expects planner.ACTIONS breaks.
ACTIONS = planner_pi.ACTIONS

_PROFILES = {"pi": planner_pi, "virtual": planner_virtual}


def _profile():
    """PLANNER_PROFILE wins if set; otherwise pi for ROVER=pi, else virtual.
    Picking this per call (not once at import) is what lets a single process
    run the Pi prompt and the virtual prompt side by side in a benchmark."""
    name = config.get("PLANNER_PROFILE", "").strip().lower()
    if not name:
        name = "pi" if config.get("ROVER", "sim").lower() == "pi" else "virtual"
    return _PROFILES.get(name, planner_virtual)

def _guidance_for(profile) -> str:
    """NAV_MODE only matters for the pi profile (spec-free-roam-approach.md
    §C) — planner_virtual has no NAV_MODE concept, so other profiles are
    unaffected."""
    if profile is planner_pi and config.get("NAV_MODE", "free").strip().lower() == "free":
        return planner_pi.FREE_GUIDANCE
    return profile.GUIDANCE




def _build_vocabulary(actions: dict, guidance: str) -> str:
    """Same shape _VOCABULARY was built in before the split, just parameterised
    over a profile's ACTIONS/GUIDANCE instead of the old module-level constants."""
    return (
        "The rover can ONLY perform these actions:\n"
        + "\n".join(f'  "{verb}" - {what}' for verb, what in actions.items())
        + "\nIt has no arm. It cannot pick up, carry, open, push, or touch anything.\n"
        + guidance + "\n"
        'Every step\'s "action" must be exactly one of those verbs.'
    )


def profile_info() -> dict:
    """What's actually driving the planner right now — for report.py's
    models.planner_profile and the UI's header pill."""
    profile = _profile()
    vocabulary = _build_vocabulary(profile.ACTIONS, _guidance_for(profile))
    digest = hashlib.sha256((vocabulary + profile.STEP_SCHEMA).encode("utf-8")).hexdigest()[:12]
    return {"profile": profile.PROFILE_NAME, "prompt_sha": digest, "actions": dict(profile.ACTIONS)}


def build_plan_prompt(command: str, scene: str = "") -> str:
    """Behaviour-neutral extraction of generate_plan's prompt construction, so
    tools/snapshot_prompt.py can print the exact prompt without calling a
    model. Nothing here may change what generate_plan sends."""
    profile = _profile()
    vocabulary = _build_vocabulary(profile.ACTIONS, _guidance_for(profile))

    scene_block = ""
    if scene:
        scene_block = (
            "The room was filmed beforehand. Everything known to be in it:\n"
            + scene
            + "\nOnly reference things from that list. If the command needs something "
            "that is not there, say so in notes instead of inventing it.\n"
        )

    return (
        "You are a mission planner for an indoor rover. Break the command into "
        "a sequence of discrete, executable steps.\n"
        + vocabulary + "\n"
        + scene_block
        + f'Command: "{command}"\n'
        "Respond ONLY with JSON: " + profile.STEP_SCHEMA
    )


_DIRECTIONAL = re.compile(
    r"\b(forward|forwards|backward|backwards|back|ahead|reverse|straight|"
    r"\d+(\.\d+)?\s*(cm|m|metres?|meters?|steps?))\b",
    re.IGNORECASE,
)


def _promote_moves_to_approach(steps: list) -> None:
    """A small planner model often writes `move -> <object or place>`, which on
    the Pi is one timed pulse and then done. In free-roam Pi mode only
    `approach` runs the search/drive-up loop, so a move whose target is a
    thing rather than a direction becomes an approach."""
    if _profile() is not planner_pi:
        return
    if config.get("NAV_MODE", "line").strip().lower() != "free":
        return
    for step in steps:
        target = str(step.get("target") or "")
        if step.get("action") == "move" and target and not _DIRECTIONAL.search(target):
            log.info("[plan] step %s: move -> %r is not a direction, promoting to approach",
                     step.get("id"), target)
            step["action"] = "approach"


def generate_plan(command: str, scene: str = "") -> dict:
    prompt = build_plan_prompt(command, scene)

    started = time.perf_counter()
    try:
        provider = get_provider("planner")
    except ProviderError as exc:
        log.error("[plan] no provider: %s", exc)
        return {"steps": [], "notes": f"planner unavailable: {exc}"}

    log.info("[plan] -> %s/%s command=%r", provider.name, provider.model, command)
    log.debug("[plan] prompt:\n%s", prompt)

    try:
        completion = provider.complete([user_message(prompt)], json_mode=True)
    except ProviderError as exc:
        log.error("[plan] failed after %.1fs: %s", time.perf_counter() - started, exc)
        return {"steps": [], "notes": f"planner failed: {exc}"}

    log.info("[plan] <- %.1fs", completion.latency_s)
    log.debug("[plan] raw response:\n%s", completion.text)
    log_completion(log, "planner", "generate_plan", completion)

    try:
        plan = json.loads(completion.text)
    except (json.JSONDecodeError, TypeError):
        log.warning("[plan] unparseable JSON: %r", completion.text[:300])
        return {"steps": [], "notes": "planner returned unparseable output"}

    if not isinstance(plan, dict):
        log.warning("[plan] JSON was not an object: %r", completion.text[:200])
        return {"steps": [], "notes": "planner returned unusable output"}

    # A small model sometimes writes steps as strings or nests them oddly; keep
    # only well-formed step objects so nothing downstream calls .get on a str.
    raw_steps = plan.get("steps", [])
    steps = [s for s in raw_steps if isinstance(s, dict)] if isinstance(raw_steps, list) else []
    if not isinstance(raw_steps, list) or len(steps) != len(raw_steps):
        log.warning("[plan] dropped malformed step(s): %r", str(raw_steps)[:200])
        plan["notes"] = (str(plan.get("notes") or "") + " [malformed steps dropped]").strip()
    plan["steps"] = steps
    _promote_moves_to_approach(steps)
    log.info("[plan] %d step(s)", len(steps))
    for step in steps:
        log.info("       %s. %s -> %s", step.get("id"), step.get("action"), step.get("target"))
    return plan

def revise_plan(confirmed_plan: dict, remaining_steps: list, digest: list, command: str) -> dict:
    """Propose a revision given what the rover can now see.

    The planner only ever *proposes*; revision.classify decides in plain Python
    whether the human has to be asked.
    """
    prompt = (
        "You are re-checking a rover's plan while it is already moving.\n"
        f'Original command: "{command}"\n'
        f"Confirmed plan: {json.dumps(confirmed_plan)}\n"
        f"Steps not yet done: {json.dumps(remaining_steps)}\n"
        "What the rover has seen since departing, oldest first:\n"
        + "\n".join(f"  - {line}" for line in digest)
        + "\nOnly propose a change if what it sees makes the remaining steps wrong or "
        "impossible. Prefer keeping the same targets. If nothing needs to change, say so.\n"
        "Respond ONLY with JSON: "
        '{"change": true/false, "reason": "...", '
        '"steps": [{"id": 1, "action": "...", "target": "..."}]}'
    )

    try:
        provider = get_provider("planner")
        completion = provider.complete([user_message(prompt)], json_mode=True)
    except ProviderError as exc:
        log.error("[revise] failed: %s", exc)
        return {"change": False, "reason": f"revision unavailable: {exc}", "steps": []}

    log.info("[revise] <- %.1fs", completion.latency_s)
    log_completion(log, "planner", "revise_plan", completion)

    try:
        return json.loads(completion.text)
    except (json.JSONDecodeError, TypeError):
        log.warning("[revise] unparseable JSON: %r", completion.text[:300])
        return {"change": False, "reason": "unparseable revision output", "steps": []}


def _allowed_actions(profile) -> dict:
    """No floor-line node in free-roam Pi mode, so follow_line is not offered on a replan."""
    actions = dict(profile.ACTIONS)
    if profile is planner_pi and config.get("NAV_MODE", "free").strip().lower() == "free":
        actions.pop("follow_line", None)
    return actions



# -- failure recovery (spec-supervisor-feedback-2026-10-05.md section D) -----
def validate_steps(steps) -> Optional[str]:
    """The rover's capability vocabulary as a gate; None means the steps pass.

    generate_plan only states the vocabulary in the prompt, and report.py audits it
    after the fact. A replan applied mid-mission has no spoken read-back, so it is
    checked here before it is allowed to replace the plan."""
    profile = _profile()
    if not isinstance(steps, list) or not steps:
        return "no steps"
    for step in steps:
        if not isinstance(step, dict):
            return "a step is not an object"
        action = str(step.get("action") or "").strip().lower()
        if action not in _allowed_actions(profile):
            return "action %r is not in the rover's vocabulary" % action
    return None


def build_replan_prompt(command: str, done_steps: list, failed_step: dict, reason: str,
                        guidance_log: list, digest: list, scene: str = "") -> str:
    """Pure prompt builder (tools/context_budget.py counts it for per_guidance_turn)."""
    profile = _profile()
    vocabulary = _build_vocabulary(_allowed_actions(profile), _guidance_for(profile))
    keep = config.get_int("PERCEPTION_DIGEST_KEEP", 6)
    lines = [
        "You are a mission planner for an indoor rover. The rover stopped partway through a "
        "mission because a step failed, and a person has told it what to do next.",
        vocabulary,
    ]
    if scene:
        lines.append("The room was filmed beforehand. Everything known to be in it:\n" + scene
                     + "\nOnly reference things from that list.")
    lines.append('Original command: "%s"' % command)
    lines.append("Steps already completed: " + (json.dumps(done_steps) if done_steps else "none"))
    lines.append("The step that failed: %s (reason: %s)" % (json.dumps(failed_step), reason))
    if digest:
        lines.append("What the rover has seen recently, oldest first:")
        lines.extend("  - %s" % line for line in digest[-keep:])
    lines.append("Conversation since the failure:")
    for turn in guidance_log:
        lines.append("  Rover: " + str(turn.get("question") or ""))
        lines.append("  Person: " + str(turn.get("answer") or "(no answer yet)"))
    
    lines.append(
        "Write a new plan for ONLY what is still left to do, starting from where the rover is "
        "now. Keep the original goal. Use the person's answer to decide where to look or what to "
        "reach instead: if they say where the target is, turn that way and then use approach on "
        "the target again, because their answer is the reason to expect it to work now. Do not "
        "repeat the failed step unchanged without using their answer. The plan must still end by "
        "reaching the target (approach, then observe or report).")

    lines.append("Respond ONLY with JSON: " + profile.STEP_SCHEMA)
    return "\n".join(lines)


def replan_from_state(command: str, done_steps: list, failed_step: dict, reason: str,
                      guidance_log: list, digest: list, scene: str = "") -> dict:
    """A new plan from where the rover is, after a human answered "what next?".
    {"steps": []} (with a note) on any failure; never raises."""
    prompt = build_replan_prompt(command, done_steps, failed_step, reason, guidance_log, digest, scene)
    try:
        provider = get_provider("planner")
        completion = provider.complete([user_message(prompt)], json_mode=True)
    except ProviderError as exc:
        log.error("[replan] failed: %s", exc)
        return {"steps": [], "notes": "replan unavailable: %s" % exc}
    log.info("[replan] <- %.1fs", completion.latency_s)
    log_completion(log, "planner", "replan_from_state", completion)

    try:
        plan = json.loads(completion.text)
    except (json.JSONDecodeError, TypeError):
        log.warning("[replan] unparseable JSON: %r", completion.text[:300])
        return {"steps": [], "notes": "replan returned unparseable output"}
    steps = plan.get("steps") if isinstance(plan, dict) else None
    if not isinstance(steps, list):
        return {"steps": [], "notes": "replan returned no steps"}
    _promote_moves_to_approach(steps)
    problem = validate_steps(steps)
    if (not problem and str(failed_step.get("action") or "").lower() == "approach"
        and not any(str(s.get("action") or "").strip().lower() == "approach" for s in steps)):
        problem = "the plan never approaches anything, so the failed step is not retried"
    if problem:
        log.warning("[replan] rejected: %s", problem)
        return {"steps": [], "notes": "replan rejected: " + problem}
    for index, step in enumerate(steps, start=1):
        step["id"] = index
    plan["steps"] = steps
    return plan
