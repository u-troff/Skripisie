#!/usr/bin/env python3
"""Summarise one or more virtual_sweep CSVs for RQ2.

    venv\\Scripts\\python.exe tools\\sweep_summary.py tools\\sweeps\\sweep_X.csv [more.csv ...]

Groups rows by (condition, depth, kind) and prints a markdown table:
n, visits_match rate, median first_divergence (over the failing rows only),
hallucinations, unresolved_targets, out_of_vocab and mean planner latency.
Also writes tools/sweeps/depth_vs_success.png (success rate vs depth, one line
per condition) if matplotlib is importable; otherwise it says so and skips.

"match" is the visits_match column, so rows with no ground truth (plain-string
commands, or literal chains whose expected_visits is []) still score: a literal
chain that triggers an approach is a mismatch. Rows that errored count in n and
count as failures.
"""

import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SWEEPS_DIR = Path(__file__).parent / "sweeps"


def _num(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _count(value: Optional[str]) -> int:
    """hallucinations is a number; unresolved_targets/out_of_vocab_actions are
    '; '-joined lists, counted by item."""
    if value in (None, ""):
        return 0
    number = _num(value)
    if number is not None:
        return int(number)
    return len([p for p in value.split(";") if p.strip()])


def _load(paths: List[str]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for path in paths:
        with open(path, newline="", encoding="utf-8") as handle:
            rows.extend(csv.DictReader(handle))
    return rows


def _fmt(value: Optional[float], digits: int = 1) -> str:
    return "-" if value is None else ("%.*f" % (digits, value))


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    rows = _load(sys.argv[1:])

    multi_room = len({r.get("room") for r in rows if r.get("room")}) > 1
    groups: Dict[Tuple[str, str, str], List[Dict[str, str]]] = defaultdict(list)
    for row in rows:
        depth = row.get("depth") or "?"
        cond = row.get("condition") or "?"
        if multi_room:
            cond = "%s/%s" % (row.get("room") or "?", cond)
        groups[(cond, depth, row.get("kind") or "?")].append(row)

    def sort_key(key: Tuple[str, str, str]):
        condition, depth, kind = key
        return (condition, int(depth) if depth.isdigit() else 10 ** 6, kind)

    print("| condition | depth | kind | n | match rate | median first_div | halluc. | unresolved | out_of_vocab | errors | mean planner s |")
    print("|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    curve: Dict[str, Dict[int, List[float]]] = defaultdict(lambda: defaultdict(list))
    for key in sorted(groups, key=sort_key):
        condition, depth, kind = key
        rs = groups[key]
        scored = [r for r in rs if r.get("visits_match") not in (None, "")]
        wins = [1.0 if r["visits_match"] == "True" else 0.0 for r in scored]
        rate = sum(wins) / len(wins) if wins else None
        divs = [_num(r.get("first_divergence")) for r in rs]
        divs = [d for d in divs if d is not None]
        lat = [_num(r.get("planner_latency_s")) for r in rs]
        lat = [x for x in lat if x is not None]
        print("| %s | %s | %s | %d | %s | %s | %d | %d | %d | %d | %s |" % (
            condition, depth, kind, len(rs),
            "-" if rate is None else "%.0f%% (%d/%d)" % (rate * 100, sum(wins), len(wins)),
            _fmt(statistics.median(divs)) if divs else "-",
            sum(_count(r.get("hallucinations")) for r in rs),
            sum(_count(r.get("unresolved_targets")) for r in rs),
            sum(_count(r.get("out_of_vocab_actions")) for r in rs),
            sum(1 for r in rs if r.get("error")),
            _fmt(statistics.mean(lat), 2) if lat else "-"))
        if rate is not None and depth.isdigit() and kind != "probe":
            curve[condition][int(depth)].extend(wins)

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("\nmatplotlib is not installed in this venv — skipped depth_vs_success.png "
              "(tell me before I pip install it).")
        return

    fig, ax = plt.subplots(figsize=(7, 4.2))
    for condition in sorted(curve):
        depths = sorted(curve[condition])
        ax.plot(depths, [100.0 * statistics.mean(curve[condition][d]) for d in depths],
                marker="o", label=condition)
    ax.set_xlabel("instructions chained (depth)")
    ax.set_ylabel("exact visit-order match (%)")
    ax.set_ylim(-3, 103)
    ax.grid(alpha=0.3)
    ax.legend()
    ax.set_title("Plan success vs chain depth (probes excluded)")
    out = SWEEPS_DIR / "depth_vs_success.png"
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    print("\nwrote %s" % out)


if __name__ == "__main__":
    main()
