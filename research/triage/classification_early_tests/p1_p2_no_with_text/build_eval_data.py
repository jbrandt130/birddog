#!/usr/bin/env python3
"""
Build eval_data.json for the metadata+OCR-text p1_p2_no classifier by
merging ../p1_p2_no/eval_data.json (the metadata-only baseline's exact
few_shot/test_cases split and P1/P2/NO targets) with the extracted printed
text from ../vision_doc_recognition/extracted_text.json (minicpm-v4.5 OCR
over sampled page images -- 426 documents as of the p1_p2_no extraction
extension, see ../vision_doc_recognition/build_p1_p2_no_extraction_data.py).

Mirrors ../doc_type_with_text/build_eval_data.py exactly: same "extra field
merged onto the existing metadata-only record" shape, same join key
("label", the archival label string -- stable across both id spaces),
same policy of dropping records with no extracted text rather than
including a text-less record with an empty field.

Unlike the doc_type_with_text case (1 known drop), p1_p2_no draws on 284
document ids of which 19 have neither rendered pages nor a source PDF
(permanently unavailable -- see build_p1_p2_no_extraction_data.py's
"no_pages" list). Those 19 are dropped here. Everything else (all 265 ids
with rendered pages) now has extracted text after the extraction extension,
so drops should be exactly the 19 unavailable ids, no more.

Usage:
    python3 build_eval_data.py

Output:
    eval_data.json -- same shape as ../p1_p2_no/eval_data.json, each kept
    record with an added "extracted_text" field (untruncated; run_eval.py
    applies its own --max-text-chars cap at prompt-build time).
"""
import json
from pathlib import Path

HERE = Path(__file__).parent
P1_P2_NO_DATA = HERE.parent / "p1_p2_no" / "eval_data.json"
EXTRACTED_TEXT = HERE.parent / "vision_doc_recognition" / "extracted_text.json"
OUT = HERE / "eval_data.json"


def main():
    p1p2no_data = json.loads(P1_P2_NO_DATA.read_text(encoding="utf-8"))
    extracted = json.loads(EXTRACTED_TEXT.read_text(encoding="utf-8"))
    text_by_label = {v["label"]: v["extracted_text"] for v in extracted.values()}

    def merge(records, section_name):
        merged = []
        dropped = []
        for rec in records:
            text = text_by_label.get(rec["label"])
            if text is None:
                dropped.append(rec["label"])
                continue
            merged.append({**rec, "extracted_text": text})
        if dropped:
            print(f"{section_name}: dropped {len(dropped)} record(s) with no "
                  f"extracted text: {dropped}")
        print(f"{section_name}: {len(merged)}/{len(records)} records kept")
        return merged

    few_shot = merge(p1p2no_data["few_shot"], "few_shot")
    test_cases = merge(p1p2no_data["test_cases"], "test_cases")

    out = {
        "label_definitions": p1p2no_data["label_definitions"],
        "few_shot": few_shot,
        "test_cases": test_cases,
    }
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {OUT} ({len(few_shot)} few_shot, {len(test_cases)} test_cases)")


if __name__ == "__main__":
    main()
