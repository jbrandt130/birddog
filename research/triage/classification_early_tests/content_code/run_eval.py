#!/usr/bin/env python3
"""
Batch eval for the content_code classifier (J / M / U / N) against a local
Ollama model, from METADATA ONLY.

WHAT content_code IS: a human-assigned scale of how likely a document is to
contain information about Jewish people --
    J = Contains Jewish information
    M = Might contain Jewish information
    U = Unlikely to have Jewish information
    N = Does not contain Jewish information
(A fifth code L, "Likely", exists in the schema but has 1 record in train
and 0 in validation, so it is excluded.)

WHY IT MATTERS: ../name_density/ established that estimated names-per-page
separates P1 from P2 (AUC 0.84) but is INVERTED for P2 vs NO (AUC 0.29) --
do-not-acquire documents are DENSER than P2 ones, because density counts
people while project value counts JEWISH people. The densest NO documents
were Greek Catholic parish lists and general population registers. So:

    value/cost  ~=  P(Jewish content)  x  names_per_page
                    └── THIS TASK ──┘     └── density ──┘

Measured on these same 284 documents, content_code nearly determines
process_code: U->NO 25/25, N->NO 68/68, M->P2 7/7, J->P1 or P2 (only 2 NO).
So this task is close to the "acquire vs do-not-acquire" decision itself.

ORDINAL, NOT NOMINAL. The four codes are ranked, so an M-for-J error is
much smaller than an N-for-J error. Scoring therefore reports, alongside
plain accuracy: MACRO recall (the test set is naturally imbalanced --
J is 66%, so always-J scores 65.6% and raw accuracy is nearly meaningless),
mean absolute ordinal error, and AUC for J-vs-rest using the 0..1 score.
The score mapping (J=1.0, M=0.67, U=0.33, N=0.0) is what feeds the
cost-benefit multiplier downstream, so it is reported as a first-class
output rather than derived later.

ABSTENTION: UNCLEAR is accepted as a non-answer, consistent with the other
tasks in this project, and scored as a non-commitment rather than a wrong
guess.

Usage:
    python3 run_eval.py [--model qwen2.5:14b] [--host http://localhost:11434]

Outputs:
    results.csv -- id, label, true_code, predicted, score, correct,
                   abstained, ordinal_error, raw_response
"""

import argparse
import csv
import json
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

VALID_LABELS = ["J", "M", "U", "N"]
ABSTAIN = "UNCLEAR"
# Ordinal position, used for "how far off" scoring.
RANK = {"J": 0, "M": 1, "U": 2, "N": 3}
# 0..1 Jewish-content score -- the P(Jewish) factor for cost-benefit ranking.
SCORE = {"J": 1.0, "M": 0.67, "U": 0.33, "N": 0.0}

DEFAULT_NUM_CTX = 8192
DEFAULT_TIMEOUT = 300


def build_system_message(label_definitions):
    lines = [
        "You are classifying archival documents from a Jewish genealogical research "
        "database by how likely they are to contain information about JEWISH PEOPLE "
        "specifically. Assign exactly ONE of the following codes.",
        "",
    ]
    for code, desc in label_definitions.items():
        lines.append(f"- {code}: {desc}")
    lines.append("")
    lines.append(
        "For each document you are given: its archival label (archive/fond/opus/case), "
        "the archive code (root_label), its hierarchy level, its doc_type classification "
        "(V=vital record, C=census, L=list of names, O=other, P=passport application), "
        "an English description of the content, a native-language description if "
        "available, a date/year range if recorded, and a page count."
    )
    lines.append("")
    lines.append(
        "THE KEY DISTINCTION -- and the one this task exists to test -- is between "
        "'this document contains many people' and 'this document contains JEWISH "
        "people'. They are NOT the same. A parish register of a Greek Catholic or "
        "Orthodox church, a household census of a Cossack regiment, or a general "
        "population list of a village can be densely packed with personal names and "
        "still contain no Jewish information at all. Judge the population the record "
        "is ABOUT, not how many names it holds."
    )
    lines.append("")
    lines.append("Signals that point to J (contains Jewish information):")
    lines.append(
        "  - explicit mention of Jews, Jewish communities, synagogues, rabbis, "
        "Hebrew/Yiddish, or a Jewish religious body"
    )
    lines.append(
        "  - metrical/vital record books kept BY a Jewish community, or civil "
        "registry (ZAGS) books from areas and periods with substantial Jewish "
        "populations"
    )
    lines.append(
        "  - lists tied to Jewish-specific events or categories: pogrom victims, "
        "Holocaust records, Jewish emigration, Jewish craftsmen or merchant guilds"
    )
    lines.append("")
    lines.append("Signals that point to N (does not contain Jewish information):")
    lines.append(
        "  - records of a Christian religious institution -- church, parish, "
        "monastery, priest, deanery, consistory -- with no Jewish connection"
    )
    lines.append(
        "  - administrative, estate, property, land-survey or bookkeeping material "
        "with no personal-name content about Jews"
    )
    lines.append(
        "  - personnel files, membership lists, or institutional records of a body "
        "with no Jewish association"
    )
    lines.append("")
    lines.append(
        "U (unlikely) vs N (does not): use N when the subject matter positively "
        "excludes Jewish content -- a named church, a named non-Jewish individual's "
        "personal file. Use U when the record is simply about a general or "
        "non-Jewish population and Jewish content would be incidental at best, such "
        "as a Cossack regimental household census or a peasant ownership dispute."
    )
    lines.append("")
    lines.append(
        "M (might) is for genuinely mixed or uncertain cases: a general population "
        "record from a town KNOWN to have had a large Jewish community, an "
        "occupational or institutional list from a region where Jews were heavily "
        "represented in that occupation, or a record whose description is too "
        "generic to tell but whose archive and period make Jewish content plausible. "
        "M is rare -- do not use it merely to hedge. If the evidence points one way, "
        "commit to that code."
    )
    lines.append("")
    lines.append(
        "Some records genuinely do not give you enough evidence to judge. In those "
        "cases respond with the single word UNCLEAR instead of guessing."
    )
    lines.append("")
    lines.append(
        "Respond with exactly one token: one of J, M, U, N, or UNCLEAR. No "
        "explanation, no additional labels, no punctuation."
    )
    return "\n".join(lines)


def format_record(rec):
    years = rec.get("years") or "not recorded"
    native = rec.get("page_native_description") or "(none)"
    doc_type = ", ".join(rec.get("doc_type") or []) or "not recorded"
    pc = rec.get("page_count")
    return (
        f"Archive (root_label): {rec['root_label']}\n"
        f"Label: {rec['label']}\n"
        f"Level: {rec.get('level') or 'not recorded'}\n"
        f"doc_type: {doc_type}\n"
        f"Page count: {pc if pc is not None else 'not recorded'}\n"
        f"Years: {years}\n"
        f"Description: {rec['page_description']}\n"
        f"Native description: {native}\n"
        f"content_code:"
    )


def build_fixed_messages(data):
    messages = [{"role": "system",
                 "content": build_system_message(data["label_definitions"])}]
    for ex in data["few_shot"]:
        messages.append({"role": "user", "content": format_record(ex)})
        messages.append({"role": "assistant", "content": ex["target"]})
    return messages


def build_fixed_prompt_prefix(data):
    parts = [format_record(ex) + " " + ex["target"] for ex in data["few_shot"]]
    return "\n\n".join(parts) + "\n\n"


def parse_prediction(raw_text):
    text = raw_text.strip().upper()
    if re.search(r"\bUNCLEAR\b", text):
        return ABSTAIN
    for tok in re.split(r"[,\s/;.]+", text):
        if tok.strip() in VALID_LABELS:
            return tok.strip()
    return ABSTAIN


def call_ollama(host, model, messages, num_ctx=None, timeout=300):
    url = f"{host.rstrip('/')}/api/chat"
    options = {"temperature": 0.1}
    if num_ctx:
        options["num_ctx"] = num_ctx
    payload = {"model": model, "messages": messages, "stream": False,
               "options": options}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))["message"]["content"]
    except urllib.error.HTTPError as e:
        print(f"ERROR HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}",
              file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"ERROR calling Ollama at {url}: {e}", file=sys.stderr)
        sys.exit(1)


def call_ollama_generate(host, model, system, prompt, num_ctx=None, timeout=300):
    url = f"{host.rstrip('/')}/api/generate"
    options = {"temperature": 0.1}
    if num_ctx:
        options["num_ctx"] = num_ctx
    payload = {"model": model, "system": system, "prompt": prompt,
               "stream": False, "options": options}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))["response"]
    except urllib.error.HTTPError as e:
        print(f"ERROR HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}",
              file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"ERROR calling Ollama at {url}: {e}", file=sys.stderr)
        sys.exit(1)


CSV_FIELDNAMES = ["id", "label", "root_label", "description", "doc_type",
                  "true_code", "predicted", "score", "correct", "abstained",
                  "ordinal_error", "raw_response"]


def load_existing_results(out_path):
    existing = {}
    if out_path.exists():
        with open(out_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing[int(row["id"])] = row
    return existing


def auc(pos, neg):
    if not pos or not neg:
        return float("nan")
    wins = sum(1.0 if a > b else (0.5 if a == b else 0.0) for a in pos for b in neg)
    return wins / (len(pos) * len(neg))


def summarize(rows):
    n = len(rows)
    committed = [r for r in rows if str(r["abstained"]) != "True"]
    correct = sum(1 for r in rows if str(r["correct"]) == "True")

    support = defaultdict(int)
    hit = defaultdict(int)
    confusion = defaultdict(lambda: defaultdict(int))
    for r in rows:
        t = r["true_code"]
        support[t] += 1
        if str(r["correct"]) == "True":
            hit[t] += 1
        confusion[t][r["predicted"]] += 1

    print("\n" + "=" * 72)
    print(f"Completeness (committed / total):   {len(committed)}/{n} "
          f"({100*len(committed)/n:.1f}%)")
    print(f"Accuracy (correct / total):         {correct}/{n} ({100*correct/n:.1f}%)")

    recalls = [hit[c] / support[c] for c in VALID_LABELS if support[c]]
    print(f"MACRO recall (unweighted mean over classes): {100*sum(recalls)/len(recalls):.1f}%")
    print("  ^ the headline number: the test set is naturally imbalanced "
          "(J is 66%),\n    so plain accuracy rewards always answering J.")

    print("\nPer-class recall:")
    print(f"  {'code':<6}{'support':>9}{'hit':>6}{'recall':>9}")
    for c in VALID_LABELS:
        if support[c]:
            print(f"  {c:<6}{support[c]:>9}{hit[c]:>6}{100*hit[c]/support[c]:>8.1f}%")

    print("\nConfusion (rows = true, cols = predicted):")
    cols = VALID_LABELS + [ABSTAIN]
    print("        " + "".join(f"{c:>9}" for c in cols))
    for t in VALID_LABELS:
        if support[t]:
            print(f"  {t:<6}" + "".join(f"{confusion[t][c]:>9}" for c in cols))

    ord_errs = [abs(int(r["ordinal_error"])) for r in rows
                if r["ordinal_error"] not in ("", "None", None)]
    if ord_errs:
        print(f"\nMean absolute ORDINAL error: {sum(ord_errs)/len(ord_errs):.2f} "
              f"steps (0=exact, 3=J vs N)")
        print(f"  off by 0: {sum(1 for e in ord_errs if e==0)}, "
              f"1: {sum(1 for e in ord_errs if e==1)}, "
              f"2: {sum(1 for e in ord_errs if e==2)}, "
              f"3: {sum(1 for e in ord_errs if e==3)}")

    # J-vs-rest ranking quality using the 0..1 score -- this is the number
    # that matters for the cost-benefit multiplier, which needs a usable
    # ordering rather than exact labels.
    pos = [float(r["score"]) for r in rows if r["true_code"] == "J"
           and r["score"] not in ("", None)]
    neg = [float(r["score"]) for r in rows if r["true_code"] != "J"
           and r["score"] not in ("", None)]
    print(f"\nAUC, J vs rest, using the 0..1 score: {auc(pos, neg):.3f}")
    print("  (this is the P(Jewish) factor's ranking quality -- what the "
          "cost-benefit\n   score actually consumes. 0.50 = useless, 1.00 = perfect.)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="qwen2.5:14b")
    p.add_argument("--host", default="http://localhost:11434")
    p.add_argument("--data", default="eval_data.json")
    p.add_argument("--out", default="results.csv")
    p.add_argument("--api", choices=["chat", "generate"], default="chat")
    p.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX)
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--resume", action="store_true", default=False)
    p.add_argument("--limit", type=int, default=None,
                   help="stride-sampled across the full set, not the first N")
    args = p.parse_args()

    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    test_cases = data["test_cases"]
    if args.limit and args.limit < len(test_cases):
        stride = len(test_cases) / args.limit
        test_cases = [test_cases[int(i * stride)] for i in range(args.limit)]
    n = len(test_cases)

    if args.api == "chat":
        fixed_messages = build_fixed_messages(data)
    else:
        system_text = build_system_message(data["label_definitions"])
        fixed_prefix = build_fixed_prompt_prefix(data)

    out_path = Path(args.out)
    existing = load_existing_results(out_path) if args.resume else {}
    if existing:
        print(f"--resume: {len(existing)} already done, skipping those.\n")
    remaining = [r for r in test_cases if r["id"] not in existing]
    print(f"Running {len(remaining)} test cases ({n-len(remaining)} done) "
          f"against {args.model} at {args.host} (--api {args.api}) ...\n")

    write_header = not (args.resume and out_path.exists())
    f = open(out_path, "a" if args.resume and out_path.exists() else "w",
             newline="", encoding="utf-8")
    writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
    if write_header:
        writer.writeheader()

    try:
        for i, rec in enumerate(remaining, 1):
            if args.api == "chat":
                msgs = fixed_messages + [{"role": "user", "content": format_record(rec)}]
                raw = call_ollama(args.host, args.model, msgs,
                                  num_ctx=args.num_ctx, timeout=args.timeout)
            else:
                raw = call_ollama_generate(args.host, args.model, system_text,
                                           fixed_prefix + format_record(rec),
                                           num_ctx=args.num_ctx, timeout=args.timeout)
            pred = parse_prediction(raw)
            truth = rec["target"]
            abstained = pred == ABSTAIN
            correct = (not abstained) and pred == truth
            ord_err = None if abstained else RANK[pred] - RANK[truth]
            score = None if abstained else SCORE[pred]

            row = {"id": rec["id"], "label": rec["label"],
                   "root_label": rec["root_label"],
                   "description": rec.get("page_description"),
                   "doc_type": ",".join(rec.get("doc_type") or []),
                   "true_code": truth, "predicted": pred,
                   "score": "" if score is None else score,
                   "correct": correct, "abstained": abstained,
                   "ordinal_error": "" if ord_err is None else ord_err,
                   "raw_response": raw.strip().replace("\n", " ")}
            writer.writerow(row)
            f.flush()
            existing[rec["id"]] = {k: str(v) for k, v in row.items()}

            status = "OK  " if correct else ("ABST" if abstained else "MISS")
            print(f"[{i:>3}/{len(remaining)}] {status}  {rec['label']:<26} "
                  f"true={truth} pred={pred}")
    finally:
        f.close()
        print(f"\n{len(existing)}/{n} done, written to {out_path}")
        if len(existing) < n:
            print("Incomplete -- rerun with --resume.")
        summarize(list(existing.values()))


if __name__ == "__main__":
    main()
