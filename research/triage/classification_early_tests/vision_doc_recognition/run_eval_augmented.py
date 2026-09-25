#!/usr/bin/env python3
"""
Batch eval for the doc_type classifier using metadata TEXT plus MiniCPM-V
-extracted printed-text fragments, against a local Ollama text model (e.g.
qwen2.5:14b -- the same model ../doc_type/run_eval.py used, since this is a
pure text task with no images sent at inference time).

This is the third leg of the doc_type classification comparison:
  1. doc_type/run_eval.py            - metadata text only
  2. vision_doc_recognition/run_eval.py - page images only ("at a glance")
  3. this script                     - metadata text + OCR'd printed-text
                                        fragments (see extract_text.py)
The goal is to see whether printed-form text extracted from page images
(headers, field labels, stamps -- explicitly NOT handwriting, see
extract_text.py's prompt) adds real signal on top of the existing
human-written archive/label/description metadata, without the cost/latency
of sending raw images to a vision model at eval time.

Run build_augmented_eval_data.py first to produce eval_data_augmented.json.

Same single-label + abstain framing and scoring methodology as
doc_type/run_eval.py (see that file's docstring for the full rationale).

Usage:
    python3 run_eval_augmented.py [--model qwen2.5:14b] [--host http://localhost:11434]

Outputs:
    results_augmented.csv - one row per test record, written incrementally
                             (crash/timeout only costs the in-flight call,
                             not the whole run -- see --resume)
    Summary printed to stdout.
"""

import argparse
import csv
import json
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

HERE = Path(__file__).parent
VALID_LABELS = ["V", "C", "L", "O", "P"]
ABSTAIN = "UNCLEAR"

CSV_FIELDNAMES = ["id", "label", "root_label", "true_doc_type", "predicted",
                   "correct", "abstained", "raw_response"]


def build_system_message(label_definitions):
    lines = [
        "You are classifying archival documents from a Jewish genealogical research "
        "database. Assign exactly ONE of the following document types - the single "
        "type you are most confident best describes the record.",
        "",
    ]
    for code, desc in label_definitions.items():
        lines.append(f"- {code}: {desc}")
    lines.append("")
    lines.append(
        '(A sixth legacy value "M" exists in the data but is undocumented and should '
        "not be predicted.)"
    )
    lines.append("")
    lines.append(
        "For each document you are given: its archival label (archive/fond/opus/case), "
        "the archive code (root_label), its hierarchy level, an English description of "
        "the content, a native-language description if available, a date/year range if "
        "recorded, and -- when available -- printed text fragments transcribed from "
        "sampled page images (headers, form field labels, stamps, captions). That "
        "transcription is machine-generated, may be incomplete or contain errors, and "
        "deliberately excludes handwritten content, so treat it as supplementary "
        "evidence rather than authoritative -- weigh it alongside the description, not "
        "above it."
    )
    lines.append("")
    lines.append(
        "Some records genuinely fit more than one type, or don't give you enough "
        "evidence to be confident in a single type. In those cases, respond with the "
        "single word UNCLEAR instead of guessing. It is better to say UNCLEAR than to "
        "commit to a label you are not confident in."
    )
    lines.append("")
    lines.append(
        "Respond with exactly one token: one of V, C, L, O, P, or UNCLEAR. No "
        "explanation, no additional labels, no punctuation."
    )
    return "\n".join(lines)


def format_record(rec):
    years = rec.get("years") or "not recorded"
    native = rec.get("page_native_description") or "(none)"
    extracted = rec.get("vision_extracted_text")
    extracted_block = extracted if extracted else "(not available)"
    return (
        f"Archive (root_label): {rec['root_label']}\n"
        f"Label: {rec['label']}\n"
        f"Level: {rec['level']}\n"
        f"Years: {years}\n"
        f"Description: {rec['page_description']}\n"
        f"Native description: {native}\n"
        f"Extracted printed text (machine OCR, printed text only, may be incomplete):\n"
        f"{extracted_block}\n"
        f"doc_type:"
    )


def build_fixed_messages(data):
    messages = [
        {"role": "system", "content": build_system_message(data["label_definitions"])}
    ]
    for ex in data["few_shot"]:
        messages.append({"role": "user", "content": format_record(ex)})
        messages.append({"role": "assistant", "content": ex["target"]})
    return messages


def parse_prediction(raw_text):
    text = raw_text.strip().upper()
    if re.search(r"\bUNCLEAR\b", text):
        return ABSTAIN
    tokens = re.split(r"[,\s/;.]+", text)
    for tok in tokens:
        tok = tok.strip()
        if tok in VALID_LABELS:
            return tok
    return ABSTAIN


def call_ollama(host, model, messages):
    url = f"{host.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.1},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"ERROR calling Ollama at {url}: {e}", file=sys.stderr)
        print("Is `ollama serve` running, and is the model pulled?", file=sys.stderr)
        sys.exit(1)
    return body["message"]["content"]


def harmonic_mean(a, b):
    if a != a or b != b:
        return float("nan")
    if (a + b) == 0:
        return 0.0
    return 2 * a * b / (a + b)


def load_existing_results(out_path):
    existing = {}
    if out_path.exists():
        with open(out_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing[int(row["id"])] = row
    return existing


def summarize(rows):
    n = len(rows)
    committed = sum(1 for r in rows if str(r["abstained"]) != "True")
    correct_committed = sum(1 for r in rows if str(r["correct"]) == "True")

    label_hit = {l: 0 for l in VALID_LABELS}
    label_hit_other = {l: 0 for l in VALID_LABELS}
    label_abstain = {l: 0 for l in VALID_LABELS}
    label_wrong = {l: 0 for l in VALID_LABELS}
    label_support = {l: 0 for l in VALID_LABELS}

    for r in rows:
        truth = set(r["true_doc_type"].split(","))
        pred = r["predicted"]
        abstained = str(r["abstained"]) == "True"
        for l in truth:
            if l not in label_support:
                continue
            label_support[l] += 1
            if abstained:
                label_abstain[l] += 1
            elif pred == l:
                label_hit[l] += 1
            elif pred in truth:
                label_hit_other[l] += 1
            else:
                label_wrong[l] += 1

    return {
        "n": n, "committed": committed, "correct_committed": correct_committed,
        "label_hit": label_hit, "label_hit_other": label_hit_other,
        "label_abstain": label_abstain, "label_wrong": label_wrong,
        "label_support": label_support,
    }


def print_summary(s):
    n, committed, correct_committed = s["n"], s["committed"], s["correct_committed"]
    if n == 0:
        print("No results to summarize.")
        return
    completeness = committed / n
    accuracy_on_committed = correct_committed / committed if committed else float("nan")
    overall_yield = correct_committed / n
    balanced = harmonic_mean(accuracy_on_committed, completeness)

    print("\n" + "=" * 70)
    print(f"Completeness (committed / total):        {committed}/{n} ({100*completeness:.1f}%)")
    if committed:
        print(f"Accuracy on committed (correct / committed): "
              f"{correct_committed}/{committed} ({100*accuracy_on_committed:.1f}%)")
    else:
        print("Accuracy on committed: n/a (no commitments)")
    print(f"Overall yield (correct / total):          {correct_committed}/{n} ({100*overall_yield:.1f}%)")
    print(f"Balanced score (harmonic mean of the two): {balanced:.3f}")

    print("\nPer-true-label breakdown (rows are records whose TRUE set contains this label):")
    print(f"{'label':<6}{'support':>8}{'hit':>6}{'hit_other':>11}{'abstain':>9}{'wrong':>7}")
    for l in VALID_LABELS:
        sup = s["label_support"][l]
        if sup == 0:
            continue
        print(
            f"{l:<6}{sup:>8}{s['label_hit'][l]:>6}{s['label_hit_other'][l]:>11}"
            f"{s['label_abstain'][l]:>9}{s['label_wrong'][l]:>7}"
        )
    print(
        "\n(hit = predicted exactly this label; hit_other = predicted a different "
        "label that is also correct for a multi-label true set; abstain = said "
        "UNCLEAR; wrong = predicted a label not in the true set)"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen2.5:14b")
    parser.add_argument("--host", default="http://localhost:11434")
    parser.add_argument("--data", default="eval_data_augmented.json")
    parser.add_argument("--out", default="results_augmented.csv")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--resume", action="store_true", default=False)
    args = parser.parse_args()

    data = json.loads((HERE / args.data).read_text(encoding="utf-8"))
    fixed_messages = build_fixed_messages(data)
    test_cases = data["test_cases"]
    if args.limit:
        test_cases = test_cases[: args.limit]
    n = len(test_cases)

    out_path = HERE / args.out
    existing = load_existing_results(out_path) if args.resume else {}
    if existing:
        print(f"--resume: found {len(existing)} already-completed result(s), appending new ones.\n")
    remaining = [rec for rec in test_cases if rec["id"] not in existing]

    print(f"Running {len(remaining)} test cases ({n - len(remaining)} already done) "
          f"against {args.model} at {args.host} ...\n")

    file_mode = "a" if (args.resume and out_path.exists()) else "w"
    out_f = open(out_path, file_mode, newline="", encoding="utf-8")
    writer = csv.DictWriter(out_f, fieldnames=CSV_FIELDNAMES)
    if file_mode == "w":
        writer.writeheader()
        out_f.flush()

    all_rows = list(existing.values())
    try:
        for i, rec in enumerate(remaining, 1):
            messages = fixed_messages + [{"role": "user", "content": format_record(rec)}]
            raw = call_ollama(args.host, args.model, messages)
            pred = parse_prediction(raw)
            truth = set(rec["doc_type"])
            abstained = pred == ABSTAIN
            correct = (not abstained) and (pred in truth)

            row = {
                "id": rec["id"],
                "label": rec["label"],
                "root_label": rec["root_label"],
                "true_doc_type": ",".join(sorted(truth)),
                "predicted": pred,
                "correct": correct,
                "abstained": abstained,
                "raw_response": raw.strip().replace("\n", " "),
            }
            writer.writerow(row)
            out_f.flush()
            all_rows.append(row)

            status = "OK  " if correct else ("ABST" if abstained else "MISS")
            print(f"[{i:>3}/{len(remaining)}] {status}  {rec['label']:<30} "
                  f"true={','.join(sorted(truth)):<8} pred={pred}")
    finally:
        out_f.close()
        print_summary(summarize(all_rows))
        print(f"\nResults written to {out_path} ({len(all_rows)}/{n} total across all runs)")
        if len(all_rows) < n:
            print("Incomplete -- rerun with --resume to continue from here.")


if __name__ == "__main__":
    main()
