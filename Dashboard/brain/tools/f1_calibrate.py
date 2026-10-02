"""F1 calibration — no models, just the chassis. Interactive: measure by hand
between each rep. See Progress/spec-free-roam-approach.md §4, row F1.

Run from Dashboard/brain/, with the venv interpreter, with the TurboPi
container already brought up (rosbridge reachable):

    venv/bin/python tools/f1_calibrate.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rover import get_rover  # noqa: E402

rover = get_rover()

print("=== F1a: hop(100) x5 ===")
seconds_used = 100 / rover.free_speed_cmps if rover.free_speed_cmps else 0
print(f"Each rep commands a {seconds_used:.2f}s forward pulse "
      f"(100cm / current FREE_SPEED_CMPS={rover.free_speed_cmps}).")
for i in range(5):
    input(f"\nRep {i+1}/5 — rover at the start mark? Press Enter to hop...")
    print("  result:", rover.hop(100))
    input("  Measure the distance travelled (cm), reset the rover to the "
          "start mark, then press Enter...")

print("\n=== F1b: pivot(+1, 1.0s) x5 ===")
print("direction=+1 is supposed to mean RIGHT (clockwise) — watch for this.")
for i in range(5):
    input(f"\nRep {i+1}/5 — rover at the start heading? Press Enter to pivot...")
    print("  result:", rover.pivot(1, 1.0))
    input("  Measure the angle turned (deg) and note which way it actually "
          "spun, reset heading, then press Enter...")

rover.close()
print(f"\nFREE_SPEED_CMPS = mean(measured_cm) / {seconds_used:.2f}")
print("FREE_TURN_DEG_PER_S = mean(measured_degrees) / 1.0")
print("If it span LEFT instead of right for direction=+1, flip the sign in "
      "rover_pi.py's pivot(): angular_z=-direction*self.free_turn_z -> "
      "angular_z=direction*self.free_turn_z")
