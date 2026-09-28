"""The virtual planner profile — no follow_line, no line sensor. `approach` is
a navigation skill (the rover finds its own way); `move`/`turn` are literal
and need an explicit magnitude. See
Progress/spec-planner-profiles-and-virtual-sweep.md §3C.

Prompt text only. planner.py assembles this into the actual prompt; nothing
here calls a model. If this text changes, it goes into Ch3 as the system
under test — flag the change rather than editing quietly.
"""

PROFILE_NAME = "virtual"

ACTIONS = {
    "approach": "go to a named thing in the room; the rover finds its own way around obstacles",
    "move": "drive straight forward or backward by a distance, with no obstacle avoidance",
    "turn": "rotate on the spot, left or right, by an angle",
    "scan": "sweep the camera around without moving the base",
    "observe": "turn to face a named thing and look at it",
    "stop": "halt",
    "report": "say what was found",
}

GUIDANCE = ("To go to a thing, use approach with its exact name from the room list. "
            "Use move and turn only when the command gives explicit directions; then give "
            "every move a distance_m and every turn a degrees value, in the order given.")

STEP_SCHEMA = ('{"steps": [{"id": 1, "action": "...", "target": "...", '
               '"distance_m": <number, move only>, "degrees": <number, turn only>}], "notes": "..."}')
