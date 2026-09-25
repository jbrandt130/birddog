#!/usr/bin/env python3
"""
Aggregate per-page density estimates to per-document scores, and validate
them INDIRECTLY against signals we already trust.

There are no hand-counted name densities yet, so nothing here is a scored
accuracy number. Instead this answers three questions that together decide
whether the density approach is worth pursuing:

  1. DOES IT SEPARATE PRIORITY? Human P1/P2/NO labels are a proxy for
     genealogical value. If estimated density doesn't separate them at all,
     the estimator is measuring noise. (It should NOT separate them
     perfectly either -- see #3.)
  2. DOES IT TRACK DOC_TYPE? doc_type L is "list of names" by definition
     and V is vital records; both should show markedly higher density than
     O (other). This is the closest thing to a free ground-truth check,
     since doc_type was assigned by humans reading the actual records.
  3. WHERE DOES IT DISAGREE WITH PRIORITY? This is the payoff, not a
     defect. Ranking by value/cost is not the same as ranking by priority:
     a 400-page case file with one 30-name list is P1-ish by human triage
     but poor yield per page, while a 20-page dense list is excellent yield
     and may sit at P2. Those disagreements are what a cost-benefit
     ranking adds over replicating the existing labels.

METRICS PER DOCUMENT:
    mean_names_per_page   - the density estimate itself
    stdev_names_per_page  - WITHIN-document variance. Low variance on a
                            dense doc => uniform register, yield really
                            does scale with page_count. High variance =>
                            one list buried in a case file, so
                            expected_total is an overestimate. This is why
                            the sampler draws k=5 rather than 1.
    expected_total_names  = mean * page_count   (genealogical yield)
    yield_per_page        = mean                (value per unit extraction
                            cost, since cost is ~linear in page_count --
                            note this is just the density, which is the
                            point: value/cost IS density)
    frac_name_pages       - share of sampled pages that are register/list

Usage:
    python3 analyze_density.py [--split validation]

Output:
    density_<split>_summary.csv  -- one row per document
    Analysis printed to stdout.
"""

import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent

NAME_BEARING_TYPES = {"register_table", "name_list", "form", "index"}
RANK = {"P1": 1, "P2": 2, "NO": 3}


def summarize_doc(doc):
    # named_individuals is the value metric: every person named on the image,
    # so a birth row naming child + father + mother contributes 3 searchable
    # people, not 1. filled_entries (one subject person per record row) is
    # tracked alongside it as a fill-rate / cross-check signal.
    counts = [p["named_individuals"] for p in doc["pages"]
              if p.get("named_individuals") is not None]
    entries = [p["filled_entries"] for p in doc["pages"]
               if p.get("filled_entries") is not None]
    types = [p["page_type"] for p in doc["pages"]]
    n_scored = len(counts)
    mean = statistics.mean(counts) if counts else 0.0
    mean_entries = statistics.mean(entries) if entries else 0.0
    stdev = statistics.stdev(counts) if len(counts) > 1 else 0.0
    frac_name_pages = (sum(1 for t in types if t in NAME_BEARING_TYPES) / len(types)
                       if types else 0.0)
    return {
        "n_pages_scored": n_scored,
        "n_parse_errors": sum(1 for t in types if t == "parse_error"),
        "mean_names_per_page": round(mean, 2),
        "mean_entries_per_page": round(mean_entries, 2),
        "names_per_entry": round(mean / mean_entries, 2) if mean_entries else None,
        "median_names_per_page": round(statistics.median(counts), 2) if counts else 0.0,
        "stdev_names_per_page": round(stdev, 2),
        "max_names_per_page": max(counts) if counts else 0,
        "frac_name_pages": round(frac_name_pages, 2),
        "expected_total_names": round(mean * doc["page_count"]),
        "yield_per_page": round(mean, 2),
        "page_types": ",".join(sorted(set(types))),
    }


def fmt_group(name, values):
    if not values:
        return f"  {name:<18} n=0"
    return (f"  {name:<18} n={len(values):>4}  median={statistics.median(values):>7.1f}  "
            f"mean={statistics.mean(values):>7.1f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["train", "validation"],
                         default="validation")
    parser.add_argument("--infile", default=None)
    args = parser.parse_args()

    in_path = HERE / (args.infile or f"density_{args.split}.json")
    if not in_path.exists():
        raise SystemExit(f"{in_path} not found -- run extract_density.py first.")
    data = json.loads(in_path.read_text(encoding="utf-8"))

    rows = []
    for doc_id, doc in data.items():
        row = {
            "id": doc_id,
            "label": doc["label"],
            "root_label": doc["root_label"],
            "doc_type": ",".join(doc.get("doc_type") or []),
            "page_count": doc["page_count"],
            "priority_target": doc.get("priority_target"),
        }
        row.update(summarize_doc(doc))
        rows.append(row)

    if not rows:
        raise SystemExit("no documents in the density file")

    out_path = HERE / f"density_{args.split}_summary.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    n_pages = sum(r["n_pages_scored"] for r in rows)
    n_err = sum(r["n_parse_errors"] for r in rows)
    print(f"{len(rows)} documents, {n_pages} pages scored"
          + (f", {n_err} parse errors" if n_err else ""))

    # ---- 1. separation by human priority label ----
    print("\n" + "=" * 68)
    print("1. DENSITY BY PRIORITY LABEL (does it separate what humans valued?)")
    print("=" * 68)
    by_pri = defaultdict(list)
    for r in rows:
        if r["priority_target"]:
            by_pri[r["priority_target"]].append(r["mean_names_per_page"])
    for lab in ("P1", "P2", "NO"):
        print(fmt_group(lab, by_pri.get(lab, [])))
    print("  (expect P1 > P2 > NO. Perfect separation is NOT the goal -- see #3.)")

    # ---- 2. tracking doc_type ----
    print("\n" + "=" * 68)
    print("2. DENSITY BY DOC_TYPE (L=name list, V=vital record should be highest)")
    print("=" * 68)
    by_dt = defaultdict(list)
    for r in rows:
        for dt in (r["doc_type"].split(",") if r["doc_type"] else []):
            if dt:
                by_dt[dt].append(r["mean_names_per_page"])
    for dt in sorted(by_dt, key=lambda d: -statistics.median(by_dt[d])):
        print(fmt_group(dt, by_dt[dt]))

    # ---- page-type mix, a sanity check on the estimator itself ----
    print("\n" + "=" * 68)
    print("PAGE-TYPE MIX (sanity check: are middle pages mostly name-bearing?)")
    print("=" * 68)
    type_counts = defaultdict(int)
    for doc in data.values():
        for p in doc["pages"]:
            type_counts[p["page_type"]] += 1
    tot = sum(type_counts.values())
    for t, c in sorted(type_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {t:<20} {c:>5} ({100*c/tot:>5.1f}%)")

    # ---- 3. where cost-benefit disagrees with priority ----
    print("\n" + "=" * 68)
    print("3. COST-BENEFIT vs PRIORITY DISAGREEMENTS (the payoff)")
    print("=" * 68)
    ranked = sorted(rows, key=lambda r: -r["yield_per_page"])
    top_q = ranked[: max(1, len(ranked) // 4)]
    bot_q = ranked[-max(1, len(ranked) // 4):]
    up = [r for r in top_q if r["priority_target"] == "NO"]
    down = [r for r in bot_q if r["priority_target"] == "P1"]
    print(f"  Top-quartile density but labelled NO: {len(up)}")
    for r in up[:5]:
        print(f"    {r['label']:<26} {r['mean_names_per_page']:>6.1f} names/pg  "
              f"{r['page_count']:>5}pp  est_total={r['expected_total_names']}")
    print(f"  Bottom-quartile density but labelled P1: {len(down)}")
    for r in down[:5]:
        print(f"    {r['label']:<26} {r['mean_names_per_page']:>6.1f} names/pg  "
              f"{r['page_count']:>5}pp  est_total={r['expected_total_names']}")
    print("\n  These are the records where ranking by yield-per-page departs from")
    print("  human triage. Worth eyeballing a few before trusting either signal.")

    # ---- within-document variance ----
    print("\n" + "=" * 68)
    print("WITHIN-DOCUMENT VARIANCE (uniform register vs one list in a case file)")
    print("=" * 68)
    dense = [r for r in rows if r["mean_names_per_page"] >= 5]
    if dense:
        uniform = [r for r in dense
                   if r["stdev_names_per_page"] <= 0.5 * r["mean_names_per_page"]]
        print(f"  documents with mean >=5 names/page: {len(dense)}")
        print(f"    of those, LOW variance (uniform register, yield scales with "
              f"page_count): {len(uniform)}")
        print(f"    HIGH variance (likely one list inside a larger file, "
              f"expected_total is optimistic): {len(dense) - len(uniform)}")
    else:
        print("  no documents with mean >=5 names/page")

    print(f"\nWrote {out_path}")
    print("Top 10 by expected total names (yield), for a quick eyeball:")
    for r in sorted(rows, key=lambda r: -r["expected_total_names"])[:10]:
        print(f"  {r['label']:<26} {r['doc_type']:<6} {r['priority_target'] or '?':<3} "
              f"{r['mean_names_per_page']:>6.1f}/pg x {r['page_count']:>5}pp "
              f"= {r['expected_total_names']:>7}")


if __name__ == "__main__":
    main()
