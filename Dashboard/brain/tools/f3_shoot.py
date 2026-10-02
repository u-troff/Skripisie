"""F3 step 3: shoot the 9-frame corpus at the new gimbal tilt (amendment
2026-10-02, rung F8). Prompts through each of 3 distances x 3 positions,
naming files the way f3_score_locate.py expects ("<dist>_<side>.jpg").

    venv/bin/python tools/f3_shoot.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config  # noqa: E402
from rover import get_rover  # noqa: E402

OUT_DIR = Path(__file__).parent / "f3_frames_v2"
OUT_DIR.mkdir(exist_ok=True)

tilt = config.get_int("APPROACH_TILT_OFFSET", 0)
rover = get_rover()
rover.set_gimbal(pan=0, tilt=tilt)

print(f"Gimbal set to pan=0, tilt={tilt}. Shooting 9 frames: "
      f"3 distances (2m/1m/0.5m) x 3 positions (left/centre/right).\n")

for dist in ("2m", "1m", "0.5m"):
    for side in ("left", "centre", "right"):
        input(f"Place the target at {dist}, {side} of frame. Press Enter to grab...")
        frame = rover.get_frame()
        if frame is None:
            print("  no frame — check camera/web_video_server, try again")
            continue
        path = OUT_DIR / f"{dist}_{side}.jpg"
        path.write_bytes(frame)
        print(f"  saved {path}")

rover.close()
print(f"\nDone. Frames in {OUT_DIR}")
print(f'Run: venv/bin/python tools/f3_score_locate.py {OUT_DIR} "the <target>"')
print(f'Then: venv/bin/python tools/f3_plausible_check.py {OUT_DIR} "the <target>"')
