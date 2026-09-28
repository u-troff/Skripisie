"""The Pi planner profile — verbatim, byte-identical to the prompt this
project has always sent for ROVER=pi. Proven by tools/snapshot_prompt.py
(Progress/spec-planner-profiles-and-virtual-sweep.md §2).

Prompt text only. planner.py assembles this into the actual prompt; nothing
here calls a model.
"""

PROFILE_NAME = "pi"

# The rover drives and looks. Nothing else. A plan step it cannot perform is a
# failed plan, not a partial success, so the vocabulary is stated up front
# rather than filtered afterwards.
ACTIONS = {
    "follow_line": ("follow the floor line to its end marker — the only way to "
                    "travel more than a short distance"),
    "move": "drive forward or backward",
    "turn": "rotate on the spot",
    "approach": "drive toward a visible target until close to it",
    "scan": "sweep the camera around without moving the base",
    "observe": "hold still and look at a named target",
    "stop": "halt",
    "report": "say what was found",
}

GUIDANCE = (
    "To go somewhere, use one follow_line step whose target is what should be "
    "visible at the end, then observe that target, then report."
)

STEP_SCHEMA = '{"steps": [{"id": 1, "action": "...", "target": "..."}], "notes": "..."}'
