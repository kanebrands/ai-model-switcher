"""Bounded document, audio, and ComfyUI work for the persistent job service."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


MAX_PDF_PAGES = 20
TIMEOUT = 180


def run(command, timeout=TIMEOUT):
    result = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, env={**os.environ, "OMP_THREAD_LIMIT": "4"})
    return result.stdout


def pdf(job, payload):
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "document.pdf"
        source.write_bytes(payload)
        pages = int(run(["pdfinfo", str(source)]).decode(errors="replace").split("Pages:", 1)[1].splitlines()[0].strip())
        if pages > MAX_PDF_PAGES:
            raise ValueError(f"PDF exceeds the {MAX_PDF_PAGES}-page limit")
        text = run(["pdftotext", "-layout", "-f", "1", "-l", str(pages), str(source), "-"]).decode(errors="replace")
        ocr_used = False
        if not text.strip() and job["request"].get("ocr", True):
            prefix = Path(directory) / "page"
            run(["pdftoppm", "-f", "1", "-l", str(pages), "-r", "150", "-png", str(source), str(prefix)], timeout=300)
            text = "\n\n".join(run(["tesseract", str(page), "stdout"], timeout=60).decode(errors="replace")
                               for page in sorted(Path(directory).glob("page-*.png")))
            ocr_used = True
        return {"text": text, "pages": pages, "ocr_used": ocr_used}


def ocr(job, payload):
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "image"
        source.write_bytes(payload)
        language = job["request"].get("language", "eng")
        if not isinstance(language, str) or not language.replace("+", "").isalpha() or len(language) > 24:
            raise ValueError("Invalid OCR language")
        text = run(["tesseract", str(source), "stdout", "-l", language]).decode(errors="replace")
        return {"text": text, "language": language}


def transcription(job, payload):
    from faster_whisper import WhisperModel
    model_name = job["request"].get("model", "")
    if model_name not in {"tiny", "base", "small"}:
        raise ValueError("Choose a supported Whisper size: tiny, base, or small")
    cache = Path(os.environ.get("ASR_MODEL_DIR", "/app/data/asr-models"))
    cache.mkdir(parents=True, exist_ok=True)
    # The user explicitly selects the ASR size in the job or active profile.
    model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=4,
                         download_root=str(cache))
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "audio"
        source.write_bytes(payload)
        segments, info = model.transcribe(str(source), beam_size=1)
        text = " ".join(segment.text.strip() for segment in segments)
        return {"text": text, "language": info.language, "duration": info.duration}


def speech(job):
    text = job["request"].get("input", "")
    voice = job["request"].get("voice", "en")
    if not isinstance(text, str) or not 1 <= len(text) <= 4000:
        raise ValueError("Speech input must have 1–4000 characters")
    if not isinstance(voice, str) or not voice.replace("-", "").isalpha() or len(voice) > 24:
        raise ValueError("Invalid speech voice")
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "speech.wav"
        run(["espeak-ng", "-v", voice, "-w", str(output), text], timeout=60)
        return {"voice": voice, "format": "wav"}, output.read_bytes(), "audio/wav"


def comfy_request(path, body=None, timeout=30):
    base = os.environ.get("COMFY_INTERNAL_URL", "http://comfyui:8188")
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data,
                                     headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def sdxl_workflow(prompt, checkpoint, width, height, steps):
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
        "8": {"class_type": "KSampler", "inputs": {"seed": int(time.time_ns() % (2**53)),
                 "steps": steps, "cfg": 7, "sampler_name": "euler", "scheduler": "normal",
                 "denoise": 1, "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["4", 2]}},
        "10": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ai-switcher", "images": ["9", 0]}},
    }


def image(job, update):
    request = job["request"]
    prompt = request.get("prompt", "")
    checkpoint = request.get("checkpoint", "")
    width, height, steps = (request.get(key) for key in ("width", "height", "steps"))
    if not isinstance(prompt, str) or not 1 <= len(prompt) <= 4000:
        raise ValueError("Image prompt must have 1–4000 characters")
    if not isinstance(checkpoint, str) or Path(checkpoint).name != checkpoint or not checkpoint.endswith(".safetensors"):
        raise ValueError("Select an installed checkpoint")
    if not (isinstance(width, int) and isinstance(height, int) and isinstance(steps, int) and
            256 <= width <= 1536 and 256 <= height <= 1536 and width % 8 == height % 8 == 0 and 1 <= steps <= 60):
        raise ValueError("Choose width/height 256–1536 in multiples of 8 and steps 1–60")
    installed = Path(os.environ.get("COMFY_MODEL_DIR", "/app/media_models")) / "checkpoints" / checkpoint
    if not installed.is_file():
        raise ValueError("Checkpoint is not installed in ComfyUI")
    workflow = sdxl_workflow(prompt, checkpoint, width, height, steps)
    reply = comfy_request("/prompt", {"prompt": workflow, "client_id": job["id"]})
    ref = reply.get("prompt_id")
    if not ref:
        raise RuntimeError("ComfyUI rejected the workflow")
    update(job["id"], backend_ref=ref, stage="rendering in ComfyUI")
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        history = comfy_request("/history/" + ref).get(ref)
        if history:
            if history.get("status", {}).get("status_str") == "error":
                raise RuntimeError("ComfyUI workflow failed")
            outputs = history.get("outputs", {})
            images = outputs.get("10", {}).get("images", [])
            if images:
                item = images[0]
                from urllib.parse import urlencode
                url = os.environ.get("COMFY_INTERNAL_URL", "http://comfyui:8188") + "/view?" + urlencode({
                    "filename": item["filename"], "subfolder": item.get("subfolder", ""), "type": item.get("type", "output")})
                with urllib.request.urlopen(url, timeout=60) as response:
                    data = response.read(32 * 1024 * 1024 + 1)
                    if len(data) > 32 * 1024 * 1024:
                        raise ValueError("ComfyUI result exceeds 32 MiB")
                    return {"prompt_id": ref, "checkpoint": checkpoint}, data, response.headers.get_content_type()
        time.sleep(3)
    raise TimeoutError("ComfyUI image job timed out")
