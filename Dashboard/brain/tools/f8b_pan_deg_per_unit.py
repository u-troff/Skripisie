"""F8b: measure GIMBAL_DEG_PER_UNIT and its sign — no models. Run AFTER F8a,
at the chosen tilt, so the constant absorbs any cos(tilt) factor.
See Progress/i-think-we-have-cached-moth.md §E. Run from Dashboard/brain/:

    venv/bin/python tools/f8b_pan_deg_per_unit.py
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rover import get_rover  # noqa: E402

SWEEPS_DIR = Path(__file__).parent / "sweeps"
SWEEPS_DIR.mkdir(exist_ok=True)

rover = get_rover()
tilt = int(input("APPROACH_TILT_OFFSET to hold during this test: ").strip() or "0")
hfov_deg = float(input("CAMERA_HFOV_DEG (from .env, default 60): ").strip() or "60")

print("\n=== Method 1: vision ===")
print("Place a distinctive object in frame, centred, then step pan.\n")
measurements = []
for pan in (0, -200, 200, -400, 400):
    rover.set_gimbal(pan=pan, tilt=tilt)
    frame = rover.get_frame()
    if frame is None:
        print(f"  pan={pan}: no frame")
        continue
    path = SWEEPS_DIR / f"pandeg_{pan}.jpg"
    path.write_bytes(frame)
    x_frac = input(f"  pan={pan} -> {path.name}. Object's x-fraction "
                   f"(0=left edge, 1=right edge): ").strip()
    try:
        measurements.append((pan, float(x_frac)))
    except ValueError:
        print("  skipped (not a number)")

rover.set_gimbal(pan=0, tilt=tilt)

x0 = next((x for p, x in measurements if p == 0), None)
vision_estimates = []
if x0 is not None:
    for pan, x in measurements:
        if pan == 0:
            continue
        deg_per_unit = (x0 - x) * hfov_deg / pan
        vision_estimates.append(deg_per_unit)
        print(f"  pan={pan}: deg_per_unit = {deg_per_unit:.5f}")

print("\n=== Method 2: tape measure ===")
print("Mark where frame-centre lands on a wall at distance D (measure D once).\n")
D = float(input("  Distance to the wall, D (cm): ").strip())

input(f"  Gimbal is at pan=0. Mark the wall point at frame-centre now, "
      f"then press Enter...")

tape_estimates = []
for p in (-400, -200, 200, 400):
    rover.set_gimbal(pan=p, tilt=tilt)
    raw = input(f"  pan={p}: gimbal moved. Mark the new frame-centre point, "
                f"measure its distance from the pan=0 mark (cm), enter L "
                f"(blank to skip): ").strip()
    if not raw:
        continue
    try:
        l = float(raw)
        deg = math.degrees(math.atan(l / D))
        deg_per_unit = deg / abs(p)
        tape_estimates.append(deg_per_unit)
        print(f"    -> deg_per_unit = {deg_per_unit:.5f}")
    except ValueError:
        print("    not understood, skipped")

rover.set_gimbal(pan=0, tilt=tilt)


rover.close()

all_estimates = vision_estimates + tape_estimates
if not all_estimates:
    print("\nNo measurements taken — nothing to report.")
    sys.exit(1)

mean_est = sum(all_estimates) / len(all_estimates)
spread = (max(all_estimates) - min(all_estimates)) / abs(mean_est) if mean_est else float("inf")

print(f"\nVision estimates: {vision_estimates}")
print(f"Tape estimates:   {tape_estimates}")
print(f"Mean: {mean_est:.5f}, spread: {spread * 100:.1f}%, "
      f"sign: {'positive' if mean_est > 0 else 'negative'}")

if mean_est < 0:
    print("\n*** STOP ***")
    print("Sign is NEGATIVE. Do not patch the approach loop.")
    print("Fix: flip ROVER_PI_PAN_INVERT in .env, set LOOK_LEFT_PAN=1900, "
          "re-run this tool, then re-run T5 (line mission's look-left).")
else:
    print("\nPaste into .env:")
    print(f"GIMBAL_DEG_PER_UNIT={abs(mean_est):.4f}")
