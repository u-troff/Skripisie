"""F9 - how accurate is the VLM's distance estimate? (spec-supervisor-feedback-2026-10-05.md C2/C3)

Interactive, same style as f1_calibrate.py. Run from Dashboard/brain/ with the venv interpreter:

    # C3 live: box on tape marks at 30/50/75/100/150 cm, 3 reps each (rover + rosbridge up);
    # frames are saved as logs/f9/<timestamp>/d030cm_1.jpg ... (folder + names are automatic)
    venv/bin/python tools/f9_distance_calib.py --models qwen2.5vl:3b,gemma4:e4b --target "orange box"

    # capture only (no VLM), e.g. to build the C2 photo set; scoring can be rerun on it later
    venv/bin/python tools/f9_distance_calib.py --capture-only --distances 30,50,75,100,150 --reps 2

    # C2 go/no-go without the rover: score a saved session ("latest" = newest folder in logs/f9)
    venv/bin/python tools/f9_distance_calib.py --from-dir latest --models qwen2.5vl:3b
    venv/bin/python tools/f9_distance_calib.py --from-dir "../../VLM test/dist" --models qwen2.5vl:3b

Photos of your own only need `<N>cm` somewhere in the file name. One frame per rep is shared by
every model, so the models are compared on identical pictures. Honours VLM_DIST_MODE
(numeric | bucket). Results go to <frames folder>/results_<time>.csv.
"""
import argparse
import csv
import os
import re
import statistics
import sys
import time
from pathlib import Path

BRAIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BRAIN))

import config  # noqa: E402
import providers  # noqa: E402
import vlm  # noqa: E402

DISTANCES = (30, 50, 75, 100, 150)
REPS = 3
FRAMES = BRAIN / "logs" / "f9"
MAX_REL_ERR = 0.30  # decision rule: median relative error at or below this ...


def frames_from_dir(folder):
    out = []
    for path in sorted(Path(folder).glob("*.jp*g")):
        match = re.search(r"(\d+)\s*cm", path.name, re.I)
        if match:
            out.append((int(match.group(1)), path.name, path.read_bytes()))
    return out


def latest_session():
    sessions = sorted(p for p in FRAMES.glob("*") if p.is_dir()) if FRAMES.exists() else []
    if not sessions:
        sys.exit("no saved sessions in %s - capture one first" % FRAMES)
    return sessions[-1]


def frames_from_rover(distances, reps):
    from rover import get_rover
    rover = get_rover()
    if hasattr(rover, "set_gimbal"):  # the same view the approach loop uses
        rover.set_gimbal(pan=0, tilt=config.get_int("APPROACH_TILT_OFFSET", 0))
    folder = FRAMES / time.strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    print("saving frames to %s" % folder)
    out = []
    try:
        for distance in distances:
            for rep in range(1, reps + 1):
                input("\nBox on the %d cm mark, centred (rep %d/%d). Press Enter to grab..." % (distance, rep, reps))
                frame = rover.get_frame() or rover.get_frame()
                if frame is None:
                    print("  no frame - skipped")
                    continue
                name = "d%03dcm_%d.jpg" % (distance, rep)
                (folder / name).write_bytes(frame)
                out.append((distance, name, frame))
    finally:
        rover.close()
    return folder, out


def ask(model, frame, target):
    os.environ["VLM_PROVIDER"], os.environ["VLM_MODEL_OLLAMA"] = "ollama", model
    providers.reset_cache()  # the provider bakes the model in at construction
    raw = vlm.locate_target(frame, target)
    return raw, (None if vlm.failed(raw) else vlm.distance_cm_from(raw))


def summarise(model, rows):
    answered = [r for r in rows if r["d_vlm_cm"] is not None]
    print("\n=== %s  (%s mode): %d/%d frames gave a usable distance" % (
        model, config.get("VLM_DIST_MODE", "numeric"), len(answered), len(rows)))
    if not answered:
        print("  no estimates -> keep HOP_MODE=fixed and report this as a finding")
        return
    rel = [abs(r["d_vlm_cm"] - r["d_true_cm"]) / r["d_true_cm"] for r in answered]
    bias = statistics.mean(r["d_vlm_cm"] - r["d_true_cm"] for r in answered)
    medians = []
    for d in sorted({r["d_true_cm"] for r in rows}):
        sub = [r for r in answered if r["d_true_cm"] == d]
        if not sub:
            print("  %4d cm: no estimates" % d)
            continue
        med = statistics.median(r["d_vlm_cm"] for r in sub)
        medians.append(med)
        worst = max(sub, key=lambda r: abs(r["d_vlm_cm"] - d))
        print("  %4d cm: median est %6.1f cm | median rel err %3.0f%% | worst %6.1f cm (%+.0f%%)" % (
            d, med, 100 * statistics.median(abs(r["d_vlm_cm"] - d) / d for r in sub),
            worst["d_vlm_cm"], 100 * (worst["d_vlm_cm"] - d) / d))
    rising = len(medians) == len({r["d_true_cm"] for r in rows}) and all(b > a for a, b in zip(medians, medians[1:]))
    median_rel = statistics.median(rel)
    print("  overall: median rel err %.0f%% | bias %+.1f cm | estimates rise with distance: %s" % (
        100 * median_rel, bias, rising))
    if median_rel <= MAX_REL_ERR and rising:
        print("  VERDICT: usable -> keep this VLM_DIST_MODE for HOP_MODE=variable")
    elif config.get("VLM_DIST_MODE", "numeric").lower() != "bucket":
        print("  VERDICT: numeric too noisy -> rerun with VLM_DIST_MODE=bucket")
    else:
        print("  VERDICT: still noise -> keep HOP_MODE=fixed, report VLM distance accuracy as a finding")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", default=config.get("VLM_MODEL_OLLAMA", "qwen2.5vl:3b"))
    parser.add_argument("--target", default="orange box")
    parser.add_argument("--from-dir", default="", help="score saved frames instead of using the rover; 'latest' = newest session")
    parser.add_argument("--capture-only", action="store_true", help="grab and save the frames, skip the VLM")
    parser.add_argument("--distances", default=",".join(map(str, DISTANCES)), help="tape marks in cm, comma separated")
    parser.add_argument("--reps", type=int, default=REPS)
    # commands copied out of chat can carry an invisible zero-width space on the last
    # argument (-> 'invalid model name' / int('40' + U+200B)); drop them
    args = parser.parse_args([re.sub('[\u200b-\u200d\u2060\ufeff]', '', a) for a in sys.argv[1:]])

    if args.from_dir:
        folder = latest_session() if args.from_dir == "latest" else Path(args.from_dir)
        print("scoring frames in %s" % folder)
        frames = frames_from_dir(folder)
    else:
        distances = [int(d) for d in args.distances.split(",") if d.strip()]
        folder, frames = frames_from_rover(distances, args.reps)
    if not frames:
        sys.exit("no frames")
    if args.capture_only:
        print("\nsaved %d frames to %s" % (len(frames), folder))
        return
    out = folder / ("results_%s.csv" % time.strftime("%H%M%S"))
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["model", "mode", "frame", "d_true_cm", "visible",
                                                    "d_vlm_cm", "raw_distance"])
        writer.writeheader()
        for model in [m.strip() for m in args.models.split(",") if m.strip()]:
            rows = []
            for d_true, name, frame in frames:
                raw, d_vlm = ask(model, frame, args.target)
                raw = raw if isinstance(raw, dict) else {}
                row = {"model": model, "mode": config.get("VLM_DIST_MODE", "numeric"), "frame": name,
                       "d_true_cm": d_true, "visible": raw.get("visible"), "d_vlm_cm": d_vlm,
                       "raw_distance": raw.get("distance_m", raw.get("distance_bucket"))}
                rows.append(row)
                writer.writerow(row)
                print("  %-22s true %4d cm -> %s" % (name, d_true, d_vlm))
            summarise(model, rows)
    print("\nwrote %s" % out)


if __name__ == "__main__":
    main()
