import modal

# 1. Define a persistent Volume to store the model weights
volume = modal.Volume.from_name("qwen-model-cache", create_if_missing=True)
MODEL_DIR = "/model-cache"
MODEL_ID = "Qwen/Qwen2.5-7B-Instruct"

# 2. Function to download model weights into the volume during image build
def download_model():
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=MODEL_ID,
        local_dir=f"{MODEL_DIR}/Qwen2.5-7B-Instruct",
        # Ignore bulky safety checker/non-essential files if present
        ignore_patterns=["*.pt", "*.bin"],  # Uses .safetensors
    )

# 3. Create image and execute pre-download step
# Pin compatible versions of torch and vllm
vllm_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "torch>=2.5.0",
        "vllm>=0.6.3",
        "transformers>=4.46.0",
        "huggingface_hub",
    )
    .run_function(download_model, volumes={MODEL_DIR: volume})
)

app = modal.App("qwen-inference-service")

@app.function(
    image=vllm_image,
    gpu="A10G",
    timeout=600,
    scaledown_window=300,
    volumes={MODEL_DIR: volume},
)
@modal.web_server(port=8000, startup_timeout=600)
def serve():
    import os
    import subprocess

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    cmd = [
        "python3",
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        f"{MODEL_DIR}/Qwen2.5-7B-Instruct",
        "--served-model-name",
        "Qwen/Qwen2.5-7B-Instruct",  # Matches the model string in your client request
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--api-key",
        os.getenv("HF_TOKEN", ""),
        "--gpu-memory-utilization",
        "0.80",
        "--max-model-len",
        "4096",
        "--enforce-eager",
    ]

    # Non-blocking process launch
    subprocess.Popen(cmd, env=env)
