# Dashboard/brain/tools/grab_frame.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rover import get_rover  # noqa: E402

rover = get_rover()
frame = rover.get_frame()
if frame is None:
    print("no frame — check the camera/web_video_server")
else:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "frame.jpg")
    out.write_bytes(frame)
    print(f"saved {len(frame)} bytes -> {out}")
rover.close()
