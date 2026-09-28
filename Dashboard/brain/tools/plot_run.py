#!/usr/bin/env python3
"""Turn a VirtualRover trace into an SVG figure for evaluation.tex.

    venv/bin/python tools/plot_run.py logs/virtual_run_20260911-204300.jsonl

Stdlib only on purpose. The trace embeds its own room map and settings
(clearance, robot dimensions), so this needs nothing but the file it is
given — see Progress/spec-planner-profiles-and-virtual-sweep.md §3G.
"""

import argparse
import json
import math
import sys
from pathlib import Path

MARGIN = 48
TARGET_W = 900
OK, BLOCKED, HALTED = "#2e8b57", "#c02626", "#d98c00"
PLANNED_PATH = "#3d78d1"
COLLISION = "#c02626"


def load_runs(path):
    runs = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("event") == "run_start":
            runs.append({"head": record, "steps": []})
        elif record.get("event") == "step" and runs:
            runs[-1]["steps"].append(record)
    return runs


def _footprint_corners(pose, length_m, width_m):
    """Room-frame corners of the rover's rectangle at `pose`, length along the
    heading, width across it."""
    cx, cy, theta = pose["x"], pose["y"], math.radians(pose["theta"])
    half_l, half_w = length_m / 2.0, width_m / 2.0
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    local = [(half_l, half_w), (half_l, -half_w), (-half_l, -half_w), (-half_l, half_w)]
    return [(cx + lx * cos_t - ly * sin_t, cy + lx * sin_t + ly * cos_t) for lx, ly in local]


def _inflated_box(polygon, clearance_m):
    """Approximate Minkowski inflation by clearance_m: exact for the
    axis-aligned rectangles every fixture obstacle currently is, a reasonable
    envelope otherwise. Good enough for "does the route look right", which is
    what this figure is for."""
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return [
        (min(xs) - clearance_m, min(ys) - clearance_m),
        (max(xs) + clearance_m, min(ys) - clearance_m),
        (max(xs) + clearance_m, max(ys) + clearance_m),
        (min(xs) - clearance_m, max(ys) + clearance_m),
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("trace")
    parser.add_argument("--run", type=int, default=-1, help="1-based; default last")
    parser.add_argument("-o", "--out", default=None)
    args = parser.parse_args()

    runs = load_runs(args.trace)
    if not runs:
        sys.exit("no runs in %s" % args.trace)
    run = runs[args.run - 1 if args.run > 0 else -1]
    head, steps = run["head"], run["steps"]
    room = head["room"]
    settings = head.get("settings") or {}
    clearance_m = settings.get("clearance_m")
    robot_length_m = settings.get("robot_length_m")
    robot_width_m = settings.get("robot_width_m")

    scale = TARGET_W / room["width_m"]
    width = room["width_m"] * scale + 2 * MARGIN
    height = room["height_m"] * scale + 2 * MARGIN

    def sx(x):
        return MARGIN + x * scale

    def sy(y):  # SVG y grows downward
        return MARGIN + (room["height_m"] - y) * scale

    def room_polygon(points, fill, stroke, dash=""):
        joined = " ".join("%.1f,%.1f" % (sx(p[0]), sy(p[1])) for p in points)
        return ('<polygon points="%s" fill="%s" stroke="%s" %s/>'
                % (joined, fill, stroke, 'stroke-dasharray="4 3"' if dash else ""))

    def polyline(points, colour, width_px=2, dash=""):
        joined = " ".join("%.1f,%.1f" % (sx(p[0]), sy(p[1])) for p in points)
        return ('<polyline points="%s" fill="none" stroke="%s" stroke-width="%d" %s/>'
                % (joined, colour, width_px, 'stroke-dasharray="6 4"' if dash else ""))

    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%.0f" height="%.0f" '
           'font-family="Helvetica,Arial,sans-serif" font-size="12">' % (width, height),
           '<rect width="100%%" height="100%%" fill="white"/>',
           '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="none" '
           'stroke="#222" stroke-width="2"/>' % (sx(0), sy(room["height_m"]),
                                                 room["width_m"] * scale,
                                                 room["height_m"] * scale)]

    # Inflated footprint first (light, behind), then the obstacle itself.
    if clearance_m is not None:
        for obstacle in room["obstacles"]:
            inflated = _inflated_box(obstacle["polygon"], clearance_m)
            out.append(room_polygon(inflated, "#f3ecec", "#d9c8c8", dash=True))

    for obstacle in room["obstacles"]:
        points = " ".join("%.1f,%.1f" % (sx(p[0]), sy(p[1])) for p in obstacle["polygon"])
        cx = sum(sx(p[0]) for p in obstacle["polygon"]) / len(obstacle["polygon"])
        cy = sum(sy(p[1]) for p in obstacle["polygon"]) / len(obstacle["polygon"])
        out.append('<polygon points="%s" fill="#e2e2e2" stroke="#9a9a9a"/>' % points)
        out.append('<text x="%.1f" y="%.1f" fill="#666" text-anchor="middle">%s</text>'
                   % (cx, cy, obstacle["name"]))

    for landmark in room["landmarks"]:
        out.append('<circle cx="%.1f" cy="%.1f" r="4" fill="#2a6fdb"/>'
                   % (sx(landmark["x"]), sy(landmark["y"])))
        out.append('<text x="%.1f" y="%.1f" fill="#2a6fdb">%s</text>'
                   % (sx(landmark["x"]) + 7, sy(landmark["y"]) + 4, landmark["name"]))

    # Each approach's planned A* route, thin blue, under the travelled path.
    for record in steps:
        planned = record.get("planned_path")
        if planned:
            out.append(polyline(planned, PLANNED_PATH, width_px=1))

    start = head["pose"]
    achieved = [(start["x"], start["y"])] + [(s["pose"]["x"], s["pose"]["y"]) for s in steps]
    intended = [(start["x"], start["y"])] + [(s["intended"]["x"], s["intended"]["y"]) for s in steps]

    out.append(polyline(intended, "#aaaaaa", dash=True))
    out.append(polyline(achieved, "#444444"))

    # Footprint rectangle at the start and end poses.
    if robot_length_m and robot_width_m:
        end_pose = steps[-1]["pose"] if steps else start
        for pose, colour in ((start, "#2a6fdb"), (end_pose, "#444444")):
            corners = _footprint_corners(pose, robot_length_m, robot_width_m)
            out.append(room_polygon(corners, "none", colour))

    for index, record in enumerate(steps):
        pose = record["pose"]
        colour = {"ok": OK, "blocked": BLOCKED, "halted": HALTED}.get(record["status"], BLOCKED)
        px, py = sx(pose["x"]), sy(pose["y"])
        rad = math.radians(pose["theta"])
        out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s"/>'
                   % (px, py, px + 16 * math.cos(rad), py - 16 * math.sin(rad), colour))
        out.append('<circle cx="%.1f" cy="%.1f" r="5" fill="%s"/>' % (px, py, colour))
        label = str(record["step"].get("id") or index + 1)
        if record["status"] != "ok":
            label += " %s" % (record.get("reason") or record["status"])
        out.append('<text x="%.1f" y="%.1f" fill="%s">%s</text>' % (px + 8, py - 8, colour, label))

        # Collision points get their own marker: an X at the pose, on top of
        # the step's own circle, so a collision reads at a glance.
        if record.get("reason") == "collision":
            arm = 6
            out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="2"/>'
                       % (px - arm, py - arm, px + arm, py + arm, COLLISION))
            out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="2"/>'
                       % (px - arm, py + arm, px + arm, py - arm, COLLISION))

    blocked = sum(1 for s in steps if s["status"] == "blocked")
    ghosts = sum(1 for s in steps if s.get("reason") == "unresolved_target")
    collisions = sum(1 for s in steps if s.get("reason") == "collision")
    odometer_m = steps[-1].get("odometer_m") if steps else None
    sim_time_s = sum(s.get("sim_time_s") or 0.0 for s in steps)
    caption = ("%s — run %d, seed %d — %d step(s), %d blocked, %d unresolved target(s), "
              "%d collision(s)" % (room["name"], head["run"], head["seed"],
                                   len(steps), blocked, ghosts, collisions))
    if odometer_m is not None:
        caption += "; odometer %.2fm" % odometer_m
    caption += "; sim time %.1fs" % sim_time_s
    caption += "; dashed grey = intended, solid = achieved, blue = planned route"
    out.append('<text x="%.1f" y="%.1f" fill="#222">%s</text>' % (MARGIN, height - 16, caption))
    out.append("</svg>")

    destination = Path(args.out) if args.out else Path(args.trace).with_suffix(".svg")
    destination.write_text("\n".join(out), encoding="utf-8")
    print("%s — %d step(s), %d blocked, %d unresolved, %d collisions"
         % (destination, len(steps), blocked, ghosts, collisions))


if __name__ == "__main__":
    main()
