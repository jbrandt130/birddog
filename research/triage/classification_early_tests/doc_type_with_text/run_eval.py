#!/usr/bin/env python3
"""
Batch eval for the doc_type classifier prompt -- metadata PLUS extracted
OCR text -- against a local Ollama model.

This is a parallel experiment to ../doc_type/run_eval.py: identical system
framing, identical single-label + abstain scoring methodology, identical
216 held-out test_cases and doc_type ground truth. The only difference is
that each record's prompt also includes printed text OCR'd from its sampled
page images (via ../vision_doc_recognition/extract_text.py, minicpm-v4.5),
built into eval_data.json by build_eval_data.py in this folder. Comparing
results.csv here against ../doc_type/results.csv tells us whether OCR text
adds usable signal on top of metadata alone.

For the comparison to mean anything, run this with the SAME model as the
metadata-only baseline (qwen2.5:14b, the default in ../doc_type/run_eval.py)
so any score difference is attributable to the added text, not a different
model's capability. See session notes for why.

Usage:
    python3 run_eval.py [--model qwen2.5:14b] [--host http://localhost:11434]

One disclosed deviation from strict parity: the metadata-only baseline's
few-shot set has 18 examples; this one has 17 (one document has no
extracted text -- see build_eval_data.py). The 216 test_cases are identical
in both.

Extracted OCR text is truncated to --max-text-chars (default 1500) per
record before being included in the prompt. Some documents' OCR output
runs to 20K+ characters (a dense page transcribed near the extraction
script's own token cap); including that in full, in the FIXED few-shot
prefix, would bloat every single call for one outlier document. 1500 chars
comfortably covers the letterhead/header/caption signal this experiment
is actually testing for.

Outputs:
    results.csv      - same columns as ../doc_type/results.csv
    Summary printed to stdout.
"""

import argparse
import csv
import json
import re
import sys
import urllib.request
import urllib.error
import zlib
from pathlib import Path

VALID_LABELS = ["V", "C", "L", "O", "P"]  # M is legacy/undocumented - excluded
ABSTAIN = "UNCLEAR"

# The metadata-only baseline didn't need to set this explicitly (short
# prompts). With OCR text added to every few-shot example, the fixed prefix
# is meaningfully larger (~5-8K tokens depending on --max-text-chars), so
# size num_ctx with real headroom rather than relying on Ollama's default.
DEFAULT_NUM_CTX = 32768
DEFAULT_TIMEOUT = 300
DEFAULT_MAX_TEXT_CHARS = 1500

# Some minicpm-v4.5 OCR output is degenerate repetition rather than real
# transcription (e.g. a single short fragment repeated 50 times, or one word
# repeated for thousands of characters) -- checked across all 231 documents
# with extracted text: genuine transcribed prose zlib-compresses to roughly
# 0.40-0.70 of its original size (median 0.44), while the repetitive/garbled
# cases compress to as little as 0.01-0.15. A zlib compression-ratio check is
# a cheap, effective stand-in for a full entropy test. Texts shorter than
# MIN_LEN_FOR_QUALITY_CHECK are skipped -- zlib's fixed overhead dominates
# the ratio for very short strings and makes it meaningless there.
DEFAULT_MIN_COMPRESSION_RATIO = 0.20
MIN_LEN_FOR_QUALITY_CHECK = 200


def text_compression_ratio(text):
    raw = text.encode("utf-8")
    if not raw:
        return 1.0
    return len(zlib.compress(raw, level=9)) / len(raw)


def is_noisy_text(text, min_ratio):
    if min_ratio <= 0 or len(text) < MIN_LEN_FOR_QUALITY_CHECK:
        return False
    return text_compression_ratio(text) < min_ratio


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
        "recorded, and printed text OCR'd from a sample of the document's page images "
        "(letterhead, form field labels, stamps, headers -- handwritten content was "
        "deliberately not transcribed and is not included). The OCR text may be partial, "
        "imperfect, or occasionally absent. Use all of this together - the archive and "
        "fond often carry strong signal about record type, the description is usually "
        "the most direct evidence, and the OCR text can corroborate or add signal the "
        "description doesn't capture, but don't over-trust it where it looks garbled or "
        "clearly mistranscribed."
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


def format_record(rec, max_text_chars, min_compression_ratio=0.0):
    years = rec.get("years") or "not recorded"
    native = rec.get("page_native_description") or "(none)"
    text = (rec.get("extracted_text") or "").strip()
    if not text or text == "NONE":
        text_block = "(none)"
    elif is_noisy_text(text, min_compression_ratio):
        # Checked against the raw, untruncated text -- truncating first could
        # change a repetitive string's apparent compressibility and mask
        # exactly the pattern this check is looking for.
        text_block = "(OCR text omitted -- appears to be repetitive/low-information garbage)"
    elif len(text) > max_text_chars:
        text_block = text[:max_text_chars] + " [...truncated]"
    else:
        text_block = text
    return (
        f"Archive (root_label): {rec['root_label']}\n"
        f"Label: {rec['label']}\n"
        f"Level: {rec['level']}\n"
        f"Years: {years}\n"
        f"Description: {rec['page_description']}\n"
        f"Native description: {native}\n"
        f"OCR'd printed text (from sampled page images): {text_block}\n"
        f"doc_type:"
    )


def build_fixed_messages(data, max_text_chars, min_compression_ratio=0.0):
    """Fixed prefix: system instructions + few-shot examples as user/assistant
    turns. Identical across every call in this run -> eligible for Ollama's
    automatic prefix/KV-cache reuse. Only the final user turn (the record to
    classify) varies per call. Used for --api chat."""
    messages = [
        {"role": "system", "content": build_system_message(data["label_definitions"])}
    ]
    for ex in data["few_shot"]:
        messages.append({"role": "user",
                          "content": format_record(ex, max_text_chars, min_compression_ratio)})
        messages.append({"role": "assistant", "content": ex["target"]})
    return messages


def build_fixed_prompt_prefix(data, max_text_chars, min_compression_ratio=0.0):
    """Same fixed few-shot content as build_fixed_messages, flattened into a
    single text block for --api generate. Needed for minicpm-v4.5: its
    Ollama chat template routes /api/chat through a strict "peg-native"
    parser that hard-failed (HTTP 500) on plain narrative text in
    ../vision_doc_recognition/extract_text.py, short or long, degenerate or
    clean. /api/generate has no such parsing layer."""
    parts = []
    for ex in data["few_shot"]:
        parts.append(format_record(ex, max_text_chars, min_compression_ratio) + " " + ex["target"])
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


def call_ollama(host, model, messages, num_ctx=None, timeout=300):
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


def call_ollama_generate(host, model, system, prompt, num_ctx=None, timeout=300):
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
    missing file (returns {}). Mirrors ../vision_doc_recognition/run_eval.py's
    approach -- this run also gets interrupted by disconnects/crashes often
    enough that per-record incremental writes are worth having."""
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
    parser.add_argument("--model", default="qwen2.5:14b",
                         help="use the same model as ../doc_type/run_eval.py's "
                              "default for a valid apples-to-apples comparison")
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
    parser.add_argument("--max-text-chars", type=int, default=DEFAULT_MAX_TEXT_CHARS,
                         help="truncate each record's OCR text to this many "
                              "characters before including it in the prompt")
    parser.add_argument("--min-compression-ratio", type=float,
                         default=DEFAULT_MIN_COMPRESSION_RATIO,
                         help="OCR text that zlib-compresses below this ratio (i.e. is "
                              "mostly repetition) is omitted from the prompt rather than "
                              "included -- checked on real data to separate genuine "
                              "transcription (~0.40-0.70) from degenerate repeated-token "
                              "garbage (as low as 0.01-0.15). Set to 0 to disable.")
    parser.add_argument("--resume", action="store_true", default=False,
                         help="skip test cases already present in --out and "
                              "append to it instead of starting over")
    parser.add_argument("--limit", type=int, default=None,
                         help="only run this many test cases, evenly spread across "
                              "the full set (for diversity across labels/archives) "
                              "rather than just the first N -- for cheap diagnostics")
    args = parser.parse_args()

    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    test_cases = data["test_cases"]
    if args.limit and args.limit < len(test_cases):
        stride = len(test_cases) / args.limit
        test_cases = [test_cases[int(i * stride)] for i in range(args.limit)]
    n = len(test_cases)

    if args.api == "chat":
        fixed_messages = build_fixed_messages(data, args.max_text_chars,
                                               args.min_compression_ratio)
    else:
        system_text = build_system_message(data["label_definitions"])
        fixed_prefix = build_fixed_prompt_prefix(data, args.max_text_chars,
                                                  args.min_compression_ratio)

    out_path = Path(args.out)
    existing = load_existing_results(out_path) if args.resume else {}
    if existing:
        print(f"--resume: {len(existing)} test cases already done, skipping those.\n")

    remaining = [rec for rec in test_cases if rec["id"] not in existing]
    print(f"Running {len(remaining)} test cases ({n - len(remaining)} already done) "
          f"against {args.model} at {args.host} "
          f"(--api {args.api}, max_text_chars={args.max_text_chars}, "
          f"min_compression_ratio={args.min_compression_ratio}) ...\n")

    write_header = not (args.resume and out_path.exists())
    f = open(out_path, "a" if args.resume and out_path.exists() else "w",
             newline="", encoding="utf-8")
    writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
    if write_header:
        writer.writeheader()

    try:
        for i, rec in enumerate(remaining, 1):
            if args.api == "chat":
                messages = fixed_messages + [
                    {"role": "user", "content": format_record(
                        rec, args.max_text_chars, args.min_compression_ratio)}
                ]
                raw = call_ollama(args.host, args.model, messages,
                                   num_ctx=args.num_ctx, timeout=args.timeout)
            else:
                prompt = fixed_prefix + format_record(
                    rec, args.max_text_chars, args.min_compression_ratio)
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
        print(f"\nCompare against ../doc_type/results.csv (same 216 test_cases, "
              f"metadata-only baseline) to see whether OCR text helped.")


if __name__ == "__main__":
    main()
