#!/usr/bin/env python3
"""
Run content_code/run_eval.py on a Modal GPU.

Same design as the other Modal runners in this project: persistent,
periodically-committed output Volume + --resume, so disconnects can't cost
the run; shared ollama-model-cache Volume so qwen2.5:14b isn't re-downloaded.

Setup (once), if you haven't already from the earlier tasks:
    pip install modal
    modal setup
    python3 build_eval_data.py     # writes eval_data.json with fond_siblings

Run:
    modal run --detach modal_run_eval.py

    --detach so a local disconnect can't cancel the remote job.

Model: qwen2.5:14b by default -- the same model as every other classifier
in this project, so results are comparable.

Benchmarks on the 250 test cases: the class distribution is natural and
imbalanced (J 164 / N 59 / U 22 / M 5), so ALWAYS ANSWERING J scores
65.6% accuracy. Read MACRO recall and the J-vs-rest AUC instead -- the
AUC is what the cost-benefit multiplier actually consumes.

If your client disconnects before the run finishes, pull whatever's been
committed so far from any terminal with:

    modal volume get jewishgen-content-code-output results.csv .
"""
import subprocess
import time
from pathlib import Path

import modal

HERE = Path(__file__).parent

MODEL = "qwen2.5:14b"
COMMIT_INTERVAL_S = 20

app = modal.App("jewishgen-content-code")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("curl", "zstd")
    .run_commands("curl -fsSL https://ollama.com/install.sh | sh")
    .add_local_dir(str(HERE), remote_path="/root/work", ignore=["__pycache__"])
)

model_cache = modal.Volume.from_name("ollama-model-cache", create_if_missing=True)
output_vol = modal.Volume.from_name("jewishgen-content-code-output", create_if_missing=True)


@app.function(
    image=image,
    gpu="A10G",
    timeout=6 * 60 * 60,
    volumes={"/root/.ollama": model_cache, "/root/output": output_vol},
)
def run_classification(model: str = MODEL, out_name: str = "results.csv") -> bytes:
    import csv
    import os
    import shutil
    import threading
    import urllib.request

    persist = Path("/root/output")
    persist.mkdir(exist_ok=True)

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
def main(model: str = MODEL, out: str = "results.csv"):
    result = run_classification.remote(model=model, out_name=out)
    out_path = HERE / out
    out_path.write_bytes(result)
    print(f"Wrote {out_path} ({len(result)} bytes)")
