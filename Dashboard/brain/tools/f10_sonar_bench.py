"""F10 - sonar bench for the 300 mm stop (spec-supervisor-feedback-2026-10-05.md C5). No models.

    venv/bin/python tools/f10_sonar_bench.py

For each case you place the obstacle and the rover, the rover hops 80 cm toward it, and you
measure the gap (cm, rover front to obstacle) with a tape measure. Pass = the rover is no
closer than 25 cm, or - for starts already inside 25 cm - it did not drive closer than it began.
A hop that returns "ok" with a gap under 25 cm is a sonar MISS: exactly the case where vision
arrival has to remain the fallback. Writes logs/f10_sonar_bench.csv.
"""
import csv
import sys
import time
from pathlib import Path

BRAIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BRAIN))

from rover import get_rover  # noqa: E402

HOP_CM = 80
MIN_GAP_CM = 25.0
# (label, start gap in cm or None = start ~70 cm away, reps)
CASES = [
    ("box square-on, start 50 cm", 50, 3),
    ("box square-on, start 30 cm", 30, 3),
    ("box square-on, start 20 cm", 20, 3),
    ("box 30 deg off-axis", None, 3),
    ("soft / cloth surface", None, 3),
    ("chair leg", None, 3),
    ("wall", None, 3),
]

rover = get_rover()
print("sonar_stop_mm = %s" % rover.sonar_stop_mm)
rows = []
for label, start_cm, reps in CASES:
    for rep in range(1, reps + 1):
        input("\n[%s] rep %d/%d - place obstacle and rover%s. Press Enter to hop %d cm..." % (
            label, rep, reps, "" if start_cm is None else " (%d cm apart)" % start_cm, HOP_CM))
        result = rover.hop(HOP_CM)
        time.sleep(0.4)
        sonar_after = rover.telemetry_snapshot().get("sonar_mm")
        print("  hop ->", result, "| sonar now:", sonar_after, "mm")
        text = input("  measured gap now (cm; blank = skip this rep): ").strip()
        if not text:
            continue
        gap = float(text)
        ok = gap >= MIN_GAP_CM or (start_cm is not None and gap >= start_cm - 1)
        miss = result.get("status") == "ok" and gap < MIN_GAP_CM and (start_cm is None or gap < start_cm - 1)
        rows.append({"case": label, "rep": rep, "status": result.get("status"),
                     "hop_sonar_mm": result.get("sonar_mm"), "sonar_after_mm": sonar_after,
                     "moved_s": result.get("moved_s"), "gap_cm": gap, "pass": ok, "sonar_miss": miss})
        print("  ->", "PASS" if ok else "FAIL", "(SONAR MISS)" if miss else "")
rover.close()

out = BRAIN / "logs" / "f10_sonar_bench.csv"
out.parent.mkdir(exist_ok=True)
if rows:
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
print("\n=== summary (pass = gap >= %d cm, or did not close in from an inside start)" % MIN_GAP_CM)
for label, _, _ in CASES:
    sub = [r for r in rows if r["case"] == label]
    if sub:
        print("  %-30s %d/%d pass, %d sonar miss(es), min gap %.0f cm" % (
            label, sum(r["pass"] for r in sub), len(sub), sum(r["sonar_miss"] for r in sub),
            min(r["gap_cm"] for r in sub)))
print("wrote", out)
print("If any box/wall rep stopped under 25 cm: raise ROVER_PI_SONAR_STOP_MM (e.g. 330) and repeat those cases.")
