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
never touched. See also ../doc_type_with_text/modal_run_eval_minicpm.py,
the other half of the model x with/without-text comparison.

If your client disconnects before it finishes, pull whatever's been
committed so far from any terminal with:

    modal volume get doc-type-minicpm-output results_minicpm.csv .
"""
import subprocess
import time
from pathlib import Path

import modal

HERE = Path(__file__).parent

MODEL = "minicpm-v4.5"
COMMIT_INTERVAL_S = 20

app = modal.App("jewishgen-doc-type-minicpm")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("curl", "zstd")
    .run_commands("curl -fsSL https://ollama.com/install.sh | sh")
    .add_local_dir(str(HERE), remote_path="/root/work",
                    ignore=["__pycache__", "results.csv"])
)

# Same model-cache volume the other Modal runs in this project used --
# minicpm-v4.5 is already cached there from the OCR extraction task, so
# this shouldn't need to re-download it.
model_cache = modal.Volume.from_name("ollama-model-cache", create_if_missing=True)
output_vol = modal.Volume.from_name("doc-type-minicpm-output", create_if_missing=True)


@app.function(
    image=image,
    gpu="A10G",
    timeout=6 * 60 * 60,
    volumes={"/root/.ollama": model_cache, "/root/output": output_vol},
)
def run_classification() -> bytes:
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

    staged = Path("/root/work/results_minicpm.csv")
    kept = persist / "results_minicpm.csv"
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

    result = subprocess.run(
        ["python3", "run_eval.py", "--model", MODEL, "--api", "generate",
         "--num-ctx", "8192", "--out", "results_minicpm.csv", "--resume"],
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
def main():
    result = run_classification.remote()
    out_path = HERE / "results_minicpm.csv"
    out_path.write_bytes(result)
    print(f"Wrote {out_path} ({len(result)} bytes)")
