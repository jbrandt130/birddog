#!/usr/bin/env python3
"""
Score a Hebrew-detection run against known ground truth.

This is a MODEL CAPABILITY test, not a signal-value test. minicpm-v4.5
returned hebrew_script=false on every page of documents that visibly,
abundantly contain Hebrew (7307 is a fully-Hebrew divorce register; 7156 a
bilingual birth register) -- confirmed by eye. The question here is narrow:
does the model under test actually SEE Hebrew when it is unmistakably present?

The eval pairs 7 Hebrew-bearing Jewish metric books (expected_hebrew=yes)
against 6 Cyrillic-only estate/census/church documents (expected_hebrew=no).
A model that can read the page should light up on nearly all the former and
none of the latter. minicpm's score here is ~0/7 -- run this on qwen2.5-vl
to see whether ANY available model clears the bar.

Usage:
    python3 analyze_hebtest.py --infile density_hebtest_qwenvl.json
"""
import argparse
import json
from pathlib import Path

HERE = Path(__file__).parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--infile", required=True)
    ap.add_argument("--truth", default="eval_data_hebtest.json")
    args = ap.parse_args()

    truth = {str(r["id"]): r["expected_hebrew"]
             for r in json.loads((HERE / args.truth).read_text())["validation"]}
    data = json.loads((HERE / args.infile).read_text())

    print(f"{'doc':<22}{'expect':>8}{'heb pages':>11}{'result':>9}")
    tp = fp = tn = fn = 0
    for did, doc in data.items():
        exp = truth.get(did)
        if exp is None:
            continue
        pages = doc["pages"]
        n_heb = sum(1 for p in pages if p.get("hebrew_script"))
        fired = n_heb > 0
        if exp == "yes":
            res = "OK" if fired else "MISS"
            tp += fired
            fn += not fired
        else:
            res = "OK" if not fired else "FALSE+"
            tn += not fired
            fp += fired
        print(f"{doc['label']:<22}{exp:>8}{n_heb:>5}/{len(pages):<5}{res:>9}")

    n_yes = tp + fn
    n_no = tn + fp
    print("\n" + "=" * 48)
    print(f"Hebrew-bearing docs detected: {tp}/{n_yes} "
          f"({100*tp/n_yes:.0f}% recall)" if n_yes else "no positive cases")
    print(f"Control docs kept clean:      {tn}/{n_no} "
          f"({100*tn/n_no:.0f}% specificity)" if n_no else "no controls")
    print("=" * 48)
    if n_yes and tp / n_yes >= 0.7 and (not n_no or fp / n_no <= 0.2):
        print("VERDICT: this model CAN see Hebrew. Stage 3 is viable -- Hebrew")
        print("  detection catches pre-1917 Jewish books regardless of metadata.")
    elif n_yes and tp / n_yes >= 0.7:
        print("VERDICT: detects Hebrew but also false-positives on Cyrillic --")
        print("  tighten the prompt before trusting it.")
    else:
        print("VERDICT: this model also cannot reliably see Hebrew. If it was")
        print("  the last candidate, stage 3 is dead; rely on stages 1-2.")


if __name__ == "__main__":
    main()
