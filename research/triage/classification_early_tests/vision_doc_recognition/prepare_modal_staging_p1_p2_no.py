#!/usr/bin/env python3
"""
Staging script for the p1_p2_no OCR-extraction extension, parallel to
prepare_modal_staging.py but scoped to eval_data_p1_p2_no_extra.json's 193
new documents instead of eval_data.json's 233.

Stages: extract_text.py, run_eval.py, eval_data_p1_p2_no_extra.json, the
current extracted_text.json (so the Modal run --resumes from all 233
existing entries and only does the 193 new ones), and just the ~579 page
JPGs eval_data_p1_p2_no_extra.json references (not the full pages/ tree).

Run this locally before:  modal run modal_extract_text_p1_p2_no.py
Re-run any time eval_data_p1_p2_no_extra.json or extracted_text.json changes
(e.g. re-run build_p1_p2_no_extraction_data.py first if extracted_text.json
has since grown).
"""
import json
import shutil
from pathlib import Path

HERE = Path(__file__).parent
# Rendered page JPGs live in ../data/pages/ -- see prepare_modal_staging.py.
DATA_ROOT = HERE.parent / "data"
STAGING = HERE / "modal_staging_p1_p2_no"
DATA_FILE = "eval_data_p1_p2_no_extra.json"

FILES_TO_COPY = ["extract_text.py", "run_eval.py", DATA_FILE]


def main():
    STAGING.mkdir(exist_ok=True)

    for name in FILES_TO_COPY:
        shutil.copy2(HERE / name, STAGING / name)

    existing = HERE / "extracted_text.json"
    if existing.exists():
        shutil.copy2(existing, STAGING / "extracted_text.json")
        print(f"Included existing extracted_text.json "
              f"({existing.stat().st_size} bytes, for --resume)")
    else:
        print("No existing extracted_text.json found -- starting from scratch")

    data = json.loads((HERE / DATA_FILE).read_text(encoding="utf-8"))
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
    print("Next: modal run --detach modal_extract_text_p1_p2_no.py")


if __name__ == "__main__":
    main()
