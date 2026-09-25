#!/usr/bin/env python3
"""
Extend extracted_text.json to cover the 193 additional p1_p2_no documents
that have rendered page images but weren't part of the original 233-doc
doc_type/doc_type_with_text extraction run.

This is the same job as modal_extract_text.py (same lessons learned: durable
Volume + periodic commit + --resume so disconnects/preemption don't lose
progress, --detach so a local disconnect can't cancel the remote job), just
pointed at a different, smaller --data file (eval_data_p1_p2_no_extra.json,
193 docs instead of 233) and reusing the SAME output volume so the result is
one unified extracted_text.json (426 entries when done) rather than a second,
fragmented file.

Setup (once), if you haven't already from the doc_type extraction task:
    pip install modal
    modal setup
    python3 build_p1_p2_no_extraction_data.py     # writes eval_data_p1_p2_no_extra.json
    python3 prepare_modal_staging_p1_p2_no.py      # stages the ~193*3 new page images

Run:
    modal run --detach modal_extract_text_p1_p2_no.py

    --detach matters: without it, closing your laptop, losing wifi, or
    Ctrl+C'ing your terminal cancels the remote job outright.

What it does:
    - Spins up a GPU container (A10G) with Ollama + minicpm-v4.5 (cached in
      the same ollama-model-cache Volume the original extraction used, so no
      re-download)
    - Seeds /root/output from whichever of {staged extracted_text.json,
      volume's extracted_text.json} has more completed documents, so a
      previous partial run of THIS job is picked back up correctly
    - Copies over any new page-image directories (the 193 new docs') that
      aren't already on the volume -- the original 699 images from the
      first extraction run are left alone, not re-copied
    - Runs extract_text.py --data eval_data_p1_p2_no_extra.json --resume,
      which skips any id already present in extracted_text.json (so the 72
      p1_p2_no docs that already overlapped with the original 233 are
      naturally skipped, along with any of the 193 already done in an
      earlier attempt at this job)
    - Commits the output volume every 20s while running
    - Writes the resulting extracted_text.json back into this directory

If your client disconnects, pull whatever's been committed so far from any
terminal with:

    modal volume get jewishgen-extraction-output extracted_text.json .

That's the SAME volume the original doc_type extraction wrote to -- this
job extends it in place rather than creating a second output volume.
"""
import subprocess
import time
from pathlib import Path

import modal

HERE = Path(__file__).parent
STAGING = HERE / "modal_staging_p1_p2_no"
DATA_FILE = "eval_data_p1_p2_no_extra.json"

MODEL = "minicpm-v4.5"
COMMIT_INTERVAL_S = 20

app = modal.App("jewishgen-text-extraction-p1p2no")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("curl", "zstd")
    .run_commands("curl -fsSL https://ollama.com/install.sh | sh")
    .pip_install("pillow")
    .add_local_dir(str(STAGING), remote_path="/root/work")
)

# Same volumes as modal_extract_text.py -- model_cache is a shared blob
# store keyed by model digest, and output_vol is the SAME extracted_text.json
# artifact, extended in place rather than forked.
model_cache = modal.Volume.from_name("ollama-model-cache", create_if_missing=True)
output_vol = modal.Volume.from_name("jewishgen-extraction-output", create_if_missing=True)


@app.function(
    image=image,
    gpu="A10G",
    timeout=6 * 60 * 60,
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

    # Scripts + the new data file: always take the freshly-staged copy.
    for name in ["extract_text.py", "run_eval.py", DATA_FILE]:
        shutil.copy2(f"/root/work/{name}", persist / name)

    # Page images: copy only directories not already on the volume. The
    # original 699 images (233 docs) are already there from the first
    # extraction run -- don't re-copy them. Any of the 193 new docs already
    # committed from an earlier attempt at *this* job are also skipped.
    work_pages = Path("/root/work/pages")
    persist_pages = persist / "pages"
    persist_pages.mkdir(exist_ok=True)
    n_copied = 0
    if work_pages.exists():
        for d in work_pages.iterdir():
            if d.is_dir():
                dst = persist_pages / d.name
                if not dst.exists():
                    shutil.copytree(d, dst)
                    n_copied += 1
    print(f"Copied {n_copied} new page-image directories to the persistent volume")

    # extracted_text.json: keep whichever copy has more completed documents
    # -- the volume may be further along than what's staged locally if a
    # previous run of this job got cut off before its results made it back.
    staged = Path("/root/work/extracted_text.json")
    kept = persist / "extracted_text.json"

    def doc_count(p):
        return len(json.loads(p.read_text())) if p.exists() else 0

    if doc_count(staged) > doc_count(kept):
        shutil.copy2(staged, kept)

    print(f"Starting with {doc_count(kept)} documents already in extracted_text.json")

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
        ["python3", "extract_text.py", "--model", MODEL,
         "--host", "http://localhost:11434", "--data", DATA_FILE, "--resume"],
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
            "modal_staging_p1_p2_no/ not found -- run "
            "`python3 prepare_modal_staging_p1_p2_no.py` first."
        )
    result = run_extraction.remote()
    out_path = HERE / "extracted_text.json"
    out_path.write_bytes(result)
    print(f"Wrote {out_path} ({len(result)} bytes)")


if __name__ == "__main__":
    pass
