# Dashboard/brain/tools/f4_sonar_stop.py
"""F4 — sonar stop during a hop. See spec §4, row F4. No models.

    venv/bin/python tools/f4_sonar_stop.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rover import get_rover  # noqa: E402

rover = get_rover()
input("Clear path ahead, rover at the start mark. Press Enter to hop 300cm — "
      "drop a box into its path partway through the run...")
print("result:", rover.hop(100))
rover.close()
