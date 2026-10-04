#!/usr/bin/env python3
"""Builds a depth suite for every room in rooms/ except room_tour1, whose
suite (tools/sweeps/commands_depth.json) is hand-authored by
gen_commands_depth.py and left alone.

    venv\\Scripts\\python.exe tools\\gen_room_suites.py            # all other rooms
    venv\\Scripts\\python.exe tools\\gen_room_suites.py room_loft  # one room

Writes tools/sweeps/commands_depth_<room>.json with the same entry format and
the same shape as the tour1 suite: approach chains at depth 1, 2, 3 and two
orderings at 4, 6, 8, 10, 12 (revisits from 8 up), literal chains at 2, 4, 6
and two at 8 (closed square + rectangle), mixed chains at 4, 6, 8, and three
depth-8 probes.

Differences from tour1's suite: landmark orderings are seeded-random (the seed
is the room name, so reruns are identical), and literal/mixed distances and
turn directions are SEARCHED rather than typed: for each chain the generator
tries turn directions and decreasing distances until the chain executes with
no collision on the real VirtualRover (inflated grid, from that room's own
start pose), then keeps the first that works. Ground truth comes from that
execution, exactly as in gen_commands_depth.py. Anything that can't be made
collision-free is reported and the script exits non-zero.
"""

import itertools
import json
import random
import sys
import tempfile
import zlib
from pathlib import Path
from typing import Dict, List, Optional

BRAIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BRAIN_DIR))
sys.path.insert(0, str(Path(__file__).parent))

import gen_commands_depth as g  # noqa: E402
import room_map  # noqa: E402
import rover as rover_mod  # noqa: E402

ROOMS_DIR = BRAIN_DIR / "rooms"
OUT_DIR = BRAIN_DIR / "tools" / "sweeps"
SKIP = {"room_tour1"}

A, MOVE, TURN = g.A, g.MOVE, g.TURN
BAD = {"fridge": {"action": "approach", "target": "fridge"},
       "television": {"action": "approach", "target": "television"},
       "lamp": {"action": "pick_up", "target": "lamp"}}


# -- text ------------------------------------------------------------------
def _clause(step: dict, prev_was_approach: bool, seen: set) -> str:
    action, target = step["action"], step["target"]
    if action == "approach":
        again = " again" if target in seen else ""
        if prev_was_approach:
            return "the %s%s" % (target, again)
        return "go to the %s%s" % (target, again)
    if action == "pick_up":
        return "pick up the %s" % target
    if action == "move":
        d = step["distance_m"]
        if d == 0.5:
            amount = "half a metre"
        elif d == 1.0:
            amount = "1 metre"
        elif d < 1.0 and round(d * 100) % 10 == 0 and d != 0.5:
            amount = "%d centimetres" % round(d * 100)
        else:
            amount = "%g metres" % d
        if "backward" in target:
            return "move back %s" % amount
        return "move forward %s" % amount
    if step["degrees"] == 180:
        return "turn around"
    return "turn %s %g degrees" % (step["target"], step["degrees"])


def render(steps: List[dict]) -> str:
    pure_approach = all(s["action"] in ("approach",) for s in steps)
    parts, seen, prev = [], set(), False
    for i, s in enumerate(steps):
        clause = _clause(s, prev, seen)
        if s["action"] == "approach":
            seen.add(s["target"])
        last = i == len(steps) - 1
        if i == 0:
            parts.append(clause[0].upper() + clause[1:])
        elif last and pure_approach and len(steps) >= 4:
            parts.append("and finally " + clause)
        elif last and len(steps) > 1:
            parts.append("then " + clause)
        else:
            parts.append("then " + clause)
        prev = s["action"] == "approach"
    return ", ".join(parts) + "."


# -- chains ------------------------------------------------------------------
def approach_seq(rng: random.Random, names: List[str], depth: int) -> List[str]:
    seq: List[str] = []
    while len(seq) < depth:
        block = names[:]
        rng.shuffle(block)
        if seq and block[0] == seq[-1]:
            block.append(block.pop(0))
        seq.extend(block)
    return seq[:depth]


def search_chain(room, tmp: Path, builders) -> Optional[List[dict]]:
    """First candidate (in order) that executes with every step ok."""
    for steps in builders:
        sim = g._simulate(room, steps, tmp)
        if all(o["status"] == "ok" for o in sim["outcomes"]):
            return steps
    return None


def literal_candidates(kind: str):
    sides = [1.0, 0.8, 0.6, 0.5, 0.4, 0.3]
    longs = [1.4, 1.0, 0.8, 0.6, 0.4]
    for sgn_name in ("right", "left"):
        opp = "left" if sgn_name == "right" else "right"
        for a in sides:
            if kind == "L2":
                yield [TURN(sgn_name, 90), MOVE(a)]
            elif kind == "L4":
                for b in longs:
                    yield [MOVE(a), TURN(sgn_name, 90), MOVE(b), TURN(opp, 90)]
            elif kind == "L6":
                for b in longs:
                    yield [MOVE(a), TURN(sgn_name, 90), MOVE(b), TURN(opp, 90), MOVE(a), TURN(opp, 90)]
            elif kind == "SQUARE":
                yield [MOVE(a), TURN(sgn_name, 90)] * 4
            elif kind == "RECT":
                for b in longs:
                    if b > a:
                        yield [MOVE(a), TURN(sgn_name, 90), MOVE(b), TURN(sgn_name, 90)] * 2


def mixed_candidates(kind: str, names: List[str], rng: random.Random):
    pairs = list(itertools.permutations(names, 2))
    rng.shuffle(pairs)
    moves = [0.6, 0.5, 0.4, 0.3]
    for l1, l2 in pairs:
        for sgn_name in ("left", "right"):
            opp = "left" if sgn_name == "right" else "right"
            for a in moves:
                if kind == "M4":
                    yield [A(l1), TURN(sgn_name, 90), MOVE(a), A(l2)]
                elif kind == "M6":
                    yield [A(l1), TURN(sgn_name, 180), MOVE(a), TURN(opp, 90), MOVE(a), A(l2)]
                elif kind == "M8":
                    yield [A(l1), TURN(sgn_name, 90), MOVE(a), TURN(sgn_name, 180), MOVE(a),
                           A(l2), TURN(opp, 45), MOVE(a, back=True)]


def build_room(path: Path) -> List[dict]:
    room = room_map.load_room(str(path))
    rng = random.Random(zlib.crc32(room.name.encode("utf-8")))
    names = [lm.name for lm in room.landmarks]
    tmp = Path(tempfile.mkdtemp(prefix="suite_%s_" % room.name))
    start = (room.start_x, room.start_y, room.start_theta)
    problems: List[str] = []
    entries: List[dict] = []
    adjusted: List[str] = []

    grid = rover_mod.nav.OccupancyGrid(room, rover_mod.footprint_clearance_m())
    for lm in room.landmarks:
        ok = grid.free(*lm.point()) and \
            rover_mod.nav.plan_path(grid, start[:2], lm.point()) is not None
        if not ok:
            problems.append("landmark %r not free/reachable from start" % lm.name)
    for word in BAD:
        found, how = room.resolve(word)
        if found is not None:
            problems.append("probe word %r resolves to %s (%s)" % (word, found.name, how))

    def add(steps, kind, probe=None, visits_override=None):
        text = render(steps)
        sim = g._simulate(room, steps, tmp)
        s = sim["summary"]
        if kind != "probe":
            bad = [o for o in sim["outcomes"] if o["status"] != "ok"]
            if bad or s["collisions"] or s["no_path_count"]:
                problems.append("NOT COLLISION-FREE: %r" % text)
            visits = [st["target"] for st in steps if st["action"] == "approach"]
            if s["visits"] != visits:
                problems.append("visits mismatch for %r" % text)
        else:
            visits = visits_override
        final = None
        if kind in ("literal", "mixed"):
            fp = s["final_pose"]
            final = {"x": round(fp["x"], 3), "y": round(fp["y"], 3), "tol_m": g.TOL_M}
        entries.append({"command": text, "depth": len(steps), "kind": kind,
                        "expected_visits": visits, "expected_final": final, "probe": probe})

    # approach: 1, 2, 3 once; 4..12 twice. (Capped by what the landmark list
    # supports without consecutive repeats, which approach_seq guarantees.)
    for depth in (1, 2, 3):
        add([A(n) for n in approach_seq(rng, names, depth)], "approach")
    for depth in (4, 6, 8, 10, 12):
        for _ in range(2):
            add([A(n) for n in approach_seq(rng, names, depth)], "approach")

    for depth, kind in ((2, "L2"), (4, "L4"), (6, "L6"), (8, "SQUARE"), (8, "RECT")):
        steps = search_chain(room, tmp, literal_candidates(kind))
        if steps is None:
            problems.append("no collision-free %s chain found" % kind)
            continue
        if kind in ("SQUARE", "RECT") and steps[0]["distance_m"] != (0.6 if kind == "SQUARE" else 0.5):
            adjusted.append("%s: used side %.1f m" % (kind, steps[0]["distance_m"]))
        add(steps, "literal")
    for kind in ("M4", "M6", "M8"):
        steps = search_chain(room, tmp, mixed_candidates(kind, names, rng))
        if steps is None:
            problems.append("no collision-free %s chain found" % kind)
            continue
        add(steps, "mixed")

    # Probes at depth 8: 7 real approaches + 1 bad step at position 4 / 8 / 5.
    for word, pos in (("fridge", 4), ("television", 8), ("lamp", 5)):
        real = approach_seq(rng, names, 7)
        steps = [A(n) for n in real]
        steps.insert(pos - 1, dict(BAD[word]))
        before = [s["target"] for s in steps[:pos - 1]]
        add(steps, "probe", probe="out_of_vocab" if word == "lamp" else "unknown_target",
            visits_override=before)

    if problems:
        print("%s PROBLEMS:" % room.name)
        for p in problems:
            print("  -", p)
        sys.exit(1)
    for note in adjusted:
        print("  adjusted:", note)
    return entries


def main() -> None:
    wanted = set(sys.argv[1:])
    for path in sorted(ROOMS_DIR.glob("*.json")):
        name = path.stem
        if name in SKIP or (wanted and name not in wanted):
            continue
        entries = build_room(path)
        out = OUT_DIR / ("commands_depth_%s.json" % name)
        out.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
        print("%s: wrote %s (%d commands)" % (name, out.name, len(entries)))


if __name__ == "__main__":
    main()
