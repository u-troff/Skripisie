import hashlib
import json
import time

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
    if profile is planner_pi and config.get("NAV_MODE", "line").strip().lower() == "free":
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

    steps = plan.get("steps", [])
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
