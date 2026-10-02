"""F3 step 3b: run mission._derive_loc + mission._plausible offline over a
frame corpus, and report the max fill of a true positive at 0.5m (for tuning
MAX_FILL). Run AFTER f3_score_locate.py confirms the location classification
still holds at the new tilt.

    venv/bin/python tools/f3_plausible_check.py path/to/frames/ "the chair"
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import frames  # noqa: E402
from vlm import locate_target  # noqa: E402
from mission import _derive_loc, _plausible  # noqa: E402

TARGET = sys.argv[2] if len(sys.argv) > 2 else "the target"


def ground_truth_distance(name: str) -> float:
    name = name.lower()
    for token, meters in (("2m", 2.0), ("1m", 1.0), ("0.5m", 0.5)):
        if name.startswith(token):
            return meters
    raise ValueError(f"no distance prefix (2m/1m/0.5m) in filename: {name}")


def main():
    folder = Path(sys.argv[1])
    files = sorted(folder.glob("*.jpg")) + sorted(folder.glob("*.png"))
    rejected_true_positives = []
    max_fill_at_half_m = 0.0

    for path in files:
        distance = ground_truth_distance(path.name)
        raw = path.read_bytes()
        stats = frames.analyse(raw)
        result = locate_target(raw, TARGET)
        loc = _derive_loc(result, stats.width if stats else None,
                          stats.height if stats else None)
        reject = _plausible(loc, prev_fill=None, hops_done=1)

        print(f"{path.name:20s} visible={loc.get('visible')} "
              f"fill={loc.get('fill')} reject={reject} "
              f"raw={json.dumps(result)[:120]}")

        # Every one of these 9 frames IS a true positive by construction (the
        # target really is in frame) — a non-None reject here is exactly the
        # false-reject case step 3 needs to be zero.
        if loc.get("visible") and reject is not None:
            rejected_true_positives.append(path.name)
        if distance == 0.5 and loc.get("visible") and loc.get("fill"):
            max_fill_at_half_m = max(max_fill_at_half_m, loc["fill"])

    print(f"\nTrue positives rejected by _plausible(): {len(rejected_true_positives)}")
    if rejected_true_positives:
        print("  " + ", ".join(rejected_true_positives))
        print("MAX_FILL (or another _plausible threshold) is too tight — widen it.")
    else:
        print("None rejected — _plausible() is not cutting off real detections.")

    if max_fill_at_half_m:
        suggested = max(0.60, round((max_fill_at_half_m + 1.0) / 2, 2))
        print(f"\nMax fill at 0.5m: {max_fill_at_half_m}")
        print(f"Suggested MAX_FILL = {suggested} (midway to 1.0, floored at 0.60)")


if __name__ == "__main__":
    main()
