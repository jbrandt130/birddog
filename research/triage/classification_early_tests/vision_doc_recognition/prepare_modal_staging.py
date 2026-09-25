#!/usr/bin/env python3
"""
Build a lightweight staging directory containing only what the Modal
text-extraction job needs: the eval scripts, eval_data.json, any partial
extracted_text.json (so the Modal run can --resume from where the MacBook
run left off), and the specific page JPGs referenced by eval_data.json's
few_shot + test_cases image_paths.

Why this exists: vision_doc_recognition/ is ~112GB (95GB of source PDFs +
17GB of every rendered page), but extract_text.py only ever touches the
699 specific page images sampled into eval_data.json -- about 4GB. This
script stages just that subset so the Modal upload doesn't try to push the
whole folder.

Uses hardlinks (falls back to copy if that fails, e.g. across filesystems)
so staging is fast and doesn't double your disk usage.

Run this locally before:  modal run modal_extract_text.py
Re-run it any time eval_data.json or extracted_text.json changes.
"""
import json
import shutil
from pathlib import Path

HERE = Path(__file__).parent
# Rendered page JPGs live in ../data/pages/ (moved out of this folder to keep
# the code tree free of ~112GB of binary data). Stored image_paths stay
# relative ("pages/<id>/<n>.jpg"), so they're resolved against DATA_ROOT here
# and staged under STAGING/pages/ -- which is the layout the Modal container
# expects, so nothing on the remote side changes.
DATA_ROOT = HERE.parent / "data"
STAGING = HERE / "modal_staging"

FILES_TO_COPY = ["extract_text.py", "run_eval.py", "eval_data.json"]


def main():
    STAGING.mkdir(exist_ok=True)

    for name in FILES_TO_COPY:
        shutil.copy2(HERE / name, STAGING / name)

    existing = HERE / "extracted_text.json"
    if existing.exists():
        shutil.copy2(existing, STAGING / "extracted_text.json")
        print(f"Included existing extracted_text.json "
              f"({existing.stat().st_size} bytes) for --resume")
    else:
        print("No existing extracted_text.json found -- starting from scratch")

    data = json.loads((HERE / "eval_data.json").read_text(encoding="utf-8"))
    image_paths = set()
    for rec in data["few_shot"] + data["test_cases"]:
        image_paths.update(rec["image_paths"])

    n_linked = 0
    total_bytes = 0
    for rel in sorted(image_paths):
        src = DATA_ROOT / rel
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
    print("Next: modal run modal_extract_text.py")


if __name__ == "__main__":
    main()
