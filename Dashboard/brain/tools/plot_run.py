#!/usr/bin/env python3
"""Turn a VirtualRover trace into an SVG figure for evaluation.tex.

    venv/bin/python tools/plot_run.py logs/virtual_run_20260911-204300.jsonl

Stdlib only on purpose. The trace embeds its own room map, so this needs
nothing but the file it is given.
"""

import argparse
import json
import math
import sys
from pathlib import Path

MARGIN = 48
TARGET_W = 900
OK, BLOCKED, HALTED = "#2e8b57", "#c02626", "#d98c00"


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

    scale = TARGET_W / room["width_m"]
    width = room["width_m"] * scale + 2 * MARGIN
    height = room["height_m"] * scale + 2 * MARGIN

    def sx(x):
        return MARGIN + x * scale

    def sy(y):  # SVG y grows downward
        return MARGIN + (room["height_m"] - y) * scale

    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%.0f" height="%.0f" '
           'font-family="Helvetica,Arial,sans-serif" font-size="12">' % (width, height),
           '<rect width="100%%" height="100%%" fill="white"/>',
           '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="none" '
           'stroke="#222" stroke-width="2"/>' % (sx(0), sy(room["height_m"]),
                                                 room["width_m"] * scale,
                                                 room["height_m"] * scale)]

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

    start = head["pose"]
    achieved = [(start["x"], start["y"])] + [(s["pose"]["x"], s["pose"]["y"]) for s in steps]
    intended = [(start["x"], start["y"])] + [(s["intended"]["x"], s["intended"]["y"]) for s in steps]

    def polyline(points, colour, dash=""):
        joined = " ".join("%.1f,%.1f" % (sx(p[0]), sy(p[1])) for p in points)
        return ('<polyline points="%s" fill="none" stroke="%s" stroke-width="2" %s/>'
                % (joined, colour, 'stroke-dasharray="6 4"' if dash else ""))

    out.append(polyline(intended, "#aaaaaa", dash=True))
    out.append(polyline(achieved, "#444444"))

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

    blocked = sum(1 for s in steps if s["status"] == "blocked")
    ghosts = sum(1 for s in steps if s.get("reason") == "unresolved_target")
    out.append('<text x="%.1f" y="%.1f" fill="#222">%s — run %d, seed %d — %d step(s), '
               '%d blocked, %d unresolved target(s); dashed = intended, solid = drifted</text>'
               % (MARGIN, height - 16, room["name"], head["run"], head["seed"],
                  len(steps), blocked, ghosts))
    out.append("</svg>")

    destination = Path(args.out) if args.out else Path(args.trace).with_suffix(".svg")
    destination.write_text("\n".join(out), encoding="utf-8")
    print("%s — %d step(s), %d blocked, %d unresolved" % (destination, len(steps), blocked, ghosts))


if __name__ == "__main__":
    main()
