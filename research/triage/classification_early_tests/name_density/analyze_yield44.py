#!/usr/bin/env python3
"""
Correlate qwen-vl density estimates against ACTUAL extracted record counts
for the 44 rendered documents that appear in the team's extraction report.

This is the payoff of the whole density thread: real ground-truth yield, not
a proxy. The earlier minicpm run scored Spearman rho ~0.87 on 29 docs; this
uses the better vision model (qwen2.5-vl, which also actually sees Hebrew and
counts more accurately) over all 44.

The headline is SPEARMAN rho between predicted and actual -- ranking quality
is what a cost-benefit sort needs; absolute calibration is secondary and
reported separately as the records-per-name ratio.

Usage:
    python3 analyze_yield44.py --infile density_yield44.json
"""
import argparse
import json
import statistics
from pathlib import Path

HERE = Path(__file__).parent


def spearman(xs, ys):
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = rank(xs), rank(ys)
    n = len(xs)
    d2 = sum((rx[i] - ry[i]) ** 2 for i in range(n))
    return 1 - 6 * d2 / (n * (n * n - 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--infile", default="density_yield44.json")
    ap.add_argument("--truth", default="eval_data_yield44.json")
    args = ap.parse_args()

    truth = {str(r["id"]): r for r in
             json.loads((HERE / args.truth).read_text())["validation"]}
    data = json.loads((HERE / args.infile).read_text())

    rows = []
    for did, doc in data.items():
        t = truth.get(did)
        if not t:
            continue
        vals = [p["named_individuals"] for p in doc["pages"]
                if p.get("named_individuals") is not None]
        if not vals:
            continue
        density = statistics.mean(vals)
        rows.append({
            "label": doc["label"],
            "density": density,
            "est_total": density * doc["page_count"],
            "actual": t["actual_records"],
            "pages": len(doc["pages"]),
            "page_count": doc["page_count"],
            "n_heb": sum(1 for p in doc["pages"] if p.get("hebrew_script")),
        })
    if len(rows) < 5:
        raise SystemExit(f"only {len(rows)} scored -- need more")

    dens = [r["density"] for r in rows]
    est = [r["est_total"] for r in rows]
    act = [r["actual"] for r in rows]

    print(f"{len(rows)} documents with density estimate AND actual record count\n")
    print(f"Spearman rho, density/page  vs actual records: {spearman(dens, act):.3f}")
    print(f"Spearman rho, est_total      vs actual records: {spearman(est, act):.3f}")
    print("  (ranking quality; >0.7 strong. est_total = density x page_count.)")

    # calibration: actual records vs estimated names. A vital record names
    # ~3 people but is 1 record, so expect actual < est_total.
    ratios = [r["actual"] / r["est_total"] for r in rows if r["est_total"] > 0]
    print(f"\nrecords-per-estimated-name ratio: median={statistics.median(ratios):.3f}")
    print("  (well below 1.0 is expected: a vital record names ~3 people but "
          "counts as\n   one record, and the model over-counts names.)")

    print("\nRanked by predicted yield (est_total) vs actual:")
    print(f"  {'label':<22}{'density':>8}{'est_total':>10}{'ACTUAL':>8}{'heb':>5}")
    for r in sorted(rows, key=lambda r: -r["est_total"]):
        print(f"  {r['label']:<22}{r['density']:>8.1f}{r['est_total']:>10.0f}"
              f"{r['actual']:>8.0f}{r['n_heb']:>5}")

    # top-k precision: of the top 10 predicted, how many are top 10 actual?
    by_est = [r["label"] for r in sorted(rows, key=lambda r: -r["est_total"])]
    by_act = [r["label"] for r in sorted(rows, key=lambda r: -r["actual"])]
    for k in (5, 10):
        if len(rows) >= k:
            overlap = len(set(by_est[:k]) & set(by_act[:k]))
            print(f"\ntop-{k} overlap (predicted vs actual): {overlap}/{k}")


if __name__ == "__main__":
    main()
