#!/usr/bin/env python3
"""
Build eval_data_p1_p2_no_extra.json: a minimal extraction-only data file for
the 193 p1_p2_no documents that have rendered page images but are not yet
covered by extracted_text.json.

Background: extracted_text.json currently has 233 entries, built for the
doc_type/doc_type_with_text comparison. The p1_p2_no task (../p1_p2_no/)
draws on a different, mostly-overlapping-but-not-identical set of 284
document ids (34 few_shot + 250 test_cases). Of those:
  - 265/284 have a rendered ../data/pages/<id>/ directory
  - 19/284 have neither rendered pages nor a source PDF -- permanently
    unavailable without a fresh download (not handled here)
  - 72/284 are already in extracted_text.json (overlap with the doc_type set)
  - 193/284 have pages but no extracted text yet -- this script's target

extract_text.py only needs id/label/root_label/sampled_pages/image_paths per
record (it doesn't touch doc_type, target, page_count, etc.), so this file
is intentionally minimal rather than a full eval_data.json clone. Page
sampling reuses build_eval_data.py's exact sample_pages()/image_paths()
convention (k=3, random.Random(doc_id)-seeded) so it stays consistent with
the existing 233 entries and is independently reproducible.

Run this locally, then:
    python3 prepare_modal_staging_p1_p2_no.py
    modal run --detach modal_extract_text_p1_p2_no.py
"""
import json
from pathlib import Path

from build_eval_data import sample_pages, image_paths, available_pages

HERE = Path(__file__).parent
OUT_NAME = "eval_data_p1_p2_no_extra.json"


def main():
    p1p2no = json.loads((HERE.parent / "p1_p2_no" / "eval_data.json").read_text(encoding="utf-8"))
    by_id = {}
    for section in ("few_shot", "test_cases"):
        for rec in p1p2no[section]:
            by_id[rec["id"]] = rec

    existing = {}
    et_path = HERE / "extracted_text.json"
    if et_path.exists():
        existing = json.loads(et_path.read_text(encoding="utf-8"))
    existing_ids = {int(k) for k in existing.keys()}

    has_pages = {doc_id for doc_id in by_id if available_pages(doc_id)}
    new_ids = sorted((set(by_id.keys()) & has_pages) - existing_ids)

    no_pages = sorted(set(by_id.keys()) - has_pages)

    test_cases = []
    for doc_id in new_ids:
        rec = by_id[doc_id]
        pages = sample_pages(doc_id)
        test_cases.append({
            "id": doc_id,
            "root_label": rec["root_label"],
            "label": rec["label"],
            "sampled_pages": pages,
            "image_paths": image_paths(doc_id, pages),
        })

    out = {
        "purpose": "Extraction-only data file (no classification fields) for the "
                   "193 p1_p2_no documents with rendered pages but no OCR text yet. "
                   "Feed to extract_text.py --data eval_data_p1_p2_no_extra.json "
                   "--out extracted_text.json --resume to extend the canonical "
                   "233-entry extracted_text.json in place.",
        "few_shot": [],
        "test_cases": test_cases,
    }

    out_path = HERE / OUT_NAME
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"p1_p2_no total ids: {len(by_id)}")
    print(f"already in extracted_text.json: {len(existing_ids & set(by_id.keys()))}")
    print(f"no rendered pages available (skipped, unresolved): {len(no_pages)}: {no_pages}")
    print(f"new ids to extract: {len(new_ids)}")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
