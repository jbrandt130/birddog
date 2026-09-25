#!/usr/bin/env python3
"""
Batch eval for the doc_type classifier using page IMAGES instead of metadata
text, against a local Ollama vision model (e.g. qwen3-vl:8b).

This is a parallel test to ../doc_type/run_eval.py: same few-shot/test-case
document identities and target labels (see build_eval_data.py), same
single-label + abstain framing and scoring methodology, but the model is
shown a fixed sample of k page images per document instead of an archive/
label/description text block. Comparing results between the two scripts
tells us whether page images carry usable doc_type signal on their own,
and how that compares to the metadata-only signal.

Usage:
    python3 run_eval.py [--model qwen3-vl:8b] [--host http://localhost:11434]

Outputs:
    results.csv      - one row per test record: id, label, true doc_type,
                        predicted (single label or UNCLEAR), correct flag,
                        abstained flag, raw model output
    Summary printed to stdout.

Requires Pillow for image downscaling:  pip install pillow
"""

import argparse
import base64
import csv
import io
import json
import os
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

from PIL import Image

# Some source scans are oversized ledger/register pages rendered at 200 DPI
# (up to ~14000x11000px, legitimate large historical documents, not corrupt
# files) -- disable PIL's decompression-bomb guard rather than have it warn
# or refuse on every other page.
Image.MAX_IMAGE_PIXELS = None

HERE = Path(__file__).parent


def _resolve_image_root():
    """Root that record['image_paths'] (e.g. "pages/1234/00007.jpg") resolve
    against.

    Two layouts have to work:
      - LOCAL: the cached PDFs and rendered page JPGs live in ../data/
        (moved out of this folder so the code tree isn't carrying ~112GB of
        binary data around).
      - MODAL CONTAINER: prepare_modal_staging*.py stages only the referenced
        JPGs into modal_staging/pages/, which the runner copies next to the
        script, so "pages/" sits in the script's own directory.

    Prefer a sibling "pages/" dir when one exists (the container case), else
    fall back to ../data. JEWISHGEN_DATA_ROOT overrides both.
    """
    env = os.environ.get("JEWISHGEN_DATA_ROOT")
    if env:
        return Path(env)
    if (HERE / "pages").is_dir():
        return HERE
    return HERE.parent / "data"


DATA_ROOT = _resolve_image_root()

VALID_LABELS = ["V", "C", "L", "O", "P"]  # M is legacy/undocumented - excluded
ABSTAIN = "UNCLEAR"

# The source page renders are 200 DPI (1500-3500px on the long side, 0.5-3.5MB
# each as JPG). Sent raw, the fixed few-shot prefix alone (17 docs x 3 images)
# balloons to ~500MB of base64 PER REQUEST -- and since /api/chat is stateless
# over HTTP, that whole prefix is retransmitted on every single call, 216
# times over a full eval run. Downscaling to a model-appropriate resolution
# before encoding keeps requests fast without sacrificing anything the vision
# encoder would actually use (most VLMs downsample well below 1500px internally
# anyway). Override via --max-dim if you want to experiment with resolution.
DEFAULT_MAX_DIM = 1024
JPEG_QUALITY = 85

# Ollama's default context window (32768 on most installs) is too small for
# the fixed 17-doc/3-image few-shot prefix (~42K tokens observed at
# --max-dim 1024). 65536 gives headroom for larger scans/longer runs.
DEFAULT_NUM_CTX = 65536

# Prompt eval of a large context + many images can genuinely take minutes,
# particularly on CPU or a modest GPU, and especially on the first call
# after num_ctx forces Ollama to reallocate its KV cache.
DEFAULT_TIMEOUT = 900


def build_system_message(label_definitions):
    lines = [
        "You are classifying archival documents from a Jewish genealogical research "
        "database. You will be shown a sample of page images from a single document. "
        "Assign exactly ONE of the following document types - the single type you are "
        "most confident best describes the record.",
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
        "For each document you are given a handful of its page images (not "
        "necessarily the first pages, and not every page) - use their layout, "
        "handwriting/print style, tabular structure, headers, and any legible text "
        "to judge the record type. The images may be in Ukrainian, Russian, Polish, "
        "or other historical languages/scripts."
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


def load_image_b64(rel_path, max_dim=DEFAULT_MAX_DIM):
    path = DATA_ROOT / rel_path
    if not max_dim:
        return base64.b64encode(path.read_bytes()).decode("ascii")
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        scale = max_dim / max(w, h)
        if scale < 1:
            im = im.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=JPEG_QUALITY)
        return base64.b64encode(buf.getvalue()).decode("ascii")


def format_prompt_text(rec):
    return f"Document with {len(rec['image_paths'])} sampled page image(s). doc_type:"


def build_fixed_messages(data, max_dim=DEFAULT_MAX_DIM):
    """Fixed prefix: system instructions + few-shot examples (each with its
    sampled page images) as user/assistant turns. Identical across every
    call in this run. NOTE: unlike the text-only doc_type/run_eval.py, this
    prefix does NOT appear to get cheap KV-cache reuse across separate
    /api/chat calls in practice -- prompt_eval time stayed ~30-46s across
    consecutive calls with near-identical prompt_eval_count instead of
    dropping after the first call, so the ~51 few-shot images are most
    likely being reprocessed by the vision encoder on every single call.
    Only the final user turn (the record to classify) varies per call."""
    messages = [
        {"role": "system", "content": build_system_message(data["label_definitions"])}
    ]
    for ex in data["few_shot"]:
        messages.append({
            "role": "user",
            "content": format_prompt_text(ex),
            "images": [load_image_b64(p, max_dim) for p in ex["image_paths"]],
        })
        messages.append({"role": "assistant", "content": ex["target"]})
    return messages


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


def call_ollama(host, model, messages, num_ctx=None, timeout=300, think=False, num_predict=None):
    url = f"{host.rstrip('/')}/api/chat"
    options = {"temperature": 0.1}
    if num_ctx:
        options["num_ctx"] = num_ctx
    if num_predict:
        # Hard cap on generated tokens. Some vision models don't reliably emit
        # a stop token for this style of prompt and will otherwise run all the
        # way to num_ctx -- burning a couple minutes per call and, once in a
        # while, degenerating into a repetition loop that the response parser
        # can't handle (observed: HTTP 500 "does not match expected peg-native
        # format" after a call ran to the full context). Capping num_predict
        # forces a clean stop well before that happens.
        options["num_predict"] = num_predict
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": options,
        "think": think,
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
    timing = {
        "total_s": body.get("total_duration", 0) / 1e9,
        "load_s": body.get("load_duration", 0) / 1e9,
        "prompt_eval_s": body.get("prompt_eval_duration", 0) / 1e9,
        "prompt_eval_count": body.get("prompt_eval_count"),
        "eval_s": body.get("eval_duration", 0) / 1e9,
        "eval_count": body.get("eval_count"),
    }
    return body["message"]["content"], timing


def harmonic_mean(a, b):
    if a != a or b != b:  # nan check
        return float("nan")
    if (a + b) == 0:
        return 0.0
    return 2 * a * b / (a + b)


CSV_FIELDNAMES = ["id", "label", "root_label", "sampled_pages", "true_doc_type",
                   "predicted", "correct", "abstained", "raw_response"]


def load_existing_results(out_path):
    """Load previously-written rows keyed by id, e.g. for --resume. Tolerates
    a missing file (returns {})."""
    existing = {}
    if out_path.exists():
        with open(out_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing[int(row["id"])] = row
    return existing


def summarize(rows):
    """Compute the same completeness/accuracy/balanced-score summary from a
    list of result dicts (works whether they came from this run or were
    reloaded from a previous partial run)."""
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
    parser.add_argument("--model", default="qwen3-vl:8b")
    parser.add_argument("--host", default="http://localhost:11434")
    parser.add_argument("--data", default="eval_data.json")
    parser.add_argument("--out", default="results.csv")
    parser.add_argument("--limit", type=int, default=None,
                         help="only run the first N test cases (for a quick smoke test)")
    parser.add_argument("--max-dim", type=int, default=DEFAULT_MAX_DIM,
                         help="downscale images so their longest side is at most this "
                              "many pixels before sending (0 = send full resolution)")
    parser.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX,
                         help="Ollama context window size in tokens. The full "
                              "17-doc/3-image few-shot prefix plus one test case "
                              "needs ~43K tokens at --max-dim 1024, so this must "
                              "exceed that (0 = use Ollama's default, currently 32768 "
                              "on most installs -- too small for the default few-shot set)")
    parser.add_argument("--max-few-shot", type=int, default=None,
                         help="use only the first N few-shot examples instead of all 17 "
                              "-- a quick lever to shrink the prompt if num_ctx is too "
                              "memory-hungry for your hardware")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                         help="per-request timeout in seconds. A large num_ctx first "
                              "call (reallocating KV cache + processing ~50 few-shot "
                              "images) can legitimately take several minutes, especially "
                              "on CPU or a modest GPU.")
    parser.add_argument("--think", action="store_true", default=False,
                         help="allow the model's hidden chain-of-thought reasoning "
                              "(off by default -- qwen3-vl was observed spending 100+s "
                              "generating hidden reasoning tokens before a single-token "
                              "visible answer, which Ollama strips from message.content "
                              "anyway; disabling it is much faster with no observed "
                              "downside for this single-token task)")
    parser.add_argument("--resume", action="store_true", default=False,
                         help="skip test cases already present in --out from a previous "
                              "(possibly interrupted) run, and append new results to it "
                              "instead of overwriting")
    args = parser.parse_args()

    data = json.loads((HERE / args.data).read_text(encoding="utf-8"))
    if args.max_few_shot:
        data["few_shot"] = data["few_shot"][: args.max_few_shot]
    fixed_messages = build_fixed_messages(data, args.max_dim)
    test_cases = data["test_cases"]
    if args.limit:
        test_cases = test_cases[: args.limit]
    n = len(test_cases)

    out_path = HERE / args.out
    existing = load_existing_results(out_path) if args.resume else {}
    if existing:
        print(f"--resume: found {len(existing)} already-completed result(s) in {out_path}, "
              f"skipping those and appending new ones.\n")
        remaining = [rec for rec in test_cases if rec["id"] not in existing]
    else:
        remaining = test_cases

    print(f"Running {len(remaining)} test cases ({n - len(remaining)} already done) "
          f"against {args.model} at {args.host} "
          f"(k={data.get('k_page_images')} page images/doc, "
          f"{len(data['few_shot'])} few-shot examples, num_ctx={args.num_ctx}) ...\n")

    # Results are written to disk after EVERY call (not batched to the end),
    # so a crash, timeout, or Ctrl-C only loses the one in-flight call, not
    # the whole run. --resume then picks up from here on the next invocation.
    file_mode = "a" if (args.resume and out_path.exists()) else "w"
    out_f = open(out_path, file_mode, newline="", encoding="utf-8")
    writer = csv.DictWriter(out_f, fieldnames=CSV_FIELDNAMES)
    if file_mode == "w":
        writer.writeheader()
        out_f.flush()

    all_rows = list(existing.values())
    try:
        for i, rec in enumerate(remaining, 1):
            messages = fixed_messages + [{
                "role": "user",
                "content": format_prompt_text(rec),
                "images": [load_image_b64(p, args.max_dim) for p in rec["image_paths"]],
            }]
            raw, timing = call_ollama(args.host, args.model, messages, num_ctx=args.num_ctx,
                                       timeout=args.timeout, think=args.think)
            pred = parse_prediction(raw)
            truth = set(rec["doc_type"])
            abstained = pred == ABSTAIN
            correct = (not abstained) and (pred in truth)

            row = {
                "id": rec["id"],
                "label": rec["label"],
                "root_label": rec["root_label"],
                "sampled_pages": ",".join(str(p) for p in rec["sampled_pages"]),
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
            print(
                f"[{i:>3}/{len(remaining)}] {status}  {rec['label']:<30} "
                f"true={','.join(sorted(truth)):<8} pred={pred}"
                f"  (total={timing['total_s']:.1f}s load={timing['load_s']:.1f}s "
                f"prompt_eval={timing['prompt_eval_s']:.1f}s/{timing['prompt_eval_count']}tok "
                f"gen={timing['eval_s']:.1f}s/{timing['eval_count']}tok)"
            )
    finally:
        out_f.close()
        # Always print whatever summary we can, even on a crash/timeout/Ctrl-C,
        # so a partial run's results are visible immediately rather than lost.
        print_summary(summarize(all_rows))
        print(f"\nResults written to {out_path} ({len(all_rows)}/{n} total across all runs)")
        if len(all_rows) < n:
            print(f"Incomplete -- rerun with --resume to continue from here.")


if __name__ == "__main__":
    main()
