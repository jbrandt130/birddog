#!/usr/bin/env python3
"""
Build eval_data.json for the metadata+OCR-text doc_type classifier by
merging ../doc_type/eval_data.json (the metadata-only baseline's exact
few_shot/test_cases split and labels) with the extracted printed text from
../vision_doc_recognition/extracted_text.json (produced by
extract_text.py's minicpm-v4.5 OCR pass over sampled page images).

This exists to make an apples-to-apples comparison possible: same label
definitions, same 216 held-out test_cases, same doc_type ground truth as
../doc_type/eval_data.json. The only thing added per record is an
"extracted_text" field. run_eval.py in this folder mirrors
../doc_type/run_eval.py's prompt framing and scoring methodology exactly,
except the per-record prompt also includes this text.

One known, disclosed deviation from strict parity: ../doc_type/eval_data.json
has 18 few-shot examples; only 17 have extracted text (TSDAVO/5/2/235 was
never part of the vision/OCR document pool, so no page images or extracted
text exist for it). That one few-shot example is dropped here rather than
included without a text field, to keep every few-shot example structurally
identical. The 216 test_cases are unaffected -- all of them have extracted
text and match ../doc_type/eval_data.json exactly, which is what the actual
accuracy comparison is measured on.

Usage:
    python3 build_eval_data.py

Output:
    eval_data.json -- same shape as ../doc_type/eval_data.json, each record
    with an added "extracted_text" field (untruncated; run_eval.py applies
    its own --max-text-chars cap at prompt-build time, not here, so the cap
    stays an adjustable eval-time knob rather than baked into the data).
"""
import json
from pathlib import Path

HERE = Path(__file__).parent
DOC_TYPE_DATA = HERE.parent / "doc_type" / "eval_data.json"
EXTRACTED_TEXT = HERE.parent / "vision_doc_recognition" / "extracted_text.json"
OUT = HERE / "eval_data.json"


def main():
    doc_type_data = json.loads(DOC_TYPE_DATA.read_text(encoding="utf-8"))
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

    few_shot = merge(doc_type_data["few_shot"], "few_shot")
    test_cases = merge(doc_type_data["test_cases"], "test_cases")

    assert len(test_cases) == len(doc_type_data["test_cases"]), (
        "test_cases should match ../doc_type/eval_data.json exactly -- "
        "if this assertion fails, the comparison is no longer apples-to-apples."
    )

    out = {
        "label_definitions": doc_type_data["label_definitions"],
        "few_shot": few_shot,
        "test_cases": test_cases,
    }
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {OUT} ({len(few_shot)} few_shot, {len(test_cases)} test_cases)")


if __name__ == "__main__":
    main()
