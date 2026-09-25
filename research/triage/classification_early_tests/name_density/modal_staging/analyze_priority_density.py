#!/usr/bin/env python3
"""
Estimate P(density | P1/P2/NO): the distribution of estimated name density
conditioned on the human priority label, using priority as a proxy for value.

THE QUESTION: does measured name density carry the value signal that human
triagers were expressing when they assigned P1/P2/NO? Three outcomes, all
informative:
  - Strong separation  -> density recovers human judgment automatically, and
    can extend it to unlabelled documents.
  - No separation      -> density is measuring something orthogonal to value;
    the cost-benefit framing needs rethinking.
  - Partial separation -> the most likely and most useful case. Density adds
    a yield-per-page dimension the priority label does not capture, and the
    DISAGREEMENTS are where it earns its keep.

WHY RANK-BASED STATISTICS: the page-level distribution is extremely skewed
(hand counts: twelve 0s, then 1..5, then 12, 12, 44). Means are dominated by
single dense pages, so this reports medians, quartiles and AUC rather than
means and t-tests. AUC (equivalently the Mann-Whitney statistic) asks "pick a
random P1 and a random NO -- how often is the P1 denser?", which is exactly
the ranking question and is immune to the skew.

MEASUREMENT NOISE IS NOT ZERO: at k=20 a document's density still carries
~51% relative standard error, which inflates the apparent WITHIN-class
spread. Class medians are far better determined than the spread, because
noise averages out across ~75 documents per class. Read the medians and AUC
with more confidence than the quartile widths.

Usage:
    python3 analyze_priority_density.py --infile density_validation_v3.json

Output:
    priority_density_summary.csv  -- per-document rows
    Analysis printed to stdout.
"""

import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent

NAME_BEARING = {"register_table", "name_list", "form", "index"}
CLASSES = ["P1", "P2", "NO"]


def doc_metrics(doc):
    pages = doc["pages"]
    named = [p["named_individuals"] for p in pages
             if p.get("named_individuals") is not None]
    entries = [p["filled_entries"] for p in pages
               if p.get("filled_entries") is not None]
    types = [p["page_type"] for p in pages]
    n = len(named)
    if not n:
        return None
    density = statistics.mean(named)
    frac_name_pages = sum(1 for t in types if t in NAME_BEARING) / len(types)
    # Two-stage estimator: P(name-bearing page) x E[names | name-bearing].
    # Less sensitive to the model's 1-3 false positives on empty pages than
    # the raw mean, because an empty page contributes 0 to the proportion
    # rather than a spurious count.
    nb = [p["named_individuals"] for p in pages
          if p["page_type"] in NAME_BEARING and p.get("named_individuals") is not None]
    two_stage = frac_name_pages * (statistics.mean(nb) if nb else 0.0)
    return {
        "n_pages": n,
        "density": density,
        "density_two_stage": two_stage,
        "median_page": statistics.median(named),
        "max_page": max(named),
        "frac_name_pages": frac_name_pages,
        "mean_entries": statistics.mean(entries) if entries else 0.0,
        "expected_total": density * doc["page_count"],
        "zero_pages": sum(1 for v in named if v == 0) / n,
    }


def auc(pos, neg):
    """P(random pos > random neg), ties counted as half. Mann-Whitney U / mn."""
    if not pos or not neg:
        return float("nan")
    wins = 0.0
    for a in pos:
        for b in neg:
            wins += 1.0 if a > b else (0.5 if a == b else 0.0)
    return wins / (len(pos) * len(neg))


def quart(v):
    if not v:
        return (0, 0, 0)
    s = sorted(v)
    def q(p):
        i = p * (len(s) - 1)
        lo, hi = int(i), min(int(i) + 1, len(s) - 1)
        return s[lo] + (s[hi] - s[lo]) * (i - lo)
    return q(0.25), q(0.5), q(0.75)


def report(rows, field, title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)
    by = defaultdict(list)
    for r in rows:
        if r["priority"] in CLASSES:
            by[r["priority"]].append(r[field])
    print(f"  {'class':<6}{'n':>5}{'p25':>9}{'median':>9}{'p75':>9}{'mean':>9}{'max':>9}")
    for c in CLASSES:
        v = by.get(c, [])
        if not v:
            continue
        p25, med, p75 = quart(v)
        print(f"  {c:<6}{len(v):>5}{p25:>9.2f}{med:>9.2f}{p75:>9.2f}"
              f"{statistics.mean(v):>9.2f}{max(v):>9.2f}")
    p1, p2, no = by.get("P1", []), by.get("P2", []), by.get("NO", [])
    print(f"\n  AUC P1 vs NO: {auc(p1, no):.3f}   P1 vs P2: {auc(p1, p2):.3f}   "
          f"P2 vs NO: {auc(p2, no):.3f}")
    print("  (0.50 = no separation, 1.00 = perfect. >0.65 is a usable signal.)")
    return by


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--infile", default="density_validation_v3.json")
    args = ap.parse_args()

    path = HERE / args.infile
    if not path.exists():
        raise SystemExit(f"{path} not found -- run the density extraction first.")
    data = json.loads(path.read_text(encoding="utf-8"))

    rows = []
    for did, doc in data.items():
        m = doc_metrics(doc)
        if not m:
            continue
        rows.append({
            "id": did, "label": doc["label"], "root_label": doc["root_label"],
            "doc_type": ",".join(doc.get("doc_type") or []),
            "page_count": doc["page_count"],
            "priority": doc.get("priority_target"), **m,
        })

    if not rows:
        raise SystemExit("no scored documents found")

    out = HERE / "priority_density_summary.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    n_pages = sum(r["n_pages"] for r in rows)
    print(f"{len(rows)} documents, {n_pages} pages scored "
          f"({n_pages/len(rows):.1f} pages/doc)")
    zp = statistics.mean([r["zero_pages"] for r in rows])
    print(f"mean fraction of sampled pages with zero people: {zp:.2f}")

    report(rows, "density", "1. DENSITY (people per page) BY PRIORITY")
    report(rows, "density_two_stage",
           "2. TWO-STAGE DENSITY  = P(name page) x E[names | name page]")
    report(rows, "frac_name_pages",
           "3. FRACTION OF PAGES THAT ARE NAME-BEARING (most robust signal)")
    report(rows, "expected_total",
           "4. EXPECTED TOTAL NAMES (density x page_count) -- the yield estimate")

    # cost-benefit vs priority: where do they disagree?
    print("\n" + "=" * 72)
    print("5. DISAGREEMENTS -- where yield-per-page departs from human priority")
    print("=" * 72)
    ranked = sorted(rows, key=lambda r: -r["density"])
    q = max(1, len(ranked) // 4)
    top, bot = ranked[:q], ranked[-q:]
    up = [r for r in top if r["priority"] == "NO"]
    down = [r for r in bot if r["priority"] == "P1"]
    print(f"  Top-quartile density but labelled NO: {len(up)}/{len(top)}")
    for r in sorted(up, key=lambda r: -r["expected_total"])[:8]:
        print(f"    {r['label']:<26} {r['doc_type']:<6} {r['density']:>6.1f}/pg "
              f"x {r['page_count']:>5}pp = {r['expected_total']:>8.0f} est. names")
    print(f"  Bottom-quartile density but labelled P1: {len(down)}/{len(bot)}")
    for r in sorted(down, key=lambda r: -r["page_count"])[:8]:
        print(f"    {r['label']:<26} {r['doc_type']:<6} {r['density']:>6.1f}/pg "
              f"x {r['page_count']:>5}pp = {r['expected_total']:>8.0f} est. names")

    print("\n" + "=" * 72)
    print("6. DENSITY BY doc_type (cross-check -- hand counts suggested doc_type")
    print("   does NOT predict density well; L averaged 0.0 people/page)")
    print("=" * 72)
    by_dt = defaultdict(list)
    for r in rows:
        for dt in (r["doc_type"].split(",") if r["doc_type"] else []):
            if dt:
                by_dt[dt].append(r["density"])
    for dt in sorted(by_dt, key=lambda d: -statistics.median(by_dt[d])):
        v = by_dt[dt]
        print(f"  {dt:<6} n={len(v):>4}  median={statistics.median(v):>6.2f}  "
              f"mean={statistics.mean(v):>6.2f}")

    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
