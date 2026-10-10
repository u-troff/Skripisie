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
    "approach": ("find a visible object and drive up to it (searches by "
                "turning if it is not in view)"),
    "scan": "sweep the camera around without moving the base",
    "observe": "hold still and look at a named target",
    "stop": "halt",
    "report": "say what was found, and is around the object",    
    "check": ("look once and decide whether a stated condition is true (yes or no); "
              "steps tagged \"when\" run only for the matching answer"),

}

GUIDANCE = (
    "To go somewhere, use one follow_line step whose target is what should be "
    "visible at the end, then observe that target, then report."
)
CONDITIONAL_GUIDANCE = (
    'The command contains a condition. First add one "check" step whose target is the condition '
    'as a short statement, e.g. "the orange box is visible". Then give the steps for the yes case, '
    'each with "when": "yes", and the steps for the no case, each with "when": "no". Steps that '
    'always run have no "when". Example: [{"id": 1, "action": "check", "target": "the orange box is visible"}, '
    '{"id": 2, "action": "approach", "target": "orange box", "when": "yes"}, '
    '{"id": 3, "action": "report", "target": "orange box", "when": "yes"}, '
    '{"id": 4, "action": "turn", "target": "left", "when": "no"}, '
    '{"id": 5, "action": "stop", "target": null, "when": "no"}].'
)

FREE_GUIDANCE = ("To go to an object without a floor line, use approach with the object as target, then observe it, then report. "
                 "Never use move to reach an object or place: move is only a short hop forward or backward.")
STEP_SCHEMA = '{"steps": [{"id": 1, "action": "...", "target": "..."}], "notes": "..."}'
