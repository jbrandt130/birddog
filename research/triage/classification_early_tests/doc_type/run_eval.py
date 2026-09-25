#!/usr/bin/env python3
"""
Batch eval for the doc_type classifier prompt against a local Ollama model.

Usage:
    python3 run_eval.py [--model qwen2.5:14b] [--host http://localhost:11434]

SINGLE-LABEL + ABSTAIN FRAMING (v2 of this script):
Instead of asking the model to predict every applicable doc_type label
(multi-label), the model is now asked to commit to exactly one label it is
confident about, or say UNCLEAR if the record plausibly fits more than one
type or doesn't give enough evidence to choose. This was a deliberate
reframing after multi-label eval runs showed heavy over-tagging (especially
spurious extra L and O/C labels riding along with a correct guess). Making
abstention a legitimate answer removes the pressure to guess on genuinely
ambiguous O/L/C boundary cases and lets us score in terms of a
precision/coverage trade-off instead of exact-match set equality.

Scoring is no longer per-label precision/recall/F1 over label sets. Instead:
  - completeness: fraction of test records where the model committed to a
    label instead of saying UNCLEAR
  - accuracy_on_committed: of the records where it committed, what fraction
    were "correct" (predicted label is a member of the record's true
    doc_type set - not exact match, since ground truth can still be
    multi-label even though the model only ever gives one answer)
  - overall_yield: correct_committed / total (coverage-weighted accuracy -
    a single number that penalizes both wrong guesses and abstentions)
  - balanced_score: harmonic mean of accuracy_on_committed and completeness,
    analogous to F1 but over (accuracy, coverage) instead of (precision,
    recall). Reported alongside the two components since the single number
    alone hides which one is driving it.
  - per-true-label breakdown: for records whose true set contains a given
    label, how often the model (a) named that exact label, (b) named a
    different label that also happens to be correct (multi-label ground
    truth), (c) abstained, or (d) got it wrong. This is what tells us
    whether the model is now properly hedging on the O/L confusion cases
    instead of guessing.

MULTI-MODEL / --api NOTES (added when reusing this script to test
minicpm-v4.5 as a classifier, not just qwen2.5:14b): on the Ollama version
this project runs, minicpm-v4.5's chat template routes /api/chat through a
strict "peg-native" grammar parser that hard-fails (HTTP 500) on responses
that aren't shaped like a tool call -- observed in
../vision_doc_recognition/extract_text.py on plain narrative text, short or
long, degenerate or clean. A single-token classification answer ("V",
"UNCLEAR", ...) MIGHT dodge that, but it's not proven safe, and the fix
used there (switch to /api/generate, which has no such parsing layer) is
cheap enough to apply here too. --api generate flattens the fixed few-shot
prefix into a single prompt string instead of chat messages; --api chat
(the default, unchanged) is what qwen2.5:14b's baseline results.csv was
generated with and should stay the default for reproducibility.

Outputs:
    results.csv      - one row per test record: id, label, true doc_type,
                        predicted (single label or UNCLEAR), correct flag,
                        abstained flag, raw model output
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

VALID_LABELS = ["V", "C", "L", "O", "P"]  # M is legacy/undocumented - excluded
ABSTAIN = "UNCLEAR"

DEFAULT_NUM_CTX = 8192
DEFAULT_TIMEOUT = 300


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
        "the content, a native-language description if available, and a date/year range "
        "if recorded. Use all of these together - the archive and fond often carry strong "
        "signal about record type, and the description is usually the most direct evidence."
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
    return (
        f"Archive (root_label): {rec['root_label']}\n"
        f"Label: {rec['label']}\n"
        f"Level: {rec['level']}\n"
        f"Years: {years}\n"
        f"Description: {rec['page_description']}\n"
        f"Native description: {native}\n"
        f"doc_type:"
    )


def build_fixed_messages(data):
    """Fixed prefix: system instructions + few-shot examples as user/assistant
    turns. Identical across every call in this run -> eligible for Ollama's
    automatic prefix/KV-cache reuse. Only the final user turn (the record to
    classify) varies per call. Used for --api chat."""
    messages = [
        {"role": "system", "content": build_system_message(data["label_definitions"])}
    ]
    for ex in data["few_shot"]:
        messages.append({"role": "user", "content": format_record(ex)})
        messages.append({"role": "assistant", "content": ex["target"]})
    return messages


def build_fixed_prompt_prefix(data):
    """Same fixed few-shot content as build_fixed_messages, flattened into a
    single text block for --api generate (no chat-message structure, so no
    chat-template/tool-call parsing layer to trip over)."""
    parts = []
    for ex in data["few_shot"]:
        parts.append(format_record(ex) + " " + ex["target"])
    return "\n\n".join(parts) + "\n\n"


def parse_prediction(raw_text):
    """Extract a single prediction: one of VALID_LABELS, or ABSTAIN. Falls
    back to ABSTAIN if nothing recognizable is found, since a non-committal
    or malformed response should be scored as a non-answer rather than
    penalized as a wrong guess."""
    text = raw_text.strip().upper()
    if re.search(r"\bUNCLEAR\b", text):
        return ABSTAIN
    tokens = re.split(r"[,\s/;.]+", text)
    for tok in tokens:
        tok = tok.strip()
        if tok in VALID_LABELS:
            return tok
    return ABSTAIN


def call_ollama(host, model, messages, num_ctx=None, timeout=DEFAULT_TIMEOUT):
    url = f"{host.rstrip('/')}/api/chat"
    options = {"temperature": 0.1}
    if num_ctx:
        options["num_ctx"] = num_ctx
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": options,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        print(f"ERROR calling Ollama at {url}: HTTP {e.code} {e.reason}", file=sys.stderr)
        print(f"Response body: {detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"ERROR calling Ollama at {url}: {e}", file=sys.stderr)
        print("Is `ollama serve` running, and is the model pulled?", file=sys.stderr)
        sys.exit(1)
    return body["message"]["content"]


def call_ollama_generate(host, model, system, prompt, num_ctx=None, timeout=DEFAULT_TIMEOUT):
    url = f"{host.rstrip('/')}/api/generate"
    options = {"temperature": 0.1}
    if num_ctx:
        options["num_ctx"] = num_ctx
    payload = {
        "model": model,
        "system": system,
        "prompt": prompt,
        "stream": False,
        "options": options,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        print(f"ERROR calling Ollama at {url}: HTTP {e.code} {e.reason}", file=sys.stderr)
        print(f"Response body: {detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"ERROR calling Ollama at {url}: {e}", file=sys.stderr)
        print("Is `ollama serve` running, and is the model pulled?", file=sys.stderr)
        sys.exit(1)
    return body["response"]


def harmonic_mean(a, b):
    if a != a or b != b:  # nan check
        return float("nan")
    if (a + b) == 0:
        return 0.0
    return 2 * a * b / (a + b)


CSV_FIELDNAMES = ["id", "label", "root_label", "description", "true_doc_type",
                   "predicted", "correct", "abstained", "raw_response"]


def load_existing_results(out_path):
    """Load previously-written rows keyed by id, for --resume. Tolerates a
    missing file (returns {})."""
    existing = {}
    if out_path.exists():
        with open(out_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing[int(row["id"])] = row
    return existing


def summarize(rows):
    """Compute the completeness/accuracy/balanced-score summary from a list
    of result dicts -- works whether they came from this run or were
    reloaded from a previous partial run via --resume."""
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

    completeness = committed / n if n else float("nan")
    accuracy_on_committed = correct_committed / committed if committed else float("nan")
    overall_yield = correct_committed / n if n else float("nan")
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
        s = label_support[l]
        if s == 0:
            continue
        print(
            f"{l:<6}{s:>8}{label_hit[l]:>6}{label_hit_other[l]:>11}"
            f"{label_abstain[l]:>9}{label_wrong[l]:>7}"
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
    parser.add_argument("--data", default="eval_data.json")
    parser.add_argument("--out", default="results.csv")
    parser.add_argument("--api", choices=["chat", "generate"], default="chat",
                         help="chat (default) matches how the qwen2.5:14b baseline "
                              "results.csv was generated. Use generate for models "
                              "whose Ollama chat template can hard-fail on /api/chat "
                              "(observed with minicpm-v4.5).")
    parser.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--resume", action="store_true", default=False,
                         help="skip test cases already present in --out and "
                              "append to it instead of starting over")
    args = parser.parse_args()

    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    test_cases = data["test_cases"]
    n = len(test_cases)

    if args.api == "chat":
        fixed_messages = build_fixed_messages(data)
    else:
        system_text = build_system_message(data["label_definitions"])
        fixed_prefix = build_fixed_prompt_prefix(data)

    out_path = Path(args.out)
    existing = load_existing_results(out_path) if args.resume else {}
    if existing:
        print(f"--resume: {len(existing)} test cases already done, skipping those.\n")

    remaining = [rec for rec in test_cases if rec["id"] not in existing]
    print(f"Running {len(remaining)} test cases ({n - len(remaining)} already done) "
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
                messages = fixed_messages + [{"role": "user", "content": format_record(rec)}]
                raw = call_ollama(args.host, args.model, messages,
                                   num_ctx=args.num_ctx, timeout=args.timeout)
            else:
                prompt = fixed_prefix + format_record(rec)
                raw = call_ollama_generate(args.host, args.model, system_text, prompt,
                                            num_ctx=args.num_ctx, timeout=args.timeout)

            pred = parse_prediction(raw)
            truth = set(rec["doc_type"])

            abstained = pred == ABSTAIN
            correct = (not abstained) and (pred in truth)

            row = {
                "id": rec["id"],
                "label": rec["label"],
                "root_label": rec["root_label"],
                "description": rec["page_description"],
                "true_doc_type": ",".join(sorted(truth)),
                "predicted": pred,
                "correct": correct,
                "abstained": abstained,
                "raw_response": raw.strip().replace("\n", " "),
            }
            writer.writerow(row)
            f.flush()
            existing[rec["id"]] = {k: str(v) for k, v in row.items()}

            status = "OK  " if correct else ("ABST" if abstained else "MISS")
            print(
                f"[{i:>3}/{len(remaining)}] {status}  {rec['label']:<30} "
                f"true={','.join(sorted(truth)):<8} pred={pred}"
            )
    finally:
        f.close()
        print(f"\n{len(existing)}/{n} test cases done, written to {out_path}")
        if len(existing) < n:
            print("Incomplete -- rerun with --resume to continue from here.")

        summarize(list(existing.values()))
        print(f"\nDetailed results written to {out_path}")


if __name__ == "__main__":
    main()
