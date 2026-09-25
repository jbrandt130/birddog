#!/usr/bin/env python3
"""
Build eval_data_jewish_pilot.json: the targeted set for testing whether the
page-level Jewish-content signals (hebrew_script, jewish_markers) carry
usable information.

THE QUESTION THIS ANSWERS. Splitting the 250 content_code test cases by what
their metadata actually contains:

    positive keyword (jew/synagogue/rabbi)   72 docs   P(J)=95.8%
    negative keyword (church/parish/priest)  11 docs   P(J)= 9.1%
    SILENT (no keyword either way)          167 docs   P(J)=56.3%

Stage 1 of the cascade -- a plain regex -- resolves the first two buckets at
high precision, and qwen2.5:14b adds nothing over it there (95.8% on the
positive bucket, the same as the regex). The whole problem is the SILENT 67%,
where the model scored 27.5%, WORSE than always guessing J (56.3%). Metadata
has been exhausted; only new information helps.

So this pilot deliberately samples ONLY from the silent bucket. Documents
whose description already says "Jewish" would inflate any result -- they are
already solved and would tell us nothing about the cascade's weak point.

WHAT WOULD COUNT AS SUCCESS. The signal is expected to be high-precision /
low-recall by construction: pre-1917 Jewish metric books are bilingual and
should light up, while Soviet-era ZAGS records of Jewish individuals are
entirely Cyrillic and will not. So:
  - PRECISION is the make-or-break number. If pages of true-N/U documents
    light up as often as true-J ones, the signal is noise and stage 3 dies.
  - RECALL of ~30-40% on silent-J documents would already be valuable, since
    it closes part of a gap nothing else addresses.
  - Near-zero recall means stage 3 is a dead end and the fond prior (stage 2)
    is the only remaining lever.

The existing OCR data cannot answer this: extracted_text.json shows a Jewish
marker for only 10/265 documents, but that pass was told to transcribe
printed text and never asked about Hebrew. Document 7106 ("Metric book of the
Jewish community of Khodoriv") had its page 6 OCR'd -- a page whose printed
Hebrew column headers are plainly visible -- and the transcription contains
no Hebrew at all. That 5.4% is the prompt's blind spot, not the corpus.

Usage:
    python3 build_jewish_signal_pilot.py [--per-class 20]

Output:
    eval_data_jewish_pilot.json -- same shape as eval_data_v3.json, with
    content_code carried per record for scoring.
"""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).parent
DATA_ROOT = HERE.parent / "data"
SRC = HERE.parent / "p1_p2_no" / "eval_data.json"
CC = HERE.parent / "content_code" / "content_code_by_id.json"

sys.path.insert(0, str(DATA_ROOT))
from page_sampler import sample_systematic  # noqa: E402

POS = re.compile(r"jew|hebrew|yiddish|synagog|rabbi", re.I)
NEG = re.compile(r"church|parish|priest|orthodox|monaster|deanery|consistor|cathedral", re.I)
K_PAGES = 20


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-class", type=int, default=20,
                    help="target documents per group (silent-J, silent-notJ)")
    ap.add_argument("-k", type=int, default=K_PAGES)
    args = ap.parse_args()

    src = json.loads(SRC.read_text(encoding="utf-8"))
    cc = {int(k): v for k, v in json.loads(CC.read_text(encoding="utf-8")).items()}
    pick = sample_systematic(k=args.k)

    groups = defaultdict(list)
    for rec in src["few_shot"] + src["test_cases"]:
        code = cc.get(rec["id"])
        pc = rec.get("page_count")
        if code is None or not pc:
            continue
        desc = rec.get("page_description") or ""
        if POS.search(desc) or NEG.search(desc):
            continue  # already resolved by stage 1 -- excluded on purpose
        pages = pick(pc)
        paths = [f"pages/{rec['id']}/{p:05d}.jpg" for p in pages]
        keep = [(p, r) for p, r in zip(pages, paths) if (DATA_ROOT / r).exists()]
        if len(keep) < 5:
            continue  # too few rendered pages to judge a document on
        group = "silent_J" if code == "J" else "silent_notJ"
        groups[group].append({
            "id": rec["id"],
            "label": rec["label"],
            "root_label": rec["root_label"],
            "doc_type": rec.get("doc_type") or [],
            "page_count": pc,
            "page_description": desc,
            "content_code": code,
            "priority_target": rec.get("target"),
            "sampled_pages": [p for p, _ in keep],
            "image_paths": [r for _, r in keep],
        })

    # Spread across fonds: content_code errors clustered 56% into 12 fonds, so
    # a fond-concentrated pilot would measure fond quirks, not the signal.
    selected = []
    for g, recs in groups.items():
        by_fond = defaultdict(list)
        for r in recs:
            by_fond["/".join(r["label"].split("/")[:2])].append(r)
        fonds = sorted(by_fond, key=lambda f: -len(by_fond[f]))
        out, i = [], 0
        while len(out) < args.per_class and any(by_fond[f] for f in fonds):
            for f in fonds:
                if by_fond[f] and len(out) < args.per_class:
                    out.append(by_fond[f].pop(0))
            i += 1
            if i > 100:
                break
        print(f"{g}: {len(out)} docs from {len({r['label'].split('/')[0] + '/' + r['label'].split('/')[1] for r in out})} fonds "
              f"(pool was {len(recs)})")
        selected.extend(out)

    n_pages = sum(len(r["image_paths"]) for r in selected)
    out = {
        "purpose": "Pilot: do page-level Hebrew/Jewish-marker signals "
                   "discriminate content_code J from non-J among documents "
                   "whose METADATA is silent? Stage-1-resolvable documents "
                   "are deliberately excluded.",
        "k_pages": args.k,
        "train": [],
        "validation": selected,
    }
    (HERE / "eval_data_jewish_pilot.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nWrote eval_data_jewish_pilot.json: {len(selected)} documents, "
          f"{n_pages} page images")
    print(f"  content_code mix: {dict(Counter(r['content_code'] for r in selected))}")
    print(f"  est. runtime ~{n_pages*2.4/60:.0f} min, ~${n_pages*2.4*0.000306:.2f}")


if __name__ == "__main__":
    main()
