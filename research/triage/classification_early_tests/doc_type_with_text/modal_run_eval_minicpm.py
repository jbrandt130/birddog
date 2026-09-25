#!/usr/bin/env python3
"""
Run this folder's run_eval.py on a Modal GPU with minicpm-v4.5 as the
classifier, for comparison against the qwen2.5:14b baseline in results.csv.

Uses --api generate rather than the default --api chat: on this Ollama
version, minicpm-v4.5's chat template routes /api/chat through a strict
"peg-native" parser that hard-failed (HTTP 500) in
../vision_doc_recognition/extract_text.py on plain narrative text, short or
long. A single-token classification answer might dodge that, but it isn't
proven safe, and /api/generate (no such parsing layer) is a cheap,
already-proven-reliable substitute.

Setup (once), if you haven't already from the earlier tasks:
    pip install modal
    modal setup

Run:
    modal run --detach modal_run_eval_minicpm.py

    --detach so a local disconnect can't cancel the remote job.

Writes to results_minicpm.csv -- the existing qwen2.5:14b results.csv is
never touched. See also ../doc_type/modal_run_eval_minicpm.py, the other
half of the model x with/without-text comparison.

Cheap diagnostic mode: the full 216-case run produced the exact same
one-token response ("V") for every single test case -- verified this isn't
a prompt-construction bug (the per-record suffix genuinely differs each
call). Checked against the actual OCR data: 7 of 17 few-shot examples (41%)
and 37 of 216 test cases are degenerate repetition garbage (a short
fragment repeated dozens of times, sometimes for 10K+ characters), not
real transcription -- run_eval.py's --min-compression-ratio (default 0.20,
now the default here too) filters those out via a zlib compression-ratio
check rather than showing the model garbage. To test cheaply on a handful
of cases before spending a full run:

    modal run modal_run_eval_minicpm.py --limit 12 --max-text-chars 300 --out results_minicpm_diag.csv

If predictions vary again, the noise-filtered/shorter prompt fixed it. If
it's still deterministically "V" even here, that's a genuine, reportable
finding about the model rather than an OCR-noise artifact.

If your client disconnects before it finishes, pull whatever's been
committed so far from any terminal with:

    modal volume get doc-type-with-text-minicpm-output results_minicpm.csv .
"""
import subprocess
import time
from pathlib import Path
from typing import Optional

import modal

HERE = Path(__file__).parent

MODEL = "minicpm-v4.5"
COMMIT_INTERVAL_S = 20

app = modal.App("jewishgen-doc-type-with-text-minicpm")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("curl", "zstd")
    .run_commands("curl -fsSL https://ollama.com/install.sh | sh")
    .add_local_dir(str(HERE), remote_path="/root/work",
                    ignore=["__pycache__", "results.csv", "modal_run_eval.py"])
)

# Same model-cache volume the other Modal runs in this project used --
# minicpm-v4.5 is already cached there from the OCR extraction task, so
# this shouldn't need to re-download it.
model_cache = modal.Volume.from_name("ollama-model-cache", create_if_missing=True)
output_vol = modal.Volume.from_name("doc-type-with-text-minicpm-output", create_if_missing=True)


@app.function(
    image=image,
    gpu="A10G",
    timeout=6 * 60 * 60,
    volumes={"/root/.ollama": model_cache, "/root/output": output_vol},
)
def run_classification(out_name: str = "results_minicpm.csv", num_ctx: int = 32768,
                        max_text_chars: int = 1500, min_compression_ratio: float = 0.20,
                        limit: Optional[int] = None, resume: bool = True) -> bytes:
    import csv
    import os
    import shutil
    import threading
    import urllib.request

    persist = Path("/root/output")
    persist.mkdir(exist_ok=True)

    for item in Path("/root/work").iterdir():
        if item.is_file():
            shutil.copy2(item, persist / item.name)

    def row_count(p):
        if not p.exists():
            return 0
        with open(p, newline="", encoding="utf-8") as f:
            return sum(1 for _ in csv.DictReader(f))

    staged = Path("/root/work") / out_name
    kept = persist / out_name
    if row_count(staged) > row_count(kept):
        shutil.copy2(staged, kept)

    os.chdir(persist)

    server = subprocess.Popen(["ollama", "serve"])
    for _ in range(60):
        try:
            urllib.request.urlopen("http://localhost:11434", timeout=2)
            break
        except Exception:
            time.sleep(1)
    else:
        raise RuntimeError("ollama serve never came up")

    subprocess.run(["ollama", "pull", MODEL], check=True)

    stop_committing = threading.Event()

    def committer():
        while not stop_committing.wait(COMMIT_INTERVAL_S):
            output_vol.commit()

    t = threading.Thread(target=committer, daemon=True)
    t.start()

    cmd = ["python3", "run_eval.py", "--model", MODEL, "--api", "generate",
           "--num-ctx", str(num_ctx), "--max-text-chars", str(max_text_chars),
           "--min-compression-ratio", str(min_compression_ratio),
           "--out", out_name]
    if resume:
        cmd.append("--resume")
    if limit:
        cmd += ["--limit", str(limit)]

    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"WARNING: run_eval.py exited with code {result.returncode} "
              f"-- returning whatever partial progress was written. "
              f"Re-run this script (it will --resume) to pick up where it left off.")

    stop_committing.set()
    server.terminate()
    output_vol.commit()

    return kept.read_bytes()


@app.local_entrypoint()
def main(out: str = "results_minicpm.csv", num_ctx: int = 32768,
         max_text_chars: int = 1500, min_compression_ratio: float = 0.20,
         limit: Optional[int] = None, resume: bool = True):
    result = run_classification.remote(
        out_name=out, num_ctx=num_ctx, max_text_chars=max_text_chars,
        min_compression_ratio=min_compression_ratio, limit=limit, resume=resume,
    )
    out_path = HERE / out
    out_path.write_bytes(result)
    print(f"Wrote {out_path} ({len(result)} bytes)")
