"""F8a: measure APPROACH_TILT_OFFSET and a sharpness floor — no models.
See Progress/i-think-we-have-cached-moth.md §E. Run from Dashboard/brain/:

    venv/bin/python tools/f8a_tilt_offset.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import frames  # noqa: E402
from rover import get_rover  # noqa: E402

SWEEPS_DIR = Path(__file__).parent / "sweeps"
SWEEPS_DIR.mkdir(exist_ok=True)

rover = get_rover()
offset = 0
sharp_samples = []

print("=== F8a: tilt offset ===")
print("Put a chair ~1.5m ahead, roughly seat-height in frame.\n")

while True:
    result = rover.set_gimbal(pan=0, tilt=offset)
    frame = rover.get_frame()
    if frame is None:
        print("  no frame from the camera")
    else:
        path = SWEEPS_DIR / f"tilt_{offset}.jpg"
        path.write_bytes(frame)
        stats = frames.analyse(frame)
        sharpness = stats.sharpness if stats else None
        if sharpness is not None:
            sharp_samples.append(sharpness)
        print(f"  tilt_offset={result['tilt_offset']} (requested {offset}) "
              f"-> {path.name}, sharpness={sharpness}")

    cmd = input("  '+'/'-' to step 50, a number to set offset directly, "
                "'ok' when framed well: ").strip().lower()
    if cmd == "ok":
        break
    elif cmd == "+":
        offset += 50
    elif cmd == "-":
        offset -= 50
    else:
        try:
            offset = int(cmd)
        except ValueError:
            print("  not understood, try again")

print(f"\nChosen APPROACH_TILT_OFFSET = {offset}")
print("Checking pan extremes at this tilt — confirm no rover body in frame, "
      "ceiling doesn't dominate:")
for pan in (0, -400, 400):
    rover.set_gimbal(pan=pan, tilt=offset)
    frame = rover.get_frame()
    if frame is not None:
        path = SWEEPS_DIR / f"pan_{pan}_at_tilt_{offset}.jpg"
        path.write_bytes(frame)
        print(f"  pan={pan} -> {path.name}")

rover.set_gimbal(pan=0, tilt=0)
rover.close()

if sharp_samples:
    median = sorted(sharp_samples)[len(sharp_samples) // 2]
    print(f"\nSuggested APPROACH_MIN_SHARPNESS = {round(0.6 * median, 1)} "
          f"(0.6 x median settled sharpness, measured on this camera)")

print("\nPaste into .env:")
print(f"APPROACH_TILT_OFFSET={offset}")
