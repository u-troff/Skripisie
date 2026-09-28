#!/usr/bin/env python3
"""Print the exact generate_plan prompt for a fixed command and scene, without
calling a model.

    venv/Scripts/python.exe tools/snapshot_prompt.py

Used to prove the Pi planner prompt is byte-identical before and after the
planner-profile split (Progress/spec-planner-profiles-and-virtual-sweep.md
§2, §C). Stdlib only, no network, no provider call.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from planner import build_plan_prompt  # noqa: E402

FIXED_COMMAND = "go to the kitchen table and tell me what you see"
FIXED_SCENE = (
    "View 1 (living room): kitchen table; window; floor obstacles: the couch, the rug"
)


def main() -> None:
    print(build_plan_prompt(FIXED_COMMAND, FIXED_SCENE))


if __name__ == "__main__":
    main()
