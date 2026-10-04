# Dashboard/brain/tools/f3_score_locate.py
"""F3 offline kill test, scaled to a 2m/1m/0.5m room (9 frames, not 12 — the
spec's 3m rung is dropped since the test room isn't long enough).
Progress/spec-free-roam-approach.md §4, row F3.

9 reference photos: 3 distances (2m/1m/0.5m) x 3 positions (left/centre/right).
Thresholds are the spec's 10/12 (83.3%) and 8/12 (66.7%) proportions, scaled:
    PASS_AT = 8/9, KILL_BELOW = 6/9.

Switch PLANNER_PROVIDER/VLM_PROVIDER to ollama in .env before running this
for the number that actually counts as the F3 result — a cloud VLM passing
this proves nothing about RQ1.

Name files so the ground truth is obvious, e.g. "2m_left.jpg", "0.5m_right.jpg".

    venv/bin/python tools/f3_score_locate.py path/to/frames/ "the red ball"
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import frames  # noqa: E402
from mission import _derive_loc  # noqa: E402
from vlm import locate_target  # noqa: E402

TARGET = sys.argv[2] if len(sys.argv) > 2 else "the target"

# Scaled from the spec's 12-frame 10/≥pass, <8 kill (83.3% / 66.7%) to 9 frames.
PASS_AT = 8
KILL_BELOW = 6


def ground_truth_side(name: str) -> str:
    name = name.lower()
    for side in ("left", "centre", "center", "right"):
        if side in name:
            return "centre" if side == "center" else side
    raise ValueError(f"no left/centre/right in filename: {name}")


def classify(frac):
    """frac = x_center as a 0-1 fraction of frame width (from _derive_loc)."""
    if frac < 1 / 3:
        return "left"
    if frac > 2 / 3:
        return "right"
    return "centre"


def main():
    folder = Path(sys.argv[1])
    files = sorted(folder.glob("*.jpg")) + sorted(folder.glob("*.png"))
    correct = 0
    for path in files:
        truth = ground_truth_side(path.name)
        raw = path.read_bytes()
        stats = frames.analyse(raw)
        result = locate_target(raw, TARGET)

        # Same conversion the mission uses: handles each model's box format
        # (pixels vs 0-1000 normalised, x,y vs y,x order) via vlm.bbox_format().
        loc = _derive_loc(result, stats.width, stats.height) if stats else {"visible": False}

        if not loc.get("visible"):
            verdict = "FAIL (not visible / invalid box)"
        else:
            guess = classify(loc["x_center"])
            ok = guess == truth
            correct += 1 if ok else 0
            verdict = f"{'OK' if ok else 'WRONG'} (guessed {guess})"

        print(f"{path.name:20s} truth={truth:7s} {verdict}  raw={json.dumps(result)[:150]}")

    total = len(files)
    print(f"\nScore: {correct}/{total}")
    if total != 9:
        print(f"(expected 9 frames — 3 distances x 3 positions — found {total})")
    if correct >= PASS_AT:
        print(f"PASS (>={PASS_AT}/{total}) — build on.")
    elif correct < KILL_BELOW:
        print(f"KILL (<{KILL_BELOW}/{total}) — stop free-roam, spec §6.")
    else:
        print("Borderline — spec gives no clean verdict here, use judgement.")


if __name__ == "__main__":
    main()
