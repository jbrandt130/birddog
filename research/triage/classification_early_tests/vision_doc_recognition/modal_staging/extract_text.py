#!/usr/bin/env python3
"""
Extract legible PRINTED text fragments from sampled page images using a
local Ollama vision model (default: minicpm-v4.5, chosen for its strong
OCRBench performance on dense/printed text).

This is a text-extraction step, not classification -- it's the first half
of testing whether OCR'd printed-form text (letterhead, field labels,
stamps, captions) can augment the metadata-only doc_type classifier
(../doc_type/run_eval.py) with real signal, without needing full
handwriting transcription. Handwriting is explicitly out of scope: the
prompt asks the model to transcribe only clearly-legible PRINTED text and
skip handwritten content, since earlier vision-only classification results
showed this class of model doesn't reliably read handwriting anyway.

Unlike run_eval.py's classification prompt, there's no few-shot prefix here
-- transcription doesn't benefit from example conversation turns the way
few-shot classification does, so each call is just a system instruction +
this document's k sampled images. That keeps each call cheap (a few
thousand tokens, not ~42K), so this should run much faster than the
classification eval.

Usage:
    python3 extract_text.py [--model minicpm-v4.5] [--host http://localhost:11434]

Output:
    extracted_text.json -- {doc_id: {"label":..., "extracted_text": "...",
                             "image_paths": [...]}, ...}
    Written incrementally after every call, and --resume skips ids already
    present, for the same reason run_eval.py needed that: a stalled/timed-out
    call shouldn't cost you the whole run.

Requires Pillow: pip install pillow
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from run_eval import load_image_b64, DEFAULT_MAX_DIM, DEFAULT_NUM_CTX, DEFAULT_TIMEOUT

HERE = Path(__file__).parent

SYSTEM_PROMPT = (
    "You will be shown several page images from a single archival document "
    "(Ukrainian/Russian/Polish archives, various historical periods). "
    "Transcribe ONLY clearly legible PRINTED text -- letterhead, form field "
    "labels/captions, printed headers, stamps, typed text. "
    "Do NOT attempt to transcribe handwritten text -- skip it entirely, even "
    "partially. Do not guess at illegible or ambiguous text.\n\n"
    "Output plain text fragments, one per line, in the order they appear. "
    "If a fragment is a form label or caption, keep it short (as printed, not "
    "paraphrased). If there is no legible printed text at all across every "
    "image shown, output exactly: NONE\n\n"
    "Do not add commentary, explanations, or descriptions of the images "
    "themselves -- only the transcribed printed text fragments."
)


def call_ollama_generate(host, model, system, prompt, images_b64, num_ctx=None,
                          num_predict=None, timeout=300, think=False):
    """Uses /api/generate instead of /api/chat.

    /api/chat on this Ollama version routes minicpm-v4.5 through a strict
    "peg-native" chat-template parser (built for tool-calling output) that
    hard-fails with HTTP 500 ("does not match the expected peg-native
    format") on plain narrative text -- observed even on short, clean,
    non-repeating responses, so it's not a length/degeneration issue.
    /api/generate returns the model's raw generated text with no such
    parsing layer, so it can't hit that bug."""
    url = f"{host.rstrip('/')}/api/generate"
    options = {"temperature": 0.1}
    if num_ctx:
        options["num_ctx"] = num_ctx
    if num_predict:
        options["num_predict"] = num_predict
    payload = {
        "model": model,
        "system": system,
        "prompt": prompt,
        "images": images_b64,
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
    return body["response"], timing


def build_generate_inputs(image_paths, max_dim):
    prompt = f"Transcribe the legible printed text from these {len(image_paths)} page images."
    images_b64 = [load_image_b64(p, max_dim) for p in image_paths]
    return prompt, images_b64


def load_existing(out_path):
    if out_path.exists():
        return json.loads(out_path.read_text(encoding="utf-8"))
    return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="minicpm-v4.5")
    parser.add_argument("--host", default="http://localhost:11434")
    parser.add_argument("--data", default="eval_data.json",
                         help="source of documents + sampled image_paths (reuses the "
                              "same k=3 page samples as the vision classification eval)")
    parser.add_argument("--out", default="extracted_text.json")
    parser.add_argument("--limit", type=int, default=None,
                         help="only process the first N documents (smoke test)")
    parser.add_argument("--max-dim", type=int, default=DEFAULT_MAX_DIM)
    parser.add_argument("--num-ctx", type=int, default=8192,
                         help="much smaller than run_eval.py's 65536 -- no few-shot "
                              "prefix here, just a system prompt + k images")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--think", action="store_true", default=False)
    parser.add_argument("--max-tokens", type=int, default=1024,
                         help="hard cap on generated tokens (num_predict). This model "
                              "doesn't reliably emit a stop token for this prompt and "
                              "will otherwise run to the full num_ctx on every call "
                              "(~150-190s each) and occasionally degenerate into a "
                              "repetition loop that crashes the call entirely. 1024 is "
                              "generous for a few page images' worth of printed text.")
    parser.add_argument("--resume", action="store_true", default=False)
    args = parser.parse_args()

    data = json.loads((HERE / args.data).read_text(encoding="utf-8"))
    docs = data["few_shot"] + data["test_cases"]
    if args.limit:
        docs = docs[: args.limit]
    n = len(docs)

    out_path = HERE / args.out
    results = load_existing(out_path) if args.resume else {}
    if results:
        print(f"--resume: {len(results)} documents already done, skipping those.\n")

    remaining = [d for d in docs if str(d["id"]) not in results]
    print(f"Extracting printed text from {len(remaining)} documents "
          f"({n - len(remaining)} already done) using {args.model} at {args.host} ...\n")

    try:
        for i, rec in enumerate(remaining, 1):
            prompt, images_b64 = build_generate_inputs(rec["image_paths"], args.max_dim)
            raw, timing = call_ollama_generate(args.host, args.model, SYSTEM_PROMPT, prompt,
                                                images_b64, num_ctx=args.num_ctx,
                                                timeout=args.timeout, think=args.think,
                                                num_predict=args.max_tokens)
            text = raw.strip()
            results[str(rec["id"])] = {
                "label": rec["label"],
                "root_label": rec["root_label"],
                "sampled_pages": rec["sampled_pages"],
                "image_paths": rec["image_paths"],
                "extracted_text": text,
            }
            out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

            preview = text.replace("\n", " | ")[:80]
            print(f"[{i:>3}/{len(remaining)}] {rec['label']:<30} "
                  f"(total={timing['total_s']:.1f}s prompt_eval={timing['prompt_eval_s']:.1f}s "
                  f"gen={timing['eval_s']:.1f}s/{timing['eval_count']}tok)  -> {preview!r}")
    finally:
        print(f"\n{len(results)}/{n} documents extracted, written to {out_path}")
        if len(results) < n:
            print("Incomplete -- rerun with --resume to continue from here.")


if __name__ == "__main__":
    main()
