#!/usr/bin/env python3
"""
Run doc_type_with_text/run_eval.py on a Modal GPU.

This mirrors ../vision_doc_recognition/modal_extract_text.py's design --
same lessons learned there (peg-native /api/chat parser issues don't apply
here since qwen2.5:14b's template doesn't hit that bug on plain single-token
answers, but disconnects/workspace hiccups can still interrupt a run, so
this still runs off a persistent, periodically-committed Volume rather than
a throwaway scratch dir).

Setup (once), if you haven't already from the vision_doc_recognition task:
    pip install modal
    modal setup

This folder is tiny (eval_data.json is ~580KB, no page images needed since
extract_text.py already baked OCR text into the data) so there's no
separate staging step -- the whole folder just gets mounted directly.

Run:
    modal run --detach modal_run_eval.py

    --detach so a local disconnect can't cancel the remote job (this bit
    the vision extraction task more than once).

Model: qwen2.5:14b by default -- the SAME model as ../doc_type/run_eval.py's
baseline. Keeping the model identical to the metadata-only baseline is the
whole point: it isolates the effect of adding OCR text as a feature, rather
than confounding it with a different model's capability. Override with
--model only if you're deliberately running a different experiment.

results.csv (the original qwen2.5:14b baseline, no OCR-noise filtering --
that check didn't exist yet when it was generated) is preserved. Re-running
now that run_eval.py has --min-compression-ratio (default 0.20, filters out
degenerate repeated-token OCR garbage -- see run_eval.py's docstring)
writes to a new file instead, so both are available:

    modal run --detach modal_run_eval.py --out results_filtered.csv

If your client disconnects before the run finishes, pull whatever's been
committed so far from any terminal with:

    modal volume get jewishgen-doc-type-with-text-output results_filtered.csv .
"""
import subprocess
import time
from pathlib import Path
from typing import Optional

import modal

HERE = Path(__file__).parent

MODEL = "qwen2.5:14b"
COMMIT_INTERVAL_S = 20

app = modal.App("jewishgen-doc-type-with-text")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("curl", "zstd")
    .run_commands("curl -fsSL https://ollama.com/install.sh | sh")
    .add_local_dir(str(HERE), remote_path="/root/work", ignore=["modal_staging", "__pycache__"])
)

# Reuses the same model-cache volume as the vision extraction task -- it's
# just a blob store keyed by model digest, so qwen2.5:14b lands alongside
# minicpm-v4.5 there without conflict, and either task benefits from the
# other's warm cache.
model_cache = modal.Volume.from_name("ollama-model-cache", create_if_missing=True)
output_vol = modal.Volume.from_name("jewishgen-doc-type-with-text-output", create_if_missing=True)


@app.function(
    image=image,
    gpu="A10G",
    timeout=6 * 60 * 60,
    volumes={"/root/.ollama": model_cache, "/root/output": output_vol},
)
def run_classification(model: str = MODEL, max_text_chars: int = 1500,
                        min_compression_ratio: float = 0.20,
                        out_name: str = "results.csv") -> bytes:
    import csv
    import os
    import shutil
    import threading
    import urllib.request

    persist = Path("/root/output")
    persist.mkdir(exist_ok=True)

    # Scripts + eval_data.json: always take the freshly-staged copy in case
    # you edited them since the last run.
    for name in ["run_eval.py", "eval_data.json", "build_eval_data.py"]:
        src = Path("/root/work") / name
        if src.exists():
            shutil.copy2(src, persist / name)

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

    subprocess.run(["ollama", "pull", model], check=True)

    stop_committing = threading.Event()

    def committer():
        while not stop_committing.wait(COMMIT_INTERVAL_S):
            output_vol.commit()

    t = threading.Thread(target=committer, daemon=True)
    t.start()

    result = subprocess.run(
        ["python3", "run_eval.py", "--model", model,
         "--host", "http://localhost:11434", "--resume",
         "--max-text-chars", str(max_text_chars),
         "--min-compression-ratio", str(min_compression_ratio),
         "--out", out_name],
    )
    if result.returncode != 0:
        print(f"WARNING: run_eval.py exited with code {result.returncode} "
              f"-- returning whatever partial progress was written. "
              f"Re-run this script (it will --resume) to pick up where it left off.")

    stop_committing.set()
    server.terminate()
    output_vol.commit()

    return kept.read_bytes()


@app.local_entrypoint()
def main(model: str = MODEL, max_text_chars: int = 1500,
         min_compression_ratio: float = 0.20, out: str = "results.csv"):
    result = run_classification.remote(
        model=model, max_text_chars=max_text_chars,
        min_compression_ratio=min_compression_ratio, out_name=out,
    )
    out_path = HERE / out
    out_path.write_bytes(result)
    print(f"Wrote {out_path} ({len(result)} bytes)")
