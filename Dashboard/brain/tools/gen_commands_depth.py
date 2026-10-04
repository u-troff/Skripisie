#!/usr/bin/env python3
"""Builds tools/sweeps/commands_depth.json, the RQ2 chained-instruction suite.

    venv\\Scripts\\python.exe tools\\gen_commands_depth.py

Every chain is written here as (spoken text, the ideal step list, depth,
kind). Ground truth is not hand-computed: the ideal steps are executed on a
real VirtualRover (same OccupancyGrid, same A*, same literal move/turn code the
sweep will drive), so expected_visits and expected_final are exactly what a
perfect planner would have produced, and a chain that would collide or have
no path when executed exactly is rejected here rather than silently written.
For literal chains the end pose is also computed independently with plain
trigonometry and cross-checked against the simulation.

The room is rooms/room_tour1.json, start pose (0.8, 0.5, 90 deg).

Depth = the number of instructions the user stated, "turn around" included.
"""

import json
import math
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

BRAIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BRAIN_DIR))

import room_map  # noqa: E402
import rover as rover_mod  # noqa: E402

ROOM_PATH = BRAIN_DIR / "rooms" / "room_tour1.json"
OUT_PATH = BRAIN_DIR / "tools" / "sweeps" / "commands_depth.json"
TOL_M = 0.15


def A(target: str) -> dict:
    return {"action": "approach", 
    "target": target}


def MOVE(metres: float, back: bool = False) -> dict:
    return {"action": "move", "target": "backward" if back else "forward", "distance_m": metres}


def TURN(direction: str, degrees: float) -> dict:
    return {"action": "turn", "target": direction, "degrees": degrees}


# -- the suite -------------------------------------------------------------
# (text, steps, kind, probe, expected_visits_override)
# expected_visits is derived from the approach steps unless overridden.
APPROACH = [
    ("Go to the couch.",
     [A("couch")]),
    ("Go to the desk, then the bookshelf.",
     [A("desk"), A("bookshelf")]),
    ("Go to the window, then the coffee table, and then the door.",
     [A("window"), A("coffee table"), A("door")]),

    ("Go to the couch, then the coffee table, then the desk, and then the bookshelf.",
     [A("couch"), A("coffee table"), A("desk"), A("bookshelf")]),
    ("Head over to the door, then the bookshelf, then the window, and finally the couch.",
     [A("door"), A("bookshelf"), A("window"), A("couch")]),

    ("Go to the couch, then the coffee table, then the desk, then the bookshelf, then the door, and then the window.",
     [A("couch"), A("coffee table"), A("desk"), A("bookshelf"), A("door"), A("window")]),
    ("Start at the window, then go to the door, then the bookshelf, then the couch, then the desk, and finally the coffee table.",
     [A("window"), A("door"), A("bookshelf"), A("couch"), A("desk"), A("coffee table")]),

    ("Go to the couch, then the coffee table, then the desk, then the window, then the door, then the bookshelf, "
     "then the couch again, and finally the desk again.",
     [A("couch"), A("coffee table"), A("desk"), A("window"), A("door"), A("bookshelf"), A("couch"), A("desk")]),
    ("Go to the bookshelf, then the door, then the coffee table, then the window, then the couch, then the desk, "
     "then the bookshelf again, and finally the window again.",
     [A("bookshelf"), A("door"), A("coffee table"), A("window"), A("couch"), A("desk"), A("bookshelf"), A("window")]),

    ("Go to the couch, then the coffee table, then the desk, then the bookshelf, then the door, then the window, "
     "then the couch again, then the desk again, then the coffee table again, and finally the bookshelf again.",
     [A("couch"), A("coffee table"), A("desk"), A("bookshelf"), A("door"), A("window"),
      A("couch"), A("desk"), A("coffee table"), A("bookshelf")]),
    ("Go to the door, then the window, then the desk, then the bookshelf, then the coffee table, then the couch, "
     "then the window again, then the door again, then the bookshelf again, and finally the couch again.",
     [A("door"), A("window"), A("desk"), A("bookshelf"), A("coffee table"), A("couch"),
      A("window"), A("door"), A("bookshelf"), A("couch")]),

    ("Go to the couch, then the coffee table, then the desk, then the bookshelf, then the door, then the window, "
     "then the couch again, then the desk again, then the coffee table again, then the door again, "
     "then the bookshelf again, and finally the window again.",
     [A("couch"), A("coffee table"), A("desk"), A("bookshelf"), A("door"), A("window"),
      A("couch"), A("desk"), A("coffee table"), A("door"), A("bookshelf"), A("window")]),
    ("Go to the window, then the bookshelf, then the couch, then the door, then the coffee table, then the desk, "
     "then the door again, then the bookshelf again, then the window again, then the couch again, "
     "then the desk again, and finally the coffee table again.",
     [A("window"), A("bookshelf"), A("couch"), A("door"), A("coffee table"), A("desk"),
      A("door"), A("bookshelf"), A("window"), A("couch"), A("desk"), A("coffee table")]),
]

LITERAL = [
    ("Turn right 90 degrees, then drive forward 1.5 metres.",
     [TURN("right", 90), MOVE(1.5)]),
    ("Move forward 1 metre, turn right 90 degrees, move forward 80 centimetres, then turn left 90 degrees.",
     [MOVE(1.0), TURN("right", 90), MOVE(0.8), TURN("left", 90)]),
    ("Move forward half a metre, turn right 90 degrees, move forward 0.9 metres, turn left 90 degrees, "
     "move forward 0.8 metres, then turn left 90 degrees.",
     [MOVE(0.5), TURN("right", 90), MOVE(0.9), TURN("left", 90), MOVE(0.8), TURN("left", 90)]),
    ("Move forward 0.6 metres, turn right 90 degrees, move forward 0.6 metres, turn right 90 degrees, "
     "move forward 0.6 metres, turn right 90 degrees, move forward 0.6 metres, then turn right 90 degrees.",
     [MOVE(0.6), TURN("right", 90), MOVE(0.6), TURN("right", 90),
      MOVE(0.6), TURN("right", 90), MOVE(0.6), TURN("right", 90)]),
    ("Move forward 0.5 metres, turn right 90 degrees, move forward 1.4 metres, turn right 90 degrees, "
     "move forward 0.5 metres, turn right 90 degrees, move forward 1.4 metres, then turn right 90 degrees.",
     [MOVE(0.5), TURN("right", 90), MOVE(1.4), TURN("right", 90),
      MOVE(0.5), TURN("right", 90), MOVE(1.4), TURN("right", 90)]),
]

# Mixed: approach + turn + move. Heading after an approach is the direction of
# the last leg of the A* route, so these are only meaningful because the
# generator executes them; the turn/move after each approach is chosen so the
# exact execution is collision-free (see the "adjusted" report this prints).
MIXED = [
    ("Go to the coffee table, turn right 90 degrees, move forward half a metre, then go to the bookshelf.",
     [A("coffee table"), TURN("right", 90), MOVE(0.5), A("bookshelf")]),
    ("Go to the desk, turn around, move forward 0.5 metres, turn left 90 degrees, move forward 0.4 metres, "
     "then go to the door.",
     [A("desk"), TURN("left", 180), MOVE(0.5), TURN("left", 90), MOVE(0.4), A("door")]),
    ("Go to the bookshelf, turn left 90 degrees, move forward 0.6 metres, turn around, move forward 0.6 metres, "
     "go to the window, turn right 45 degrees, then move back half a metre.",
     [A("bookshelf"), TURN("left", 90), MOVE(0.6), TURN("left", 180), MOVE(0.6),
      A("window"), TURN("right", 45), MOVE(0.5, back=True)]),
]

# Probes, depth 8. expected_visits is what a correct system does BEFORE the
# bad step: mission.py stops the mission on the first blocked step ("I am
# blocked and have stopped"), so nothing after an unresolvable step runs.
# A planner that quietly drops the bad step, or swaps in a real landmark,
# will not match — that is the point.
PROBES = [
    ("Go to the couch, then the coffee table, then the desk, then go to the fridge, then the bookshelf, "
     "then the door, then the window, and then the couch again.",
     "unknown_target",
     [A("couch"), A("coffee table"), A("desk"), A("fridge"), A("bookshelf"), A("door"), A("window"), A("couch")],
     ["couch", "coffee table", "desk"]),
    ("Go to the door, then the couch, then the coffee table, then the desk, then the bookshelf, "
     "then the window, then the door again, and finally go to the television.",
     "unknown_target",
     [A("door"), A("couch"), A("coffee table"), A("desk"), A("bookshelf"), A("window"), A("door"), A("television")],
     ["door", "couch", "coffee table", "desk", "bookshelf", "window", "door"]),
    ("Go to the window, then the door, then the couch, then the coffee table, then pick up the lamp, "
     "then go to the desk, then the bookshelf, and finally the window again.",
     "out_of_vocab",
     [A("window"), A("door"), A("couch"), A("coffee table"), {"action": "pick_up", "target": "lamp"},
      A("desk"), A("bookshelf"), A("window")],
     ["window", "door", "couch", "coffee table"]),
]


def _simulate(room, steps: List[dict], tmp: Path) -> Dict:
    r = rover_mod.VirtualRover(room, time_scale=1e9, drift_frac=0.0, drift_deg=0.0,
                               trace_dir=tmp, seed=1)
    outcomes = []
    for i, step in enumerate(steps, start=1):
        outcomes.append(r.execute_step(dict(step, id=i)))
    summary = r.summary()
    return {"summary": summary, "outcomes": outcomes}


def _trig_final(start, steps: List[dict]):
    x, y, th = start
    for s in steps:
        if s["action"] == "move":
            d = -s["distance_m"] if "backward" in s["target"] else s["distance_m"]
            x += d * math.cos(math.radians(th))
            y += d * math.sin(math.radians(th))
        elif s["action"] == "turn":
            th += s["degrees"] if s["target"] == "left" else -s["degrees"]
    return x, y


def build() -> List[dict]:
    room = room_map.load_room(str(ROOM_PATH))
    tmp = Path(tempfile.mkdtemp(prefix="depthsuite_"))
    start = (room.start_x, room.start_y, room.start_theta)
    entries: List[dict] = []
    problems: List[str] = []

    # Landmark sanity: every standing spot free on the inflated grid and
    # reachable from the start pose (otherwise every approach chain is moot).
    grid = rover_mod.nav.OccupancyGrid(room, rover_mod.footprint_clearance_m())
    for lm in room.landmarks:
        free = grid.free(*lm.point())
        path = rover_mod.nav.plan_path(grid, (start[0], start[1]), lm.point())
        print("landmark %-13s free=%s reachable=%s" % (lm.name, free, path is not None))
        if not free or path is None:
            problems.append("landmark %s not free/reachable" % lm.name)
    for word in ("fridge", "television", "lamp"):
        landmark, how = room.resolve(word)
        print("probe word %-10s resolves to %r (%s)" % (word, landmark.name if landmark else None, how))
        if landmark is not None:
            problems.append("probe word %r resolves to %s via %s — not a valid probe" % (word, landmark.name, how))

    def add(text, steps, kind, probe=None, visits_override=None):
        sim = _simulate(room, steps, tmp)
        s = sim["summary"]
        bad = [o for o in sim["outcomes"] if o["status"] != "ok"]
        if kind != "probe" and (bad or s["collisions"] or s["no_path_count"]):
            problems.append("NOT COLLISION-FREE: %r -> %s" % (text, bad))
        if kind == "probe":
            expected_visits = visits_override
        else:
            expected_visits = [st["target"] for st in steps if st["action"] == "approach"]
            if s["visits"] != expected_visits:
                problems.append("visits mismatch for %r: %s vs %s" % (text, s["visits"], expected_visits))
        expected_final = None
        if kind in ("literal", "mixed"):
            fp = s["final_pose"]
            expected_final = {"x": round(fp["x"], 3), "y": round(fp["y"], 3), "tol_m": TOL_M}
            if kind == "literal":
                tx, ty = _trig_final(start, steps)
                if math.hypot(tx - fp["x"], ty - fp["y"]) > 0.03:
                    problems.append("trig/sim disagree for %r: (%.3f,%.3f) vs (%.3f,%.3f)"
                                    % (text, tx, ty, fp["x"], fp["y"]))
        entries.append({
            "command": text,
            "depth": len(steps),
            "kind": kind,
            "expected_visits": expected_visits,
            "expected_final": expected_final,
            "probe": probe,
        })
        print("%-8s d=%-2d final=%s visits=%s" % (kind, len(steps), expected_final, expected_visits))

    for text, steps in APPROACH:
        add(text, steps, "approach")
    for text, steps in LITERAL:
        add(text, steps, "literal")
    for text, steps in MIXED:
        add(text, steps, "mixed")
    for text, probe, steps, visits in PROBES:
        add(text, steps, "probe", probe=probe, visits_override=visits)

    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print("  -", p)
        sys.exit(1)
    return entries


def main() -> None:
    entries = build()
    OUT_PATH.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    print("\nwrote %s (%d commands)" % (OUT_PATH, len(entries)))


if __name__ == "__main__":
    main()
