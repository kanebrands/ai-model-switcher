"""Bounded document, audio, and ComfyUI work for the persistent job service."""
import io
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile


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


def sdxl_reference_workflow(prompt, checkpoint, image_data, width, height, steps, strength):
    graph = sdxl_workflow(prompt, checkpoint, width, height, steps)
    graph["5"] = {"class_type": "AISwitcherReferenceImage",
                  "inputs": {"image_base64": base64.b64encode(image_data).decode("ascii")}}
    graph["11"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["5", 0], "vae": ["4", 2]}}
    graph["8"]["inputs"]["latent_image"] = ["11", 0]
    graph["8"]["inputs"]["denoise"] = strength
    return graph


def reference_images(payload):
    if not payload:
        return []
    from PIL import Image, ImageOps
    images = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        if not 1 <= len(names) <= 3 or names != [f"reference-{index}.bin" for index in range(1, len(names) + 1)]:
            raise ValueError("Reference archive must contain 1–3 images")
        for name in names:
            if archive.getinfo(name).file_size > 8 * 1024 * 1024:
                raise ValueError("Each reference image must be at most 8 MiB")
            with Image.open(io.BytesIO(archive.read(name))) as opened:
                if opened.format not in ("JPEG", "PNG", "WEBP") or opened.width * opened.height > 20_000_000 or min(opened.size) < 256:
                    raise ValueError("References must be JPEG, PNG, or WebP, 256 px or larger and at most 20 megapixels")
                images.append(ImageOps.exif_transpose(opened).convert("RGB"))
    return images


def _prepare_reference(image, width=None, height=None):
    from PIL import ImageOps
    if width is None and height is None:
        scale = min(1, 1024 / max(image.size))
        width = max(256, round(image.width * scale / 8) * 8)
        height = max(256, round(image.height * scale / 8) * 8)
    image = ImageOps.fit(image, (width, height))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue(), width, height


def image(job, payload, update):
    request = job["request"]
    prompt = request.get("prompt", "")
    checkpoint = request.get("checkpoint", "")
    width, height, steps = (request.get(key) for key in ("width", "height", "steps"))
    strength = request.get("reference_strength", 0.55)
    if not isinstance(prompt, str) or not 1 <= len(prompt) <= 4000:
        raise ValueError("Image prompt must have 1–4000 characters")
    if not isinstance(checkpoint, str) or Path(checkpoint).name != checkpoint or not checkpoint.endswith(".safetensors"):
        raise ValueError("Select an installed checkpoint")
    if not (isinstance(steps, int) and not isinstance(steps, bool) and 1 <= steps <= 60):
        raise ValueError("Choose 1–60 sampling steps")
    if not (width is None and height is None or
            type(width) is int and type(height) is int and 256 <= width <= 1536 and
            256 <= height <= 1536 and width % 8 == height % 8 == 0):
        raise ValueError("Choose width/height 256–1536 in multiples of 8 and steps 1–60")
    if strength not in (0.35, 0.55, 0.75):
        raise ValueError("Choose a reference strength of 0.35, 0.55, or 0.75")
    installed = Path(os.environ.get("COMFY_MODEL_DIR", "/app/media_models")) / "checkpoints" / checkpoint
    if not installed.is_file():
        raise ValueError("Checkpoint is not installed in ComfyUI")
    references = reference_images(payload)
    private = bool(request.get("_private"))
    outputs = []
    for index, reference in enumerate(references or [None], 1):
        if reference is None:
            output_width, output_height = width or 1024, height or 1024
            workflow = sdxl_workflow(prompt, checkpoint, output_width, output_height, steps)
        else:
            prepared, output_width, output_height = _prepare_reference(reference, width, height)
            workflow = sdxl_reference_workflow(prompt, checkpoint, prepared, output_width,
                                               output_height, steps, strength)
        if private:
            workflow["10"] = {"class_type": "AISwitcherPrivateImageResult", "inputs": {"images": ["9", 0]}}
        update(job["id"], stage=f"rendering image {index} of {len(references) or 1}")
        result, data, mime = render_workflow(job, workflow, update,
                                             private_key="private_images" if private else None)
        outputs.append(data)
    if len(outputs) == 1:
        return {**result, "checkpoint": checkpoint, "reference_count": len(references),
                "output_count": 1}, outputs[0], "image/png"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for index, data in enumerate(outputs, 1):
            bundle.writestr(f"generated-{index:02}.png", data)
    return {"checkpoint": checkpoint, "reference_count": len(references),
            "output_count": len(outputs)}, archive.getvalue(), "application/zip"


def wan21_workflow(prompt, width, height, frames, fps, steps,
                   model_name="wan2.1_t2v_1.3B_fp16.safetensors"):
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": model_name, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "wan_2.1_vae.safetensors"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry, static, low quality, distorted", "clip": ["2", 0]}},
        "6": {"class_type": "EmptyHunyuanLatentVideo", "inputs": {"width": width, "height": height, "length": frames, "batch_size": 1}},
        "7": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 8}},
        "8": {"class_type": "KSampler", "inputs": {"seed": int(time.time_ns() % (2**53)), "steps": steps,
                "cfg": 6, "sampler_name": "uni_pc", "scheduler": "simple", "denoise": 1,
                "model": ["7", 0], "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0]}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveWEBM", "inputs": {"images": ["9", 0],
                 "filename_prefix": "ai-switcher-video", "codec": "vp9", "fps": fps, "crf": 32}},
    }


def video(job, update):
    request = job["request"]
    prompt = request.get("prompt")
    if not isinstance(prompt, str) or not 1 <= len(prompt) <= 4000:
        raise ValueError("Video prompt must have 1–4000 characters")
    model_name = request.get("model")
    if model_name == "wan21-1b":
        model_name = "wan2.1_t2v_1.3B_fp16.safetensors"
    if (not isinstance(model_name, str) or Path(model_name).name != model_name or
            not model_name.lower().startswith("wan2.1_t2v_") or not model_name.endswith(".safetensors")):
        raise ValueError("Choose an installed Wan 2.1 text-to-video model")
    width, height, frames, fps, steps = (request.get(key) for key in
                                        ("width", "height", "frames", "fps", "steps"))
    if (any(type(value) is not int for value in (width, height, frames, fps, steps)) or
            (width, height) not in ((512, 512), (832, 480)) or
            frames not in (17, 33, 49, 81) or fps not in (8, 16, 24) or
            not 1 <= steps <= 50):
        raise ValueError("Choose 512x512 or 832x480, 17/33/49/81 frames, 8/16/24 fps, and 1–50 steps")
    model_dir = Path(os.environ.get("COMFY_MODEL_DIR", "/app/media_models"))
    files = ("diffusion_models/" + model_name,
             "text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors", "vae/wan_2.1_vae.safetensors")
    if not all((model_dir / name).is_file() for name in files):
        raise ValueError("Install the complete Wan 2.1 T2V 1.3B bundle in ComfyUI")
    workflow = wan21_workflow(prompt, width, height, frames, fps, steps, model_name)
    private = bool(request.get("_private"))
    if private:
        workflow["10"] = {"class_type": "AISwitcherPrivateVideoResult",
                          "inputs": {"images": ["9", 0], "fps": fps}}
    result, data, mime = render_workflow(job, workflow, update, output_extension=".webm",
                                         timeout=3600, size_limit=128 * 1024 * 1024,
                                         private_key="private_videos" if private else None)
    return {**result, "model": model_name, "frames": frames, "fps": fps,
            "width": width, "height": height}, data, mime


def render_workflow(job, workflow, update, output_node="10", output_extension=None,
                    timeout=900, size_limit=32 * 1024 * 1024, private_key=None):
    reply = comfy_request("/prompt", {"prompt": workflow, "client_id": job["id"]})
    ref = reply.get("prompt_id")
    if not ref:
        raise RuntimeError("ComfyUI rejected the workflow")
    update(job["id"], backend_ref=ref, stage="rendering in ComfyUI")
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            history = comfy_request("/history/" + ref).get(ref)
            if history:
                if history.get("status", {}).get("status_str") == "error":
                    raise RuntimeError("ComfyUI workflow failed")
                outputs = history.get("outputs", {})
                entries = outputs.get(output_node, {})
                if private_key and entries.get(private_key):
                    data = base64.b64decode(entries[private_key][0], validate=True)
                    if len(data) > size_limit:
                        raise ValueError("Private ComfyUI result exceeds the job size limit")
                    return {"prompt_id": ref}, data, "video/webm" if output_extension == ".webm" else "image/png"
                candidates = [item for value in entries.values() if isinstance(value, list)
                              for item in value if isinstance(item, dict) and "filename" in item]
                matches = [item for item in candidates if
                           output_extension is None or item["filename"].lower().endswith(output_extension)]
                if matches and not private_key:
                    item = matches[0]
                    from urllib.parse import urlencode
                    url = os.environ.get("COMFY_INTERNAL_URL", "http://comfyui:8188") + "/view?" + urlencode({
                        "filename": item["filename"], "subfolder": item.get("subfolder", ""), "type": item.get("type", "output")})
                    with urllib.request.urlopen(url, timeout=60) as response:
                        data = response.read(size_limit + 1)
                        if len(data) > size_limit:
                            raise ValueError("ComfyUI result exceeds the job size limit")
                        mime = "video/webm" if output_extension == ".webm" else response.headers.get_content_type()
                        return {"prompt_id": ref}, data, mime
            time.sleep(3)
        raise TimeoutError("ComfyUI job timed out")
    finally:
        if private_key:
            try:
                request = urllib.request.Request(os.environ.get("COMFY_INTERNAL_URL", "http://comfyui:8188")
                                                 + "/history", data=json.dumps({"delete": [ref]}).encode(),
                                                 headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=10):
                    pass
            except Exception:
                pass


def prepare_image_edit(job, payload, update):
    from image_edit import prepare
    return prepare(job, payload, update)


def image_edit(job, prepared, update):
    from image_edit import render
    return render(job, prepared, update, render_workflow)
