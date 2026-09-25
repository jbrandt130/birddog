#!/usr/bin/env python3
"""
Build eval_data.json for the content_code classifier: predict J / M / U / N
(a Jewish-content likelihood scale) from metadata alone.

WHY THIS TASK MATTERS MOST, of everything tried so far. The name-density
experiment established that estimated names-per-page separates P1 from P2
well (AUC 0.84) but is INVERTED for P2 vs NO (AUC 0.29) -- documents marked
do-not-acquire are systematically DENSER than P2 ones. The reason turned out
to be that density counts people, while the project's value is JEWISH people:
the densest NO documents are Greek Catholic parish lists, general population
lists, and repatriate registers. So the cost-benefit score needs a second
factor:

    value/cost  ~=  P(Jewish content)  x  names_per_page
                    └── THIS TASK ──┘     └── density ──┘

content_code is the human-assigned version of that first factor.

STRUCTURAL FINDING (measured on these 284 documents, see the module output):
content_code and process_code are almost perfectly nested --
    U -> NO (25/25),  N -> NO (68/68),  M -> P2 (7/7),
    J -> P1 (94) or P2 (88), only 2 NO
So predicting content_code J-vs-rest is very nearly the same decision as
predicting "acquire vs do-not-acquire", and it decomposes the p1_p2_no task
into two cleaner sub-questions. A model that gets content_code right has
solved most of the triage problem.

LABEL SET: the Schema Values table defines five codes, but L ("Likely to
contain Jewish information") has exactly 1 record in train and 0 in
validation -- effectively unused -- so this task predicts J/M/U/N only.
    J = Contains Jewish information
    M = Might contain Jewish information
    U = Unlikely to have Jewish information
    N = Does not contain Jewish information
The scale is ORDINAL, so run_eval.py also maps it to a 0..1 score
(J=1.0, M=0.67, U=0.33, N=0.0) for use as the P(Jewish) multiplier.

WHY THESE DOCUMENTS: the eval reuses the 284 documents already curated for
../p1_p2_no/ rather than sampling fresh. Two reasons. (1) Fond diversity:
records in the ML Document Set are ordered so that consecutive rows come
from the same fond -- a sequential sample of J records is all DADNO/R-6508
ZAGS books, of M records all CDIAK/57 household censuses. That would build
an eval measuring fond recognition rather than transferable signal. The
p1_p2_no set already spans 74 fonds. (2) These same documents have density
estimates and priority labels, so content_code predictions drop straight
into the combined cost-benefit analysis with no further data collection.

CLASS BALANCE, disclosed: test_cases follow the NATURAL distribution
(J 164 / N 59 / U 22 / M 5), which is realistic but means a model that
always answers J scores ~71%. run_eval.py therefore reports MACRO-averaged
recall and per-class breakdowns, not just accuracy, and M's 5 test cases
are too few to conclude anything about that class on its own.

few_shot is balanced instead (equal per class), drawn from ml_split=train
only, supplemented with a few hand-copied train records for M and U because
the p1_p2_no train slice has just 2 M and 3 U.

Usage:
    python3 build_eval_data.py

Output:
    eval_data.json -- {label_definitions, few_shot, test_cases}
"""
import json
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE.parent / "p1_p2_no" / "eval_data.json"
CC_MAP = HERE / "content_code_by_id.json"

LABELS = ["J", "M", "U", "N"]
N_FEW_SHOT_PER_CLASS = 6

LABEL_DEFINITIONS = {
    "J": "Contains Jewish information",
    "M": "Might contain Jewish information",
    "U": "Unlikely to have Jewish information",
    "N": "Does not contain Jewish information",
}

# Extra ml_split=train records, pulled directly from the ML Document Set to
# fill out the thin M and U few-shot classes. Kept verbatim rather than
# re-queried so the eval is reproducible offline.
SUPPLEMENT = [
    {"label": "CDIAK/491/96/23", "root_label": "CDIAK", "level": "case",
     "page_description": "Geometrical inventory of the Gleva public estate of Kyiv county",
     "doc_type": ["O"], "page_count": 71, "content_code": "M"},
    {"label": "CDIAK/57/1/279", "root_label": "CDIAK", "level": "case",
     "page_description": "Yard census of residents of the regimental town of Pereyaslav",
     "doc_type": ["C"], "page_count": 1056, "content_code": "M"},
    {"label": "CDIAK/693/1/111", "root_label": "CDIAK", "level": "case",
     "page_description": "Alphabetical list of workers and employees of the institutions of the South-Western Railways",
     "doc_type": ["L"], "page_count": 43, "content_code": "M"},
    {"label": "CDIAK/491/96/41", "root_label": "CDIAK", "level": "case",
     "page_description": "Inventory of the Kozhukhiv state estate of the Kyiv county",
     "doc_type": ["O"], "page_count": 166, "content_code": "M"},
    {"label": "CDIAK/57/1/258", "root_label": "CDIAK", "level": "case",
     "page_description": "Documents (copies of court decisions, deeds of gift, deeds of sale, universalized Ukrainian hetmans, etc. for 1676 and other years) confirming the ownership of the residents of the Kaniv Hundred of the Pereyaslav Regiment",
     "doc_type": ["O"], "page_count": 328, "content_code": "U"},
    {"label": "CDIAK/127/235/30", "root_label": "CDIAK", "level": "case",
     "page_description": "The case of the request of a state peasant from the village of Novi Petrivtsi, Pelageya Tupychka, for permission to marry again due to her husband's old age and illness",
     "doc_type": ["O"], "page_count": 17, "content_code": "U"},
    {"label": "CDIAK/57/1/391", "root_label": "CDIAK", "level": "case",
     "page_description": "Yard census of residents of the Kozeletska Hundred of the Kyiv Regiment",
     "doc_type": ["C"], "page_count": 268, "content_code": "U"},
]


def as_record(rec, code):
    """Strip to metadata the model is allowed to see. process_code / target is
    deliberately NOT included: it is a downstream human judgment that content_code
    largely determines, so showing it would leak the answer."""
    return {
        "id": rec.get("id"),
        "label": rec["label"],
        "root_label": rec["root_label"],
        "level": rec.get("level"),
        "years": rec.get("years"),
        "page_description": rec.get("page_description"),
        "page_native_description": rec.get("page_native_description"),
        "doc_type": rec.get("doc_type") or [],
        "page_count": rec.get("page_count"),
        "target": code,
    }


def main():
    src = json.loads(SRC.read_text(encoding="utf-8"))
    cc = {int(k): v for k, v in json.loads(CC_MAP.read_text(encoding="utf-8")).items()}

    train_pool = defaultdict(list)
    for rec in src["few_shot"]:
        code = cc.get(rec["id"])
        if code in LABELS:
            train_pool[code].append(as_record(rec, code))
    for extra in SUPPLEMENT:
        train_pool[extra["content_code"]].append(as_record(extra, extra["content_code"]))

    few_shot = []
    for code in LABELS:
        pool = train_pool[code]
        take = pool[:N_FEW_SHOT_PER_CLASS]
        if len(take) < N_FEW_SHOT_PER_CLASS:
            print(f"  note: only {len(take)} few-shot example(s) available for {code}")
        few_shot.extend(take)
    # interleave so the model doesn't see all of one class in a row
    few_shot.sort(key=lambda r: (LABELS.index(r["target"]) * 0, r["label"]))
    by_class = defaultdict(list)
    for r in few_shot:
        by_class[r["target"]].append(r)
    interleaved = []
    for i in range(max(len(v) for v in by_class.values())):
        for code in LABELS:
            if i < len(by_class[code]):
                interleaved.append(by_class[code][i])
    few_shot = interleaved

    test_cases = []
    missing = 0
    for rec in src["test_cases"]:
        code = cc.get(rec["id"])
        if code not in LABELS:
            missing += 1
            continue
        test_cases.append(as_record(rec, code))

    out = {
        "purpose": "Predict content_code (J/M/U/N Jewish-content likelihood) "
                   "from metadata. Supplies the P(Jewish) factor for the "
                   "cost-benefit score; see module docstring.",
        "label_definitions": LABEL_DEFINITIONS,
        "ordinal_scores": {"J": 1.0, "M": 0.67, "U": 0.33, "N": 0.0},
        "few_shot": few_shot,
        "test_cases": test_cases,
    }
    (HERE / "eval_data.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"few_shot:   {len(few_shot)} {dict(Counter(r['target'] for r in few_shot))}")
    print(f"test_cases: {len(test_cases)} {dict(Counter(r['target'] for r in test_cases))}"
          + (f"  ({missing} skipped, no content_code)" if missing else ""))
    maj = Counter(r["target"] for r in test_cases).most_common(1)[0]
    print(f"\nMajority-class baseline: always answer {maj[0]} -> "
          f"{100*maj[1]/len(test_cases):.1f}% accuracy. Beat this, and read "
          f"MACRO recall rather than raw accuracy.")
    fonds = len({'/'.join(r['label'].split('/')[:2]) for r in test_cases})
    print(f"test_cases span {fonds} distinct fonds.")


if __name__ == "__main__":
    main()
