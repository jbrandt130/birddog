#!/usr/bin/env python3
"""
Estimate personal-name density from sampled middle-page images using a
local Ollama vision model.

THE QUESTION ASKED OF THE MODEL is deliberately a LAYOUT question, not a
transcription or judgment one: "what kind of page is this, and how many
person-entries are on it?" A model that cannot read Cyrillic cursive can
still see that a page is a ruled register with ~30 name rows. This is why
this attempt is expected to succeed where the two prior vision attempts
did not -- direct doc_type classification from images (45/216) asked for a
judgment call, and OCR extraction asked for handwriting transcription and
explicitly skipped it, which is where the names live.

ONE CALL PER PAGE, not per document. Counting is inherently per-page, and
batching 5 pages into one call invites the model to conflate them into a
single blurred estimate. Per-page calls also yield a WITHIN-DOCUMENT
VARIANCE, which is real signal for cost-benefit ranking: a uniformly dense
register (low variance) scales its yield with page_count, while a case
file with one list buried in it (high variance) does not.

OUTPUT IS STRUCTURED JSON per page, parsed defensively -- vision models
routinely wrap JSON in prose or code fences. Unparseable responses are
recorded with page_type="parse_error" rather than dropped, so the failure
rate is visible in the summary instead of silently shrinking the sample.

/api/generate, not /api/chat: on this Ollama version minicpm-v4.5's chat
template routes /api/chat through a strict "peg-native" parser that
hard-failed (HTTP 500) on plain narrative text in
../vision_doc_recognition/extract_text.py, short or long. /api/generate
has no such parsing layer.

NO GROUND TRUTH YET: there are no hand-counted name densities, so this
script MEASURES rather than scores. Validation is indirect until ~30 pages
are hand-counted -- see analyze_density.py, which checks whether estimated
density separates the P1/P2/NO priority labels and tracks doc_type=L.
Few-shot examples are supported (--few-shot-file) for once hand-counted
calibration examples exist; they must come from the TRAIN split. Default
is zero-shot.

Usage:
    python3 extract_density.py --split validation [--resume]
    python3 extract_density.py --split train        # prompt development

Output:
    density_<split>.json  -- per-page records, written incrementally
    Summary printed to stdout. Use analyze_density.py to aggregate.

Requires Pillow: pip install pillow
"""

import argparse
import base64
import io
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

Image.MAX_IMAGE_PIXELS = None  # legitimate large historical scans

HERE = Path(__file__).parent


def _resolve_image_root():
    """Root that record['image_paths'] ("pages/<id>/<n>.jpg") resolve against.

    LOCAL: images live in ../data/pages/. MODAL CONTAINER: only the
    referenced JPGs are staged, landing in a "pages/" dir next to this
    script. Prefer the sibling when it exists.
    """
    import os
    env = os.environ.get("JEWISHGEN_DATA_ROOT")
    if env:
        return Path(env)
    if (HERE / "pages").is_dir():
        return HERE
    return HERE.parent / "data"


DATA_ROOT = _resolve_image_root()

DEFAULT_MAX_DIM = 1400  # a bit larger than the OCR pass: counting rows in a
                        # dense register needs the rows to stay resolvable
JPEG_QUALITY = 85
DEFAULT_NUM_CTX = 8192
DEFAULT_TIMEOUT = 300
# Hard cap: this model doesn't reliably emit a stop token and will otherwise
# run to full num_ctx on every call, occasionally degenerating into a
# repetition loop that crashes the request outright (learned the hard way in
# ../vision_doc_recognition/extract_text.py). A JSON object this small needs
# nowhere near 256 tokens.
DEFAULT_MAX_TOKENS = 256

PAGE_TYPES = [
    "register_table",   # ruled/tabular register, one row per person
    "name_list",        # list of people, not necessarily ruled
    "form",             # per-person form/application (often 1 person/page)
    "prose",            # narrative/correspondence text
    "index",            # alphabetical index or finding aid
    "cover_or_title",   # cover, title leaf, administrative front matter
    "blank_or_illegible",
    "other",
]

SYSTEM_PROMPT = (
    "You are analysing a single page image from an archival document "
    "(Ukrainian/Russian/Polish archives, various historical periods). Your "
    "job is to judge the page's LAYOUT and COUNT person-entries. You do NOT "
    "need to read the handwriting, and you should not try to transcribe "
    "names -- judge structure, not content.\n\n"
    "Classify the page as exactly one of:\n"
    "  register_table   - a ruled or tabular register, typically one row per person\n"
    "  name_list        - a list of people, not necessarily ruled\n"
    "  form             - a per-person form or application (often one person per page)\n"
    "  prose            - narrative text, correspondence, reports\n"
    "  index            - an alphabetical index or finding aid\n"
    "  cover_or_title   - cover, title leaf, or administrative front matter\n"
    "  blank_or_illegible - blank, too damaged, or unreadable\n"
    "  other            - none of the above\n\n"
    "Then give TWO separate counts.\n\n"
    "filled_entries: how many record rows/entries are actually FILLED IN with "
    "content on this image. Count only completed entries. A pre-printed but "
    "blank register form has 0 filled entries no matter how many ruled rows "
    "it has. A birth register with four completed birth records has 4.\n\n"
    "named_individuals: how many DISTINCT PEOPLE are named in total on this "
    "image, counting every person mentioned separately. A single birth record "
    "naming the child, the father and the mother contributes 3, not 1. Count "
    "people named anywhere -- in table rows, in narrative text, in margins. "
    "You do NOT need to read or transcribe the names to count them; count the "
    "positions where a personal name appears, even if the handwriting is "
    "illegible to you. If nobody is named, answer 0.\n\n"
    "Both counts may be approximate -- estimate to the nearest few. An "
    "approximate number is expected and useful; a refusal is not.\n\n"
    "NOTE: many scans show a two-page spread. Count across the whole image.\n\n"
    "FINALLY, report two VISUAL signals of Jewish content. These are "
    "recognition tasks, not reading tasks -- you are identifying which "
    "alphabet is on the page and whether certain printed words appear, not "
    "interpreting meaning.\n\n"
    "hebrew_script: true if ANY Hebrew or Yiddish text appears anywhere on "
    "the page. Hebrew script is visually unmistakable and looks nothing like "
    "Cyrillic or Latin: square block letters, many with flat horizontal tops "
    "and descending left strokes, written RIGHT-TO-LEFT, with no capital "
    "letters and few ascenders or descenders. It often appears as a parallel "
    "column, a facing half of a bilingual register, a heading, or a signature. "
    "Report true even if you cannot read it. Report false if the page is "
    "entirely Cyrillic and/or Latin.\n\n"
    "jewish_markers: true if PRINTED text on the page names a Jewish "
    "institution or category -- for example Еврейск- (Jewish), синагог- "
    "(synagogue), раввин (rabbi), иуде- (Judaic), or the equivalent in Polish "
    "or German (Judisch-, Zydowsk-). Judge printed/typeset text and stamps; "
    "do not guess from handwriting.\n\n"
    "IMPORTANT: absence of these signals is only WEAK evidence. Many records "
    "about Jewish people -- especially Soviet-era civil registration -- are "
    "written entirely in Cyrillic with no Hebrew and no such printed labels. "
    "So report false when you do not see them, but do not treat false as "
    "proof the document is not Jewish.\n\n"
    "Respond with ONLY a JSON object, no other text, no code fences:\n"
    '{"page_type": "<one of the types above>", "filled_entries": <integer>, '
    '"named_individuals": <integer>, "hebrew_script": <true|false>, '
    '"jewish_markers": <true|false>}'
)

USER_PROMPT = ("Analyse this page. Respond with only the JSON object.")


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


def call_ollama_generate(host, model, system, prompt, images_b64,
                          num_ctx=DEFAULT_NUM_CTX, num_predict=DEFAULT_MAX_TOKENS,
                          timeout=DEFAULT_TIMEOUT):
    url = f"{host.rstrip('/')}/api/generate"
    payload = {
        "model": model,
        "system": system,
        "prompt": prompt,
        "images": images_b64,
        "stream": False,
        "options": {"temperature": 0.1, "num_ctx": num_ctx,
                     "num_predict": num_predict},
        "think": False,
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
        return None
    except urllib.error.URLError as e:
        print(f"ERROR calling Ollama at {url}: {e}", file=sys.stderr)
        print("Is `ollama serve` running, and is the model pulled?", file=sys.stderr)
        sys.exit(1)
    return body.get("response", "")


def parse_density_json(raw):
    """Pull the JSON object out of a model response. Vision models wrap JSON
    in prose or code fences often enough that strict json.loads on the whole
    response would throw away otherwise-good answers."""
    empty = {"page_type": "parse_error", "filled_entries": None,
             "named_individuals": None, "hebrew_script": None,
             "jewish_markers": None}
    if not raw:
        return dict(empty)
    text = raw.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    match = re.search(r"\{.*?\}", text, flags=re.DOTALL)
    if not match:
        return dict(empty)
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return dict(empty)

    page_type = str(obj.get("page_type", "")).strip().lower()
    if page_type not in PAGE_TYPES:
        page_type = "other"

    def as_int(v):
        if v is None:
            return None
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            return None
        return max(0, n)

    def as_bool(v):
        """Tolerate the several ways a model expresses a boolean."""
        if isinstance(v, bool):
            return v
        if v is None:
            return None
        s = str(v).strip().lower()
        if s in ("true", "yes", "1", "present"):
            return True
        if s in ("false", "no", "0", "none", "absent"):
            return False
        return None

    return {
        "page_type": page_type,
        # filled_entries = record rows actually completed (each row is one
        # subject person). named_individuals = every person named on the
        # image, so a birth row naming child + father + mother contributes 3.
        # Confirmed against a hand-checked spread (4 entries / ~12 named):
        # the model tracks named_individuals well and ignored an earlier
        # prompt that asked for rows, so both are now requested explicitly.
        "filled_entries": as_int(obj.get("filled_entries")),
        "named_individuals": as_int(obj.get("named_individuals")),
        # Visual Jewish-content signals. High precision / low recall by
        # nature: pre-1917 Jewish metric books are bilingual and light up,
        # Soviet-era ZAGS records of Jewish individuals are pure Cyrillic and
        # will not. Absence is weak evidence -- see analyze_jewish_signals.py.
        "hebrew_script": as_bool(obj.get("hebrew_script")),
        "jewish_markers": as_bool(obj.get("jewish_markers")),
    }


def build_few_shot(path):
    """Optional hand-counted calibration examples, which MUST come from the
    train split (validation is the inference set here). Not available yet --
    see the module docstring."""
    if not path:
        return []
    examples = json.loads(Path(path).read_text(encoding="utf-8"))
    for ex in examples:
        if ex.get("split") not in (None, "train"):
            raise SystemExit(
                f"few-shot example {ex.get('image_path')!r} is from split "
                f"{ex.get('split')!r} -- few-shot must come from train."
            )
    return examples


def load_existing(out_path):
    if out_path.exists():
        return json.loads(out_path.read_text(encoding="utf-8"))
    return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="minicpm-v4.5")
    parser.add_argument("--host", default="http://localhost:11434")
    parser.add_argument("--data", default="eval_data.json")
    parser.add_argument("--split", choices=["train", "validation"],
                         default="validation",
                         help="validation is the inference set; train is for "
                              "prompt development only")
    parser.add_argument("--out", default=None,
                         help="default: density_<split>.json")
    parser.add_argument("--few-shot-file", default=None,
                         help="optional hand-counted calibration examples "
                              "(train split only). Default: zero-shot.")
    parser.add_argument("--limit", type=int, default=None,
                         help="only process the first N documents (smoke test)")
    parser.add_argument("--max-dim", type=int, default=DEFAULT_MAX_DIM)
    parser.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--resume", action="store_true", default=False)
    args = parser.parse_args()

    data = json.loads((HERE / args.data).read_text(encoding="utf-8"))
    docs = data[args.split]
    if args.limit and args.limit < len(docs):
        # Stride-sample, don't take the first N. The validation set is ordered
        # by priority label, so docs[:N] would be all P1 and nearly all
        # doc_type V from a single archive -- worthless as a sanity check.
        # Same approach as ../doc_type_with_text/run_eval.py.
        stride = len(docs) / args.limit
        docs = [docs[int(i * stride)] for i in range(args.limit)]

    few_shot = build_few_shot(args.few_shot_file)
    if few_shot:
        print(f"Using {len(few_shot)} hand-counted few-shot example(s) from train.\n")

    out_path = HERE / (args.out or f"density_{args.split}.json")
    results = load_existing(out_path) if args.resume else {}
    if results:
        n_pages_done = sum(len(v["pages"]) for v in results.values())
        print(f"--resume: {len(results)} documents ({n_pages_done} pages) "
              f"already done, skipping those.\n")

    remaining = [d for d in docs if str(d["id"]) not in results]
    total_pages = sum(len(d["image_paths"]) for d in remaining)
    print(f"Estimating name density for {len(remaining)} documents "
          f"({total_pages} page images, one call each) using {args.model} "
          f"at {args.host} -- split={args.split}\n")

    n_parse_errors = 0
    try:
        for i, rec in enumerate(remaining, 1):
            pages = []
            for rel, page_no in zip(rec["image_paths"], rec["sampled_pages"]):
                images_b64 = [load_image_b64(rel, args.max_dim)]
                raw = call_ollama_generate(
                    args.host, args.model, SYSTEM_PROMPT, USER_PROMPT, images_b64,
                    num_ctx=args.num_ctx, num_predict=args.max_tokens,
                    timeout=args.timeout,
                )
                parsed = parse_density_json(raw)
                if parsed["page_type"] == "parse_error":
                    n_parse_errors += 1
                parsed["page"] = page_no
                parsed["image_path"] = rel
                parsed["raw_response"] = (raw or "").strip().replace("\n", " ")[:300]
                pages.append(parsed)

            counts = [p["named_individuals"] for p in pages
                      if p["named_individuals"] is not None]
            results[str(rec["id"])] = {
                "label": rec["label"],
                "root_label": rec["root_label"],
                "doc_type": rec["doc_type"],
                "page_count": rec["page_count"],
                "priority_target": rec.get("priority_target"),
                "pages": pages,
            }
            out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False),
                                 encoding="utf-8")

            mean = (sum(counts) / len(counts)) if counts else float("nan")
            n_heb = sum(1 for p in pages if p.get("hebrew_script"))
            n_mark = sum(1 for p in pages if p.get("jewish_markers"))
            flag = ""
            if n_heb:
                flag += f" HEB={n_heb}/{len(pages)}"
            if n_mark:
                flag += f" MARK={n_mark}/{len(pages)}"
            print(f"[{i:>3}/{len(remaining)}] {rec['label']:<26} "
                  f"pages={len(pages)} names/pg={mean:>6.1f} "
                  f"est={mean * rec['page_count']:>7.0f}{flag}")
    finally:
        print(f"\n{len(results)}/{len(docs)} documents done, written to {out_path}")
        if n_parse_errors:
            print(f"WARNING: {n_parse_errors} page response(s) could not be parsed "
                  f"as JSON (recorded as page_type=parse_error)")
        if len(results) < len(docs):
            print("Incomplete -- rerun with --resume to continue from here.")
        print("\nNext: python3 analyze_density.py --split " + args.split)


if __name__ == "__main__":
    main()
