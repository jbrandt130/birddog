#!/usr/bin/env python3
"""
Batch eval for the P1/P2/NO acquisition-priority classifier prompt --
metadata PLUS extracted OCR text -- against a local Ollama model.

This is a parallel experiment to ../p1_p2_no/run_eval.py: identical system
framing (priority-aware domain framing, empirical doc_type/description
tendencies, the "break ties toward higher priority" instruction), identical
scoring methodology (single-label + abstain, deterministic compute_floor /
apply_page_count_escalation overrides, priority-aware asymmetric-cost
scoring). The only difference is that each record's prompt also includes
printed text OCR'd from its sampled page images (via
../vision_doc_recognition/extract_text.py, minicpm-v4.5), merged in by
build_eval_data.py in this folder. Comparing results.csv here against
../p1_p2_no/results.csv tells us whether OCR text adds usable signal on top
of metadata alone -- the same question already answered (positively) for
the doc_type task in ../doc_type_with_text/.

Per project decision: run this with qwen2.5:14b (the SAME model as the
metadata-only baseline, for a valid apples-to-apples comparison) and
UNFILTERED OCR text -- ../doc_type_with_text/'s zlib compression-ratio
noise filter was tested there and found to HURT qwen2.5:14b's results
(72.7% yield filtered vs 81.9% unfiltered vs 76.4% metadata-only baseline),
likely over-filtering genuinely repetitive real content (tabular/list
records) along with actual OCR garbage. So there is deliberately no
--min-compression-ratio option here.

One disclosed deviation from strict parity with ../p1_p2_no/eval_data.json:
34 few-shot examples there become 31 here, and 250 test_cases become 234
(19 documents across both sections have no rendered page images or source
PDF at all -- see ../vision_doc_recognition/build_p1_p2_no_extraction_data.py
-- so they were never eligible for OCR text and are dropped by
build_eval_data.py rather than included with an empty text field).

Usage:
    python3 run_eval.py [--model qwen2.5:14b] [--host http://localhost:11434]

Outputs:
    results.csv      - same columns as ../p1_p2_no/results.csv
    Summary printed to stdout (same priority-aware report).
"""

import argparse
import csv
import json
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

VALID_LABELS = ["P1", "P2", "NO"]
ABSTAIN = "UNCLEAR"
RANK = {"P1": 1, "P2": 2, "NO": 3}  # 1 = highest acquisition priority, 3 = do-not-acquire

# OCR text meaningfully bloats the fixed few-shot prefix (31 examples each
# carrying OCR text), same reasoning as ../doc_type_with_text/run_eval.py.
DEFAULT_NUM_CTX = 32768
DEFAULT_TIMEOUT = 300
DEFAULT_MAX_TEXT_CHARS = 1500


def build_system_message(label_definitions):
    lines = [
        "You are classifying archival documents from a Jewish genealogical research "
        "database by SECOND-LEVEL acquisition priority (this is the content-checker's "
        "decision after first-level triage, not the first-level triage decision itself). "
        "Assign exactly ONE of the following codes - the single code you are most "
        "confident best describes this record.",
        "",
    ]
    for code, desc in label_definitions.items():
        lines.append(f"- {code}: {desc}")
    lines.append("")
    lines.append(
        "For each document you are given: its archival label (archive/fond/opus/case), "
        "the archive code (root_label), its hierarchy level, its doc_type classification "
        "(V=vital record, C=census, L=list of names such as refugees/pogrom victims/"
        "passengers/community members, O=other, P=passport application), an English "
        "description of the content, a native-language description if available, a "
        "date/year range if recorded, and printed text OCR'd from a sample of the "
        "document's page images (letterhead, form field labels, stamps, headers -- "
        "handwritten content was deliberately not transcribed and is not included). The "
        "OCR text may be partial, imperfect, or occasionally absent. Use all of this "
        "together - the description is usually the most direct evidence, and the OCR "
        "text can corroborate or add signal the description doesn't capture, but don't "
        "over-trust it where it looks garbled or clearly mistranscribed."
    )
    lines.append("")
    lines.append(
        "WHAT THIS TRIAGE IS ACTUALLY FOR: this project's mission is helping genealogical "
        "researchers find records naming identifiable Jewish individuals. A record is "
        "TOP priority (P1) to the extent it is likely to name specific Jewish people - "
        "vital records (births/marriages/deaths), community/synagogue registers, and name "
        "lists (refugees, pogrom victims, passengers, community members, taxpayers, "
        "voters, petitioners) are exactly the kind of content this project exists to "
        "surface."
    )
    lines.append("")
    lines.append(
        "IMPORTANT: 'not obviously about Jewish individuals' does NOT automatically mean "
        "NO. Generic administrative/correspondence/institutional content with unclear "
        "Jewish relevance is genuinely a 3-way toss-up in this project's historical data "
        "(see the doc_type=O tendency below) - it is NOT usually NO by default. Reserve "
        "NO for content with a SPECIFIC reason to think it will never name individuals: "
        "known non-Jewish religious institutions (church/parish records with no Jewish "
        "connection), estate/garrison/property administration with no plausible personal-"
        "name content, or material that is clearly duplicative or out of scope."
    )
    lines.append("")
    lines.append(
        "Empirical tendencies from this project's own historical P1/P2/NO classifications "
        "(use these as a strong prior, not a rule - the actual description can and should "
        "override them):"
    )
    lines.append(
        "  - doc_type V (vital record): about 84% end up P1, essentially never P2 "
        "(under 1%). Vital records are the strongest single predictor of P1."
    )
    lines.append(
        "  - doc_type L (list of names): about 77% end up P1."
    )
    lines.append(
        "  - doc_type P (passport applications): about 79% end up P2 - the strongest "
        "single predictor of P2, and NO is rare for this doc_type (about 8%)."
    )
    lines.append(
        "  - doc_type C (census): mixed, leaning P1/NO - roughly 49% P1, 13% P2, 38% NO. "
        "Read the description carefully; don't assume from doc_type=C alone."
    )
    lines.append(
        "  - doc_type O (other): the MOST evenly split category - roughly 29% P1, 40% P2, "
        "31% NO. This is genuinely close to a three-way toss-up; doc_type alone tells you "
        "almost nothing for O content, so lean much more heavily on the description, "
        "archive/fond context, and the Jewish-name/church signals below."
    )
    lines.append(
        "  - Descriptions that explicitly mention Jewish people, communities, synagogues, "
        "or similar: about 85% end up P1 regardless of doc_type."
    )
    lines.append(
        "  - Descriptions about churches or other non-Jewish religious institutions with "
        "no indication of Jewish subjects: about 93% end up NO - one of the single "
        "strongest signals in the data."
    )
    lines.append(
        "  - Page count is a weaker but genuine signal: P1 records skew long (60% are "
        "over 60 pages - typically full record books/registers), while P2 records skew "
        "short (52% are 20 pages or fewer, often a handful of pages pulled from a larger "
        "case file). A long page count on a record you're inclined to call P2 out of "
        "uncertainty is a reason to reconsider - it's more consistent with a P1-style "
        "record book than a short excerpted P2 case."
    )
    lines.append("")
    lines.append(
        "P1 is the highest priority and NO means do-not-acquire. When the evidence "
        "genuinely leaves you torn between two adjacent codes, resolve that uncertainty "
        "by choosing the HIGHER priority one - it is much better to flag a record as more "
        "acquisition-worthy than it turns out to be than to let a genuinely valuable "
        "record be dropped entirely. This does not mean defaulting to P1 whenever you are "
        "unsure in general - use the content evidence to narrow it down first, and only "
        "let this rule break a genuine tie between adjacent codes."
    )
    lines.append("")
    lines.append(
        "Some records don't give you enough evidence to be confident in any code at all. "
        "In those cases, respond with the single word UNCLEAR instead of guessing. It is "
        "better to say UNCLEAR than to commit to a label you are not confident in."
    )
    lines.append("")
    lines.append(
        "Respond with exactly one token: one of P1, P2, NO, or UNCLEAR. No explanation, "
        "no additional labels, no punctuation."
    )
    return "\n".join(lines)


def format_record(rec, max_text_chars):
    years = rec.get("years") or "not recorded"
    native = rec.get("page_native_description") or "(none)"
    doc_type = ", ".join(rec.get("doc_type") or []) or "not recorded"
    page_count = rec.get("page_count")
    page_count_str = str(page_count) if page_count is not None else "not recorded"
    text = (rec.get("extracted_text") or "").strip()
    if not text or text == "NONE":
        text_block = "(none)"
    elif len(text) > max_text_chars:
        text_block = text[:max_text_chars] + " [...truncated]"
    else:
        text_block = text
    return (
        f"Archive (root_label): {rec['root_label']}\n"
        f"Label: {rec['label']}\n"
        f"Level: {rec['level']}\n"
        f"doc_type: {doc_type}\n"
        f"Page count: {page_count_str}\n"
        f"Years: {years}\n"
        f"Description: {rec['page_description']}\n"
        f"Native description: {native}\n"
        f"OCR'd printed text (from sampled page images): {text_block}\n"
        f"acquisition_priority:"
    )


def build_fixed_messages(data, max_text_chars):
    """Fixed prefix: system instructions + few-shot examples as user/assistant
    turns. Identical across every call in this run -> eligible for Ollama's
    automatic prefix/KV-cache reuse. Only the final user turn (the record to
    classify) varies per call. Used for --api chat."""
    messages = [
        {"role": "system", "content": build_system_message(data["label_definitions"])}
    ]
    for ex in data["few_shot"]:
        messages.append({"role": "user", "content": format_record(ex, max_text_chars)})
        messages.append({"role": "assistant", "content": ex["target"]})
    return messages


def build_fixed_prompt_prefix(data, max_text_chars):
    """Same fixed few-shot content as build_fixed_messages, flattened into a
    single text block for --api generate (needed for models whose Ollama
    chat template can hard-fail on /api/chat, e.g. minicpm-v4.5)."""
    parts = []
    for ex in data["few_shot"]:
        parts.append(format_record(ex, max_text_chars) + " " + ex["target"])
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


def compute_floor(rec):
    """Deterministic escalation floor -- copied unchanged from
    ../p1_p2_no/run_eval.py. See that module's docstring for the
    process_code precedent and rationale."""
    doc_types = set(rec.get("doc_type") or [])
    desc = (rec.get("page_description") or "").lower()
    if doc_types & {"V", "L", "P"}:
        return "P2"
    if "jewish" in desc:
        return "P2"
    return None


def apply_floor(pred, floor):
    if floor is None:
        return pred
    if RANK[pred] > RANK[floor]:
        return floor
    return pred


def apply_page_count_escalation(pred, page_count, doc_types, threshold=60):
    """Copied unchanged from ../p1_p2_no/run_eval.py -- see that module's
    docstring for the empirical replay that scoped this to doc_type=L."""
    if (
        pred == "P2"
        and page_count is not None
        and page_count > threshold
        and "L" in (doc_types or set())
    ):
        return "P1", True
    return pred, False


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


CSV_FIELDNAMES = ["id", "label", "root_label", "description", "page_count",
                   "true_target", "model_predicted", "final_predicted",
                   "floor_applied", "page_count_escalated", "correct",
                   "abstained", "signed_rank_error", "raw_response"]


def load_existing_results(out_path):
    """Load previously-written rows keyed by id, for --resume."""
    existing = {}
    if out_path.exists():
        with open(out_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing[int(row["id"])] = row
    return existing


def summarize(rows, under_weight, over_weight, abstain_cost):
    """Recompute the full priority-aware report from a list of result dicts
    -- works whether they came from this run or were reloaded from a
    previous partial run via --resume (the per-row floor/escalation/
    signed_rank_error fields were already baked in when each row was
    written, so this just re-aggregates them)."""
    n = len(rows)
    committed = sum(1 for r in rows if str(r["abstained"]) != "True")
    correct_committed = sum(1 for r in rows if str(r["correct"]) == "True")

    label_hit = {l: 0 for l in VALID_LABELS}
    label_abstain = {l: 0 for l in VALID_LABELS}
    label_wrong = {l: 0 for l in VALID_LABELS}
    label_support = {l: 0 for l in VALID_LABELS}
    confusion = {l: {m: 0 for m in VALID_LABELS} for l in VALID_LABELS}

    captured = 0
    under_triage = 0
    over_triage = 0
    under_severity = {1: 0, 2: 0}
    total_cost = 0.0
    floor_applied_count = 0
    page_count_escalated_count = 0

    for r in rows:
        truth = r["true_target"]
        pred = r["final_predicted"]
        abstained = str(r["abstained"]) == "True"
        correct = str(r["correct"]) == "True"
        floor_applied = str(r["floor_applied"]) == "True"
        page_count_escalated = str(r["page_count_escalated"]) == "True"

        if floor_applied:
            floor_applied_count += 1
        if page_count_escalated:
            page_count_escalated_count += 1

        if truth in label_support:
            label_support[truth] += 1
            if abstained:
                label_abstain[truth] += 1
            elif correct:
                label_hit[truth] += 1
            else:
                label_wrong[truth] += 1
                if pred in confusion:
                    confusion[pred][truth] += 1

        if abstained:
            captured += 1
            total_cost += abstain_cost
        else:
            signed_raw = r.get("signed_rank_error")
            signed_error = int(signed_raw) if signed_raw not in (None, "", "None") else None
            if signed_error is None:
                signed_error = RANK[pred] - RANK[truth]
            if signed_error > 0:
                under_triage += 1
                under_severity[signed_error] = under_severity.get(signed_error, 0) + 1
                total_cost += under_weight * signed_error
            else:
                captured += 1
                if signed_error < 0:
                    over_triage += 1
                    total_cost += over_weight * (-signed_error)

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

    print("\nPer-true-label breakdown:")
    print(f"{'label':<6}{'support':>8}{'hit':>6}{'abstain':>9}{'wrong':>7}")
    for l in VALID_LABELS:
        s = label_support[l]
        if s == 0:
            continue
        print(f"{l:<6}{s:>8}{label_hit[l]:>6}{label_abstain[l]:>9}{label_wrong[l]:>7}")

    print("\nConfusion among wrong predictions (rows=predicted, cols=true):")
    header = "        " + "".join(f"{m:>6}" for m in VALID_LABELS)
    print(header)
    for l in VALID_LABELS:
        print(f"{l:<8}" + "".join(f"{confusion[l][m]:>6}" for m in VALID_LABELS))

    print(
        "\n(hit = predicted matched true target; abstain = said UNCLEAR; "
        "wrong = predicted a different label than true target)"
    )

    print("\n" + "=" * 70)
    print("PRIORITY-AWARE SCORING (asymmetric cost: under-triage is the")
    print(f"dangerous error; weights under={under_weight}, "
          f"over={over_weight}, abstain={abstain_cost})")
    print(f"Deterministic escalation floor: applied to {floor_applied_count}/{n} "
          f"predictions ({100*floor_applied_count/n:.1f}%)")
    print(f"page_count P2->P1 escalation: applied to {page_count_escalated_count}/{n} "
          f"predictions ({100*page_count_escalated_count/n:.1f}%)")
    print("=" * 70)
    capture_rate = captured / n
    under_rate = under_triage / n
    over_rate = over_triage / n
    mean_cost = total_cost / n
    print(f"Capture rate (not silently under-triaged): {captured}/{n} ({100*capture_rate:.1f}%)")
    print(f"Under-triage rate (predicted LOWER priority than truth - the bad "
          f"direction): {under_triage}/{n} ({100*under_rate:.1f}%)")
    if under_triage:
        print(f"  of which off-by-one tier: {under_severity.get(1, 0)}, "
              f"off-by-two tiers: {under_severity.get(2, 0)}")
    print(f"Over-triage rate (predicted HIGHER priority than truth - the safe "
          f"direction): {over_triage}/{n} ({100*over_rate:.1f}%)")
    print(f"Mean weighted cost per record: {mean_cost:.3f} (lower is better)")
    print(
        "\n(capture_rate is the headline number for this use case - it answers "
        "'how often did we fail to flag a record that needed higher-priority "
        "attention.' Adjust --under-weight/--over-weight/--abstain-cost to reflect "
        "the project's actual tolerance for each error type.)"
    )
    print(f"\nCompare against ../p1_p2_no/results.csv (metadata-only baseline) "
          f"to see whether OCR text helped.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen2.5:14b",
                         help="use the same model as ../p1_p2_no/run_eval.py's "
                              "default for a valid apples-to-apples comparison")
    parser.add_argument("--host", default="http://localhost:11434")
    parser.add_argument("--data", default="eval_data.json")
    parser.add_argument("--out", default="results.csv")
    parser.add_argument("--api", choices=["chat", "generate"], default="chat",
                         help="chat (default) matches how the qwen2.5:14b metadata-only "
                              "baseline was generated. Use generate for models whose "
                              "Ollama chat template can hard-fail on /api/chat "
                              "(observed with minicpm-v4.5).")
    parser.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--max-text-chars", type=int, default=DEFAULT_MAX_TEXT_CHARS,
                         help="truncate each record's OCR text to this many characters "
                              "before including it in the prompt. No noise filter here "
                              "(unlike ../doc_type_with_text/) -- that was tested and "
                              "found to hurt qwen2.5:14b's results there.")
    parser.add_argument("--under-weight", type=float, default=5.0,
                         help="Cost per tier of under-triage (predicting lower "
                              "priority than truth). Default 5.")
    parser.add_argument("--over-weight", type=float, default=1.0,
                         help="Cost per tier of over-triage (predicting higher "
                              "priority than truth). Default 1.")
    parser.add_argument("--abstain-cost", type=float, default=1.0,
                         help="Flat cost per UNCLEAR abstention. Default 1.")
    parser.add_argument("--no-floor", action="store_true",
                         help="Disable the deterministic doc_type/keyword escalation "
                              "floor and score the model's raw predictions as-is.")
    parser.add_argument("--no-page-count-escalation", action="store_true",
                         help="Disable the page_count-based P2->P1 escalation override.")
    parser.add_argument("--page-count-threshold", type=int, default=60,
                         help="page_count above which a predicted-P2 record gets "
                              "escalated to P1 (default 60).")
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
        fixed_messages = build_fixed_messages(data, args.max_text_chars)
    else:
        system_text = build_system_message(data["label_definitions"])
        fixed_prefix = build_fixed_prompt_prefix(data, args.max_text_chars)

    out_path = Path(args.out)
    existing = load_existing_results(out_path) if args.resume else {}
    if existing:
        print(f"--resume: {len(existing)} test cases already done, skipping those.\n")

    remaining = [rec for rec in test_cases if rec["id"] not in existing]
    print(f"Running {len(remaining)} test cases ({n - len(remaining)} already done) "
          f"against {args.model} at {args.host} "
          f"(--api {args.api}, max_text_chars={args.max_text_chars}, unfiltered) ...\n")

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
                    {"role": "user", "content": format_record(rec, args.max_text_chars)}
                ]
                raw = call_ollama(args.host, args.model, messages,
                                   num_ctx=args.num_ctx, timeout=args.timeout)
            else:
                prompt = fixed_prefix + format_record(rec, args.max_text_chars)
                raw = call_ollama_generate(args.host, args.model, system_text, prompt,
                                            num_ctx=args.num_ctx, timeout=args.timeout)

            raw_pred = parse_prediction(raw)
            truth = rec["target"]

            abstained = raw_pred == ABSTAIN
            floor_applied = False
            page_count_escalated = False
            if abstained:
                pred = raw_pred
            else:
                floor = None if args.no_floor else compute_floor(rec)
                pred = apply_floor(raw_pred, floor)
                floor_applied = pred != raw_pred
                if not args.no_page_count_escalation:
                    pred, page_count_escalated = apply_page_count_escalation(
                        pred, rec.get("page_count"), set(rec.get("doc_type") or []),
                        args.page_count_threshold
                    )
            correct = (not abstained) and (pred == truth)
            signed_error = None if abstained else (RANK[pred] - RANK[truth])

            row = {
                "id": rec["id"],
                "label": rec["label"],
                "root_label": rec["root_label"],
                "description": rec["page_description"],
                "page_count": rec.get("page_count"),
                "true_target": truth,
                "model_predicted": raw_pred,
                "final_predicted": pred,
                "floor_applied": floor_applied,
                "page_count_escalated": page_count_escalated,
                "correct": correct,
                "abstained": abstained,
                "signed_rank_error": signed_error,
                "raw_response": raw.strip().replace("\n", " "),
            }
            writer.writerow(row)
            f.flush()
            existing[rec["id"]] = {k: str(v) for k, v in row.items()}

            status = "OK  " if correct else ("ABST" if abstained else "MISS")
            note = ""
            if floor_applied:
                note += f" (floored ->{pred})"
            if page_count_escalated:
                note += f" (pc-escalated ->{pred})"
            print(
                f"[{i:>3}/{len(remaining)}] {status}  {rec['label']:<30} "
                f"true={truth:<3} pred={pred}{note}"
            )
    finally:
        f.close()
        print(f"\n{len(existing)}/{n} test cases done, written to {out_path}")
        if len(existing) < n:
            print("Incomplete -- rerun with --resume to continue from here.")

        summarize(list(existing.values()), args.under_weight, args.over_weight,
                  args.abstain_cost)


if __name__ == "__main__":
    main()
