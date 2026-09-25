#!/usr/bin/env python3
"""
Run name_density/extract_density.py on a Modal GPU.

Same durable design as the other Modal runners in this project: a
persistent output Volume committed every 20s plus --resume, so a
disconnect, preemption or crash costs at most ~20s of progress rather than
the whole run; and the shared ollama-model-cache Volume, so minicpm-v4.5
(already cached from the OCR extraction task) isn't re-downloaded.

This job is more call-heavy than earlier ones: it makes ONE call per PAGE,
not per document (~5 calls per doc, ~1400 for the validation split), for
the reasons in extract_density.py's docstring -- per-page counting plus a
within-document variance. Each call is small (one image, a ~250-token JSON
answer), so it should still be quick, but --resume matters more than usual.

Setup (once), if you haven't already from the earlier tasks:
    pip install modal
    modal setup
    python3 build_eval_data.py          # needs the middle-page render done
    python3 prepare_modal_staging.py    # stages ~1400 page images

Run:
    modal run --detach modal_run_density.py                    # validation
    modal run --detach modal_run_density.py --split train      # prompt dev

    --detach so a local disconnect can't cancel the remote job.

If your client disconnects before it finishes, pull whatever's been
committed so far from any terminal with:

    modal volume get jewishgen-name-density-output density_validation.json .
"""
import subprocess
import time
from pathlib import Path
from typing import Optional

import modal

HERE = Path(__file__).parent
STAGING = HERE / "modal_staging"

MODEL = "minicpm-v4.5"
COMMIT_INTERVAL_S = 20

app = modal.App("jewishgen-name-density")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("curl", "zstd")
    .run_commands("curl -fsSL https://ollama.com/install.sh | sh")
    .pip_install("pillow")
    .add_local_dir(str(STAGING), remote_path="/root/work")
)

model_cache = modal.Volume.from_name("ollama-model-cache", create_if_missing=True)
output_vol = modal.Volume.from_name("jewishgen-name-density-output", create_if_missing=True)


@app.function(
    image=image,
    gpu="A10G",
    timeout=6 * 60 * 60,
    volumes={"/root/.ollama": model_cache, "/root/output": output_vol},
)
def run_density(model: str = MODEL, split: str = "validation",
                 limit: Optional[int] = None, max_dim: int = 1400,
                 out: Optional[str] = None) -> bytes:
    import json
    import os
    import shutil
    import threading
    import urllib.request

    persist = Path("/root/output")
    persist.mkdir(exist_ok=True)

    # --out lets a re-run write to a fresh file instead of resuming into the
    # previous one. Needed whenever the PROMPT or SCHEMA changes: --resume
    # keys on document id, so without a new filename a re-run would simply
    # skip every document already scored under the old prompt and do nothing.
    out_name = out or f"density_{split}.json"

    # Scripts + eval_data.json: always take the freshly-staged copy, in case
    # the prompt or the sampled page set changed since the last run.
    for name in ["extract_density.py", "analyze_density.py", "eval_data.json"]:
        src = Path("/root/work") / name
        if src.exists():
            shutil.copy2(src, persist / name)

    # Page images: sync PER FILE, not per directory. An earlier version
    # skipped any directory that already existed on the volume, which was
    # fine while every run used the same page sample -- but the k=20
    # systematic sample adds new pages to directories the k=5 pilot had
    # already created, so whole-directory skipping silently left the new
    # images uncopied and extraction died on the first missing file.
    # Per-file is a few thousand stat() calls: cheap, and correct when the
    # sample changes.
    work_pages = Path("/root/work/pages")
    persist_pages = persist / "pages"
    persist_pages.mkdir(exist_ok=True)
    n_copied = 0
    if work_pages.exists():
        for src in work_pages.rglob("*.jpg"):
            dst = persist_pages / src.relative_to(work_pages)
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                n_copied += 1
    n_total = sum(1 for _ in persist_pages.rglob("*.jpg"))
    print(f"Copied {n_copied} new page images to the volume ({n_total} present)")

    # Partial results: keep whichever copy has more documents -- the volume
    # may be further along than what's staged locally if a previous run was
    # cut off before its results made it back.
    staged = Path("/root/work") / out_name
    kept = persist / out_name

    def doc_count(p):
        try:
            return len(json.loads(p.read_text())) if p.exists() else 0
        except Exception:
            return 0

    if doc_count(staged) > doc_count(kept):
        shutil.copy2(staged, kept)
    print(f"Starting with {doc_count(kept)} documents already done")

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

    cmd = ["python3", "extract_density.py", "--model", model,
           "--host", "http://localhost:11434", "--split", split,
           "--max-dim", str(max_dim), "--out", out_name, "--resume"]
    if limit:
        cmd += ["--limit", str(limit)]

    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"WARNING: extract_density.py exited with code {result.returncode} "
              f"-- returning whatever partial progress was written. Re-run this "
              f"script (it will --resume) to pick up where it left off.")

    stop_committing.set()
    server.terminate()
    output_vol.commit()

    if not kept.exists():
        # Extraction died before writing anything (e.g. a missing image on the
        # very first call). Return an empty result rather than masking the real
        # error with a confusing FileNotFoundError on the output file.
        print(f"NO OUTPUT: {out_name} was never written -- see the error above.")
        return b"{}"
    return kept.read_bytes()


@app.local_entrypoint()
def main(model: str = MODEL, split: str = "validation",
         limit: Optional[int] = None, max_dim: int = 1400,
         out: Optional[str] = None):
    if not STAGING.exists():
        raise SystemExit(
            "modal_staging/ not found -- run `python3 prepare_modal_staging.py` first."
        )
    result = run_density.remote(model=model, split=split, limit=limit,
                                 max_dim=max_dim, out=out)
    out_name = out or f"density_{split}.json"
    out_path = HERE / out_name
    out_path.write_bytes(result)
    print(f"Wrote {out_path} ({len(result)} bytes)")
    print(f"Next: python3 analyze_density.py --infile {out_name}")
