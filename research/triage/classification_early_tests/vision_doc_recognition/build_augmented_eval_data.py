#!/usr/bin/env python3
"""
Merge MiniCPM-V-extracted printed text (from extract_text.py's output) into
a copy of ../doc_type/eval_data.json, adding a new "vision_extracted_text"
field to each few_shot/test_case record.

This produces eval_data_augmented.json, consumed by run_eval_augmented.py to
test whether OCR'd printed-text fragments add signal on top of the existing
human-written metadata (archive/label/description) -- not a replacement for
it. Run this only after extract_text.py has completed (or partially
completed -- records with no extraction available get vision_extracted_text
set to null and are still usable, they just won't have that evidence).
"""

import json
from pathlib import Path

HERE = Path(__file__).parent


def main():
    doc_type_data = json.loads((HERE.parent / "doc_type" / "eval_data.json").read_text(encoding="utf-8"))
    extracted_path = HERE / "extracted_text.json"
    if not extracted_path.exists():
        raise SystemExit(f"{extracted_path} not found -- run extract_text.py first.")
    extracted = json.loads(extracted_path.read_text(encoding="utf-8"))

    # extracted_text.json is keyed by str(id); few_shot records in
    # doc_type/eval_data.json have no id field (only label), so join few_shot
    # by label and test_cases by id, same approach build_eval_data.py used.
    extracted_by_label = {v["label"]: v for v in extracted.values()}

    def augment(rec, by_label=False):
        if by_label:
            match = extracted_by_label.get(rec["label"])
        else:
            match = extracted.get(str(rec["id"]))
        rec = dict(rec)
        rec["vision_extracted_text"] = match["extracted_text"] if match else None
        return rec

    few_shot = [augment(ex, by_label=True) for ex in doc_type_data["few_shot"]]
    test_cases = [augment(tc, by_label=False) for tc in doc_type_data["test_cases"]]

    missing_fs = sum(1 for ex in few_shot if ex["vision_extracted_text"] is None)
    missing_tc = sum(1 for tc in test_cases if tc["vision_extracted_text"] is None)

    out = {
        "purpose": "doc_type/eval_data.json's few_shot/test_cases augmented with "
                   "MiniCPM-V-extracted printed text per document (see "
                   "extract_text.py). Tests whether OCR'd printed-form text adds "
                   "signal on top of the existing human-written metadata.",
        "label_definitions": doc_type_data["label_definitions"],
        "few_shot": few_shot,
        "test_cases": test_cases,
    }

    out_path = HERE / "eval_data_augmented.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"few_shot: {len(few_shot)} ({missing_fs} missing extraction)")
    print(f"test_cases: {len(test_cases)} ({missing_tc} missing extraction)")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
