#!/usr/bin/env python3
"""
Build eval_data.json for the name-density estimation task.

WHAT THIS TASK IS: for each document, estimate how many distinct personal
names it is likely to contain, by looking at a sample of its MIDDLE pages
and asking a vision model a layout/counting question ("is this page a
register or list of people, and roughly how many name entries are on it?")
rather than a judgment question ("is this document valuable?") or a
transcription question ("read the names").

WHY THIS FRAMING: the two prior vision attempts failed for reasons this one
avoids. Direct doc_type classification from page images scored 45/216 --
it asked the model for a judgment call it couldn't make from imagery.
OCR text extraction deliberately skipped handwriting, which is where the
personal names actually are in registers and lists, which is likely why
adding OCR text HURT the p1_p2_no task (significant, McNemar p=0.041).
Counting rows in a ruled table is layout recognition: a model that cannot
read Cyrillic cursive can still see that a page is a register with ~30
person-rows. That makes name density the most direct measurable proxy for
what the whole project is actually ranking for.

WHY IT MATTERS DOWNSTREAM: the eventual goal is cost-benefit ranking.
    expected value ~ names_per_page * page_count   (genealogical yield)
    extraction cost ~ page_count                    (roughly per-page)
so the value/cost ratio is essentially NAME DENSITY. The P1/P2/NO priority
label is a human proxy for this; density measures it directly. Note the
ranking this produces is deliberately NOT the same as sorting by P1/P2/NO
-- a long sparse case file and a short dense name list can share a
priority label but have very different yield per unit of extraction cost.
Disagreements between the two are the point, not a defect.

SPLITS (ml_split in the NocoDB Documents table, inherited from the
doc_type project's physical split):
    train      - source for few-shot / prompt development
    validation - fond-disjoint holdout; safe to reuse during iteration
    test       - whole archives held out; touch ONCE for final reporting
The split is already encoded in ../p1_p2_no/eval_data.json: its few_shot
records were sourced via (ml_split,eq,train) and its test_cases via
(ml_split,eq,validation). This script inherits that membership rather than
re-querying NocoDB, which also guarantees these sets stay aligned with the
p1_p2_no experiments they will be analysed against. Nothing here draws
from ml_split=test.

PAGES: sampled by ../data/page_sampler.py's middle-half policy (k=5 from
[0.25, 0.75] * page_count, fixed seed). The same policy is applied at
render time and here at read time, so these paths resolve to files that
already exist -- the page numbers are a deterministic function of
page_count. Deriving image_paths from the sampler rather than globbing the
directory matters: data/pages/<id>/ is a SHARED tree that also holds the
first-10 front-matter renders used by doc_type/OCR, and globbing would
sweep those in and bias density downward (front matter is covers and title
leaves, not name lists).

NO GROUND TRUTH: there are no hand-counted name densities yet, so this
produces an ESTIMATION set, not a scored eval. Validation is indirect
(does estimated density separate P1/P2/NO? does it correlate with
doc_type L?) until ~30 pages are hand-counted for calibration.

Usage:
    python3 build_eval_data.py

Output:
    eval_data.json -- {"train": [...], "validation": [...]}, each record:
      id, label, root_label, doc_type, page_count, page_description,
      priority_target (P1/P2/NO -- carried for DOWNSTREAM ANALYSIS ONLY,
      never shown to the model), sampled_pages, image_paths
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
DATA_ROOT = HERE.parent / "data"
SRC = HERE.parent / "p1_p2_no" / "eval_data.json"
OUT = HERE / "eval_data.json"

sys.path.insert(0, str(DATA_ROOT))
from page_sampler import sample_middle_pages, DEFAULT_K  # noqa: E402

# Same policy as render time. If you change k here, the pages won't exist
# on disk until they're re-rendered with the matching sampler.
K_PAGES = DEFAULT_K


def build_records(records, pick, section_name):
    out = []
    skipped_no_pagecount = []
    skipped_no_images = []
    for rec in records:
        page_count = rec.get("page_count")
        if not page_count:
            # Without a page count there's no window to sample from, and
            # no denominator for expected-total-names either.
            skipped_no_pagecount.append(rec["label"])
            continue
        pages = pick(page_count)
        paths = [f"pages/{rec['id']}/{p:05d}.jpg" for p in pages]
        missing = [p for p in paths if not (DATA_ROOT / p).exists()]
        if missing:
            skipped_no_images.append((rec["label"], len(missing), len(paths)))
            continue
        out.append({
            "id": rec["id"],
            "label": rec["label"],
            "root_label": rec["root_label"],
            "doc_type": rec.get("doc_type") or [],
            "page_count": page_count,
            "page_description": rec.get("page_description"),
            # Carried for downstream analysis only -- the density prompt
            # never sees this. Showing it would leak the human judgment
            # this task is meant to be an independent measurement of.
            "priority_target": rec.get("target"),
            "sampled_pages": pages,
            "image_paths": paths,
        })
    print(f"{section_name}: {len(out)} usable")
    if skipped_no_pagecount:
        print(f"  skipped, no page_count ({len(skipped_no_pagecount)}): "
              f"{skipped_no_pagecount[:5]}{' ...' if len(skipped_no_pagecount) > 5 else ''}")
    if skipped_no_images:
        print(f"  skipped, middle pages not rendered yet ({len(skipped_no_images)}): "
              f"{[s[0] for s in skipped_no_images[:5]]}"
              f"{' ...' if len(skipped_no_images) > 5 else ''}")
    return out


def main():
    src = json.loads(SRC.read_text(encoding="utf-8"))
    pick = sample_middle_pages(k=K_PAGES)

    # few_shot == ml_split train, test_cases == ml_split validation.
    train = build_records(src["few_shot"], pick, "train (prompt/few-shot pool)")
    validation = build_records(src["test_cases"], pick, "validation (inference set)")

    out = {
        "purpose": "Name-density estimation from middle-page samples. "
                   "Train split is the prompt/few-shot pool; validation is "
                   "the inference set. priority_target is carried for "
                   "downstream analysis only and is never shown to the model.",
        "k_pages": K_PAGES,
        "page_policy": "middle-half [0.25,0.75] * page_count, fixed seed "
                       "(see ../data/page_sampler.py)",
        "train": train,
        "validation": validation,
    }
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {OUT} ({len(train)} train, {len(validation)} validation)")
    if not train or not validation:
        print("NOTE: sections are empty or short -- if the middle-page render "
              "is still running, re-run this once it finishes.")


if __name__ == "__main__":
    main()
