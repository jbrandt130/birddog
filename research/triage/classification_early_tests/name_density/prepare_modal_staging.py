#!/usr/bin/env python3
"""
Stage only what the Modal density job needs: the scripts, eval_data.json,
any partial density_*.json (so the Modal run can --resume), and the
specific page JPGs eval_data.json references.

Why: ../data/ is ~112GB (source PDFs plus every rendered page), while this
job touches only the k=5 middle pages per document -- roughly 1400 images.
Staging just that subset keeps the Modal upload small.

IMPORTANT -- the shared page tree: ../data/pages/<id>/ holds BOTH the
first-10 front-matter renders (used by doc_type/OCR) and the k=5 middle
renders (used here). This script stages only the paths eval_data.json
actually lists, which build_eval_data.py derived from the middle-half
sampler rather than by globbing the directory. So the front pages are
never uploaded and can't contaminate the density estimate.

Uses hardlinks (falling back to copy across filesystems) so staging is fast
and doesn't double disk usage.

Run locally before:  modal run --detach modal_run_density.py
Re-run whenever eval_data.json or a density_*.json changes.
"""
import argparse
import json
import shutil
from pathlib import Path

HERE = Path(__file__).parent
DATA_ROOT = HERE.parent / "data"
STAGING = HERE / "modal_staging"

FILES_TO_COPY = ["extract_density.py", "analyze_density.py",
                 "analyze_priority_density.py"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="eval_data_v3.json",
                     help="which sample to stage: eval_data_v3.json (k=20 "
                          "systematic, the current test) or eval_data.json "
                          "(the k=5 middle-half pilot)")
    args = ap.parse_args()

    STAGING.mkdir(exist_ok=True)

    for name in FILES_TO_COPY:
        src = HERE / name
        if src.exists():
            shutil.copy2(src, STAGING / name)
    # The container always reads "eval_data.json"; stage the chosen sample
    # under that name so extract_density.py needs no --data plumbing.
    shutil.copy2(HERE / args.data, STAGING / "eval_data.json")
    print(f"Staging sample: {args.data}")

    for partial in HERE.glob("density_*.json"):
        shutil.copy2(partial, STAGING / partial.name)
        print(f"Included existing {partial.name} "
              f"({partial.stat().st_size} bytes) for --resume")

    data = json.loads((HERE / args.data).read_text(encoding="utf-8"))
    image_paths = set()
    for section in ("train", "validation"):
        for rec in data[section]:
            image_paths.update(rec["image_paths"])

    n_linked = 0
    total_bytes = 0
    missing = []
    for rel in sorted(image_paths):
        src = DATA_ROOT / rel
        if not src.exists():
            missing.append(rel)
            continue
        dst = STAGING / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        total_bytes += src.stat().st_size
        if dst.exists():
            continue
        try:
            dst.hardlink_to(src)
        except OSError:
            shutil.copy2(src, dst)
        n_linked += 1

    print(f"Staged {n_linked} new page images "
          f"({total_bytes / 1e9:.2f} GB total referenced) into {STAGING}")
    if missing:
        print(f"WARNING: {len(missing)} referenced image(s) not found on disk "
              f"-- is the middle-page render still running? "
              f"Re-run build_eval_data.py then this script once it finishes.")
        print(f"  e.g. {missing[:3]}")
    print("Next: modal run --detach modal_run_density.py")


if __name__ == "__main__":
    main()
