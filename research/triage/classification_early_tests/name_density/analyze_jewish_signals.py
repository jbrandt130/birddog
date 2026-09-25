#!/usr/bin/env python3
"""
Score the Jewish-content page signals from a density run.

Reports, for hebrew_script and jewish_markers separately and combined, at
both PAGE and DOCUMENT level: precision, recall, and the lift over the base
rate. Document level is what matters -- a document counts as positive if the
signal fires on ANY sampled page, because one bilingual register page is
enough to identify a Jewish record.

HOW TO READ IT:
  - PRECISION is make-or-break. If true-N/U documents light up as often as
    true-J ones, the signal is noise and cascade stage 3 is dead.
  - RECALL around 30-40% on metadata-silent J documents is already valuable:
    those documents are the cascade's weak point, where the LLM scored 27.5%,
    worse than always guessing J.
  - LIFT = P(J | signal fires) / P(J | base rate). Below ~1.2 the signal is
    not carrying its weight.

Run on the pilot (metadata-silent documents only, so the base rate is near
50% and the numbers are not inflated by already-solved cases):
    python3 analyze_jewish_signals.py --infile density_jewish_pilot.json
"""

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).parent


def rates(docs, predicate, name, base):
    """Document-level precision/recall for a signal predicate."""
    fired = [d for d in docs if predicate(d)]
    tp = sum(1 for d in fired if d["content_code"] == "J")
    fp = len(fired) - tp
    all_j = sum(1 for d in docs if d["content_code"] == "J")
    fn = all_j - tp
    prec = tp / len(fired) if fired else float("nan")
    rec = tp / all_j if all_j else float("nan")
    lift = (prec / base) if fired and base else float("nan")
    print(f"  {name:<34} fired on {len(fired):>3}/{len(docs)}   "
          f"precision={prec:>6.1%}  recall={rec:>6.1%}  lift={lift:>4.2f}x")
    return {"fired": len(fired), "tp": tp, "fp": fp, "fn": fn,
            "precision": prec, "recall": rec, "lift": lift}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--infile", default="density_jewish_pilot.json")
    args = ap.parse_args()

    path = HERE / args.infile
    if not path.exists():
        raise SystemExit(f"{path} not found -- run the density extraction first.")
    data = json.loads(path.read_text(encoding="utf-8"))

    docs = []
    for did, doc in data.items():
        code = doc.get("content_code")
        if code is None:
            continue
        pages = doc["pages"]
        docs.append({
            "id": did, "label": doc["label"], "content_code": code,
            "page_count": doc["page_count"],
            "doc_type": ",".join(doc.get("doc_type") or []),
            "n_pages": len(pages),
            "n_heb": sum(1 for p in pages if p.get("hebrew_script")),
            "n_mark": sum(1 for p in pages if p.get("jewish_markers")),
            "density": statistics.mean(
                [p["named_individuals"] for p in pages
                 if p.get("named_individuals") is not None] or [0]),
        })
    if not docs:
        raise SystemExit("no documents with content_code in the density file")

    n = len(docs)
    n_j = sum(1 for d in docs if d["content_code"] == "J")
    base = n_j / n
    n_pages = sum(d["n_pages"] for d in docs)
    print(f"{n} documents, {n_pages} pages")
    print(f"content_code mix: {dict(Counter(d['content_code'] for d in docs))}")
    print(f"BASE RATE P(J) = {base:.1%}  <- any signal must beat this to be useful")

    print("\n" + "=" * 74)
    print("DOCUMENT-LEVEL SIGNAL QUALITY (fires if ANY sampled page shows it)")
    print("=" * 74)
    rates(docs, lambda d: d["n_heb"] > 0, "hebrew_script on >=1 page", base)
    rates(docs, lambda d: d["n_mark"] > 0, "jewish_markers on >=1 page", base)
    rates(docs, lambda d: d["n_heb"] > 0 or d["n_mark"] > 0, "EITHER signal", base)
    rates(docs, lambda d: d["n_heb"] > 0 and d["n_mark"] > 0, "BOTH signals", base)
    rates(docs, lambda d: d["n_heb"] >= 2, "hebrew_script on >=2 pages", base)

    print("\n" + "=" * 74)
    print("PAGE-LEVEL RATE BY TRUE CODE (is the signal specific, or everywhere?)")
    print("=" * 74)
    by_code = defaultdict(lambda: [0, 0, 0])
    for d in docs:
        c = by_code[d["content_code"]]
        c[0] += d["n_pages"]; c[1] += d["n_heb"]; c[2] += d["n_mark"]
    print(f"  {'code':<6}{'pages':>8}{'hebrew':>9}{'heb %':>8}{'markers':>9}{'mark %':>8}")
    for code in ("J", "M", "U", "N"):
        if code in by_code:
            p, h, m = by_code[code]
            print(f"  {code:<6}{p:>8}{h:>9}{100*h/p:>7.1f}%{m:>9}{100*m/p:>7.1f}%")

    print("\n" + "=" * 74)
    print("DOCUMENTS WHERE A SIGNAL FIRED")
    print("=" * 74)
    for d in sorted(docs, key=lambda d: -(d["n_heb"] + d["n_mark"])):
        if d["n_heb"] or d["n_mark"]:
            ok = "OK " if d["content_code"] == "J" else "FP "
            print(f"  {ok} {d['label']:<24} code={d['content_code']} "
                  f"heb={d['n_heb']}/{d['n_pages']} mark={d['n_mark']}/{d['n_pages']} "
                  f"{d['doc_type']}")

    misses = [d for d in docs if d["content_code"] == "J"
              and not d["n_heb"] and not d["n_mark"]]
    print(f"\nTrue-J documents with NO signal at all: {len(misses)}/{n_j}")
    for d in misses[:10]:
        print(f"     {d['label']:<24} {d['doc_type']:<8} {d['page_count']}pp")

    print("\n" + "=" * 74)
    print("VERDICT")
    print("=" * 74)
    fired = [d for d in docs if d["n_heb"] or d["n_mark"]]
    if not fired:
        print("  Signal never fired -- stage 3 is a dead end. Fall back to the")
        print("  fond prior (stage 2) as the only remaining lever.")
    else:
        prec = sum(1 for d in fired if d["content_code"] == "J") / len(fired)
        rec = sum(1 for d in fired if d["content_code"] == "J") / n_j
        if prec >= max(0.80, base + 0.20) and rec >= 0.25:
            print(f"  USABLE: precision {prec:.0%} at recall {rec:.0%} on documents where")
            print("  metadata is silent. Worth wiring into the cascade as stage 3.")
        elif prec >= max(0.80, base + 0.20):
            print(f"  HIGH PRECISION ({prec:.0%}) BUT LOW RECALL ({rec:.0%}): fires too")
            print("  rarely to matter much, though it is nearly free to keep since the")
            print("  density pass already encodes these images.")
        else:
            print(f"  NOT DISCRIMINATIVE: precision {prec:.0%} vs base rate {base:.0%}.")
            print("  The signal fires on non-Jewish documents about as often as Jewish")
            print("  ones. Drop stage 3 and rely on the fond prior.")


if __name__ == "__main__":
    main()
