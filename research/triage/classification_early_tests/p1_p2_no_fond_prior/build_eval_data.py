#!/usr/bin/env python3
"""
Build eval_data.json for the fond-prior variant of the P1/P2/NO classifier:
../p1_p2_no/eval_data.json with one added field per record --
"fond_siblings": the leave-one-out count of P1/P2/NO labels among OTHER
records in the same fond (archive/fond, the first two components of the
label) across the full labeled pool (few_shot + test_cases, 284 records).

WHY: offline analysis of ../p1_p2_no/results.csv showed triage labels are
strongly fond-clustered: within fonds having >=3 test records, the majority
label covers 74.6% of records (vs 33% chance), 50% of all model errors sit
in 9 fonds that are >50% wrong as a block, and a naive leave-one-out fond-
majority "classifier" scores 74.9% on covered records vs the model's 57.6%
(McNemar p=0.00008). A sequential cold-start simulation (prior may only use
already-processed records) still beat model-only 66.0% vs 56.4%. Human
triagers evidently made fond-level decisions the record-level model can't
see.

This experiment tests OPTION 2 of the proposed policy: inject the sibling
label distribution into the prompt and let the MODEL weigh it against the
description evidence, rather than hard-overriding with the majority. The
model should be able to follow the prior on typical records but deviate on
genuinely atypical ones, and degrades gracefully to current behavior when a
fond has no classified siblings yet.

LEAKAGE NOTE (deliberate, disclosed): sibling counts are computed leave-one-
out over the labeled eval pool, so each test record's own label is excluded
but its siblings' true labels are used. This mirrors the intended production
setting -- fonds arrive partially human-triaged and the counts come from
already-confirmed records -- and matches the 74.9% LOO upper-bound analysis
this experiment is testing against. It does NOT leak the record's own label.

Few-shot examples get the same LOO treatment so they demonstrate how to use
the feature (including some with "(none)" siblings).

Usage:
    python3 build_eval_data.py

Output:
    eval_data.json -- same shape as ../p1_p2_no/eval_data.json, all 34
    few_shot and all 250 test_cases kept (no OCR dependency, so no drops),
    each record with an added "fond_siblings" field like
    {"P1": 3, "P2": 1, "NO": 0}.
"""
import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE.parent / "p1_p2_no" / "eval_data.json"
OUT = HERE / "eval_data.json"

VALID_LABELS = ["P1", "P2", "NO"]


def fond(label):
    return "/".join(label.split("/")[:2])


def main():
    data = json.loads(SRC.read_text(encoding="utf-8"))
    all_recs = data["few_shot"] + data["test_cases"]

    fond_counts = defaultdict(lambda: {l: 0 for l in VALID_LABELS})
    for rec in all_recs:
        fond_counts[fond(rec["label"])][rec["target"]] += 1

    def with_siblings(rec):
        counts = dict(fond_counts[fond(rec["label"])])
        counts[rec["target"]] -= 1  # leave self out
        return {**rec, "fond_siblings": counts}

    few_shot = [with_siblings(r) for r in data["few_shot"]]
    test_cases = [with_siblings(r) for r in data["test_cases"]]

    out = {
        "label_definitions": data["label_definitions"],
        "few_shot": few_shot,
        "test_cases": test_cases,
    }
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    n_with = sum(1 for r in test_cases if sum(r["fond_siblings"].values()) > 0)
    print(f"Wrote {OUT} ({len(few_shot)} few_shot, {len(test_cases)} test_cases)")
    print(f"test_cases with at least one classified fond sibling: {n_with}/{len(test_cases)}")


if __name__ == "__main__":
    main()
