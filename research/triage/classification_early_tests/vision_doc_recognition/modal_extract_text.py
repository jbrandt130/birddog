#!/usr/bin/env python3
"""
Run vision_doc_recognition/extract_text.py on a Modal GPU instead of on the
MacBook, where the minicpm-v4.5 vision model has been too slow (only 8/233
docs done, several calls timing out).

Setup (once):
    pip install modal
    modal setup                        # opens a browser to authenticate
    python3 prepare_modal_staging.py   # stages the ~4GB of needed files
                                        # (only the specific page images
                                        # eval_data.json references, not
                                        # the full 112GB docs/+pages/ trees)

Run:
    modal run --detach modal_extract_text.py

    --detach matters: without it, closing your laptop, losing wifi, or
    Ctrl+C'ing your terminal cancels the remote job outright (this has
    already happened once). With --detach, the job keeps running on
    Modal's infrastructure even if your local client disconnects.

What it does:
    - Spins up a GPU container (A10G, 24GB -- plenty for an 8B vision model)
    - Installs Ollama, pulls minicpm-v4.5 (cached in a persistent Modal
      Volume after the first run, so repeat runs skip the ~5GB download)
    - Runs extract_text.py --resume directly inside a second persistent
      Volume (not a throwaway scratch dir), and commits that volume every
      20s while it runs -- so progress survives even if the container
      itself is killed mid-run, not just a clean exit
    - Streams extract_text.py's own progress output back to your terminal
    - Writes the resulting extracted_text.json back into this directory
      once the run completes and your client is still attached

If your client disconnects (or you used --detach and closed the terminal),
the local write-back at the end won't happen automatically. Pull whatever
progress has been committed at any time, from any terminal, with:

    modal volume get jewishgen-extraction-output extracted_text.json .

That works regardless of whether the job already finished, is still
running, or got cancelled -- it just reads the volume's current state.

Re-run prepare_modal_staging.py + this script again later if you add more
sampled documents to eval_data.json.
"""
import subprocess
import time
from pathlib import Path

import modal

HERE = Path(__file__).parent
STAGING = HERE / "modal_staging"

MODEL = "minicpm-v4.5"
COMMIT_INTERVAL_S = 20

app = modal.App("jewishgen-text-extraction")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("curl", "zstd")
    .run_commands("curl -fsSL https://ollama.com/install.sh | sh")
    .pip_install("pillow")
    .add_local_dir(str(STAGING), remote_path="/root/work")
)

model_cache = modal.Volume.from_name("ollama-model-cache", create_if_missing=True)
output_vol = modal.Volume.from_name("jewishgen-extraction-output", create_if_missing=True)


@app.function(
    image=image,
    gpu="A10G",
    timeout=6 * 60 * 60,  # generous ceiling; expected actual runtime is much shorter
    volumes={"/root/.ollama": model_cache, "/root/output": output_vol},
)
def run_extraction() -> bytes:
    import json
    import os
    import shutil
    import threading
    import urllib.request

    persist = Path("/root/output")
    persist.mkdir(exist_ok=True)

    # Scripts + eval_data.json: always take the freshly-staged copy (in case
    # you edited eval_data.json or the scripts since the last run).
    for name in ["extract_text.py", "run_eval.py", "eval_data.json"]:
        shutil.copy2(f"/root/work/{name}", persist / name)

    # Page images: copy once. They don't change between runs and re-copying
    # ~4GB every time is wasted work once the volume already has them.
    if not (persist / "pages").exists():
        shutil.copytree("/root/work/pages", persist / "pages")

    # extracted_text.json: keep whichever copy has more completed documents
    # -- the volume may be further along than what's staged locally if a
    # previous run got cut off before its results made it back to your
    # laptop.
    staged = Path("/root/work/extracted_text.json")
    kept = persist / "extracted_text.json"

    def doc_count(p):
        return len(json.loads(p.read_text())) if p.exists() else 0

    if doc_count(staged) > doc_count(kept):
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

    # extract_text.py rewrites extracted_text.json after every single
    # document, but a Modal Volume only durably persists writes when
    # committed. Commit periodically in the background so a hard kill
    # (disconnect, OOM, preemption) loses at most ~COMMIT_INTERVAL_S
    # seconds of progress instead of the whole run.
    stop_committing = threading.Event()

    def committer():
        while not stop_committing.wait(COMMIT_INTERVAL_S):
            output_vol.commit()

    t = threading.Thread(target=committer, daemon=True)
    t.start()

    result = subprocess.run(
        ["python3", "extract_text.py", "--model", MODEL,
         "--host", "http://localhost:11434", "--resume"],
    )
    if result.returncode != 0:
        print(f"WARNING: extract_text.py exited with code {result.returncode} "
              f"-- returning whatever partial progress was written. "
              f"Re-run this script (it will --resume) to pick up where it left off.")

    stop_committing.set()
    server.terminate()
    output_vol.commit()

    return kept.read_bytes()


@app.local_entrypoint()
def main():
    if not STAGING.exists():
        raise SystemExit(
            "modal_staging/ not found -- run `python3 prepare_modal_staging.py` first."
        )
    result = run_extraction.remote()
    out_path = HERE / "extracted_text.json"
    out_path.write_bytes(result)
    print(f"Wrote {out_path} ({len(result)} bytes)")
