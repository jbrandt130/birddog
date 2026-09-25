#!/usr/bin/env python3
"""
Build eval_data.json for the vision (page-image) doc_type classifier.

This is a parallel test to ../doc_type/eval_data.json: it reuses the exact
same few_shot and test_cases document identities (same archival records,
same target labels), so results are directly comparable between the
metadata-only text experiment and this page-image experiment. The
difference is what's shown to the model -- instead of an archive/label/
description text block, the model is shown a random sample of k page
images (out of the first 10 pages rendered per document in ../data/pages/<id>/).

One few_shot example from the original set (TSDAVO/5/2/235, id 34) has no
downloaded page images (its source URL wasn't a wiki file page), so it is
dropped here -- O still has 2 remaining few-shot examples without it.

Page sampling is done once here (not resampled per eval run) with a fixed
per-document random seed, so the same page images are used across repeated
runs of run_eval.py for reproducibility.
"""

import json
import random
from pathlib import Path

HERE = Path(__file__).parent
# Cached source PDFs (data/docs/) and rendered page JPGs (data/pages/) live
# outside this folder so the code tree isn't carrying ~112GB of binary data.
# The download/render scripts that produce them (wiki_download.py,
# pdf_to_jpgs.py) live there too. Stored image_paths stay relative
# ("pages/<id>/<n>.jpg") and are resolved against DATA_ROOT at read time.
DATA_ROOT = HERE.parent / "data"
K_PAGE_IMAGES = 3
MAX_RENDERED_PAGES = 10  # only the first 10 pages were rasterized per document


def available_pages(doc_id):
    """Page numbers that actually have a rendered jpg on disk. Some individual
    pages can be missing even within the first 10 -- pdf_to_jpgs.py continues
    past a page-level render failure rather than aborting the whole doc, so
    the sequence can have gaps (e.g. 1,2,3,6,7,8,9,10)."""
    d = DATA_ROOT / "pages" / str(doc_id)
    if not d.is_dir():
        return []
    return sorted(int(p.stem) for p in d.glob("*.jpg"))


def sample_pages(doc_id, k=K_PAGE_IMAGES):
    pages = available_pages(doc_id)
    n = min(k, len(pages))
    rng = random.Random(doc_id)  # deterministic per-document, independent across docs
    return sorted(rng.sample(pages, n))


def image_paths(doc_id, pages):
    return [f"pages/{doc_id}/{p:05d}.jpg" for p in pages]


def main():
    doc_type_data = json.loads((HERE.parent / "doc_type" / "eval_data.json").read_text(encoding="utf-8"))
    urls = json.loads((DATA_ROOT / "document_urls.json").read_text(encoding="utf-8"))

    by_label = {d["label"]: d for d in urls["documents"]}
    by_id = {d["id"]: d for d in urls["documents"]}

    few_shot = []
    dropped = []
    for fs in doc_type_data["few_shot"]:
        match = by_label.get(fs["label"])
        if not match:
            dropped.append(fs["label"])
            continue
        doc_id = match["id"]
        pages = sample_pages(doc_id)
        few_shot.append({
            "id": doc_id,
            "root_label": fs["root_label"],
            "label": fs["label"],
            "doc_type": fs["doc_type"],
            "target": fs["target"],
            "page_count": match.get("page_count"),
            "sampled_pages": pages,
            "image_paths": image_paths(doc_id, pages),
        })

    test_cases = []
    missing_test = []
    for tc in doc_type_data["test_cases"]:
        match = by_id.get(tc["id"])
        if not match:
            missing_test.append(tc["id"])
            continue
        doc_id = tc["id"]
        pages = sample_pages(doc_id)
        test_cases.append({
            "id": doc_id,
            "root_label": tc["root_label"],
            "label": tc["label"],
            "doc_type": tc["doc_type"],
            "page_count": match.get("page_count"),
            "sampled_pages": pages,
            "image_paths": image_paths(doc_id, pages),
        })

    out = {
        "purpose": "Parallel vision (page-image) test of the doc_type classifier -- "
                   "same document identities/targets as ../doc_type/eval_data.json, "
                   "but the model is shown sampled page images instead of metadata text.",
        "label_definitions": doc_type_data["label_definitions"],
        "k_page_images": K_PAGE_IMAGES,
        "max_rendered_pages": MAX_RENDERED_PAGES,
        "few_shot": few_shot,
        "test_cases": test_cases,
    }

    out_path = HERE / "eval_data.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"few_shot: {len(few_shot)} (dropped {len(dropped)}: {dropped})")
    print(f"test_cases: {len(test_cases)} (missing {len(missing_test)}: {missing_test})")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
