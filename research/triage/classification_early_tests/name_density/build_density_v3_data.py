#!/usr/bin/env python3
"""
Build eval_data_v3.json for the LARGER density test: k=20 systematic pages
across the whole document, over the full validation split.

GOAL: estimate P(density | priority), i.e. the distribution of estimated
name density conditioned on the human P1/P2/NO label, using priority as a
proxy for value. This is deliberately INDEPENDENT of doc_type -- the 25-page
hand calibration showed doc_type does NOT mediate density the way expected
(documents catalogued L averaged 0.0 people per sampled page, while O
averaged 7.3), because the catalogue label describes the document while a
sampled page shows typical content, and for sparse documents those differ.

WHY k=20 AND FULL-RANGE (vs the k=5 middle-half pilot) -- both changes are
forced by the hand-count calibration, see ../data/page_sampler.py:
  - at k=5 a document's density has ~103% relative standard error, and a
    document with 10% name-bearing pages scores ZERO 59% of the time
  - the middle-half window biases density upward by excluding covers and
    blank end matter, which are real pages with real extraction cost

SAMPLE REUSE: many pages are already rendered (the first-10 front sample
and the k=5 middle sample both live in data/pages/<id>/). This script
reports which of the k=20 systematic pages already exist, so the render
only has to produce the remainder.

Usage:
    python3 build_density_v3_data.py            # report what needs rendering
    python3 build_density_v3_data.py --write    # write eval_data_v3.json

Output:
    eval_data_v3.json        -- same shape as eval_data.json (train/validation)
    render_manifest_v3.json  -- {doc_id: [page numbers still to render]}
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
DATA_ROOT = HERE.parent / "data"
SRC = HERE.parent / "p1_p2_no" / "eval_data.json"

sys.path.insert(0, str(DATA_ROOT))
from page_sampler import sample_systematic  # noqa: E402

K_PAGES = 20


def build(records, pick, section, require_rendered=True):
    out, skipped, partial = [], [], []
    for rec in records:
        page_count = rec.get("page_count")
        if not page_count:
            skipped.append(rec["label"])
            continue
        pages = pick(page_count)
        if require_rendered:
            # A handful of pages are genuinely unrenderable: PyMuPDF raises
            # "Overly large image" at 200 DPI on a few enormous scans (they
            # do render at <=100 DPI). Drop just those pages rather than the
            # whole document -- the affected docs still have 17-19 of 20, so
            # their density estimate is barely affected, whereas listing a
            # nonexistent path would crash extraction mid-run.
            keep = [p for p in pages
                    if (DATA_ROOT / f"pages/{rec['id']}/{p:05d}.jpg").exists()]
            if len(keep) < len(pages):
                partial.append((rec["label"], len(keep), len(pages)))
            pages = keep
            if not pages:
                skipped.append(rec["label"] + " (no pages rendered)")
                continue
        out.append({
            "id": rec["id"],
            "label": rec["label"],
            "root_label": rec["root_label"],
            "doc_type": rec.get("doc_type") or [],
            "page_count": page_count,
            "page_description": rec.get("page_description"),
            # analysis only -- never shown to the model
            "priority_target": rec.get("target"),
            "sampled_pages": pages,
            "image_paths": [f"pages/{rec['id']}/{p:05d}.jpg" for p in pages],
        })
    print(f"{section}: {len(out)} docs ({len(skipped)} skipped, no page_count)")
    if partial:
        print(f"  {len(partial)} doc(s) with unrenderable pages dropped: "
              + ", ".join(f"{l} ({h}/{w})" for l, h, w in partial))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("-k", type=int, default=K_PAGES)
    args = ap.parse_args()

    src = json.loads(SRC.read_text(encoding="utf-8"))
    pick = sample_systematic(k=args.k)

    train = build(src["few_shot"], pick, "train")
    validation = build(src["test_cases"], pick, "validation")

    # what's already on disk vs what still needs rendering
    manifest, have, need = {}, 0, 0
    for rec in train + validation:
        missing = [p for p, rel in zip(rec["sampled_pages"], rec["image_paths"])
                   if not (DATA_ROOT / rel).exists()]
        have += len(rec["sampled_pages"]) - len(missing)
        need += len(missing)
        if missing:
            manifest[str(rec["id"])] = missing

    total = have + need
    print(f"\nk={args.k} systematic sample over {len(train) + len(validation)} documents")
    print(f"  total page images needed: {total}")
    print(f"  already rendered:         {have} ({100*have/total:.0f}%)")
    print(f"  still to render:          {need} across {len(manifest)} documents")

    pri = Counter(r["priority_target"] for r in validation)
    print(f"\nvalidation priority mix (the conditioning variable): {dict(pri)}")

    if args.write:
        out = {
            "purpose": "k=20 systematic full-range density sample for "
                       "estimating P(density | P1/P2/NO).",
            "k_pages": args.k,
            "page_policy": "systematic across full page range, seeded random "
                           "start (see ../data/page_sampler.py:sample_systematic)",
            "train": train,
            "validation": validation,
        }
        (HERE / "eval_data_v3.json").write_text(
            json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        (HERE / "render_manifest_v3.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"\nWrote eval_data_v3.json and render_manifest_v3.json")
    else:
        print("\n(dry run -- pass --write to save eval_data_v3.json + manifest)")


if __name__ == "__main__":
    main()
