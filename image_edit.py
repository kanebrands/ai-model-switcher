"""Grounded object removal: detect, segment, inpaint, and composite."""
import gc
import base64
from io import BytesIO
import os
from pathlib import Path
import re
import time


DINO_ID = "IDEA-Research/grounding-dino-tiny"
SAM_ID = "facebook/sam-vit-base"
MAX_PIXELS = 20_000_000


def target_from_prompt(prompt, explicit=None):
    if explicit is not None:
        target = explicit
    else:
        if not isinstance(prompt, str):
            raise ValueError("An image-edit prompt is required")
        cleaned = prompt.strip().lower().rstrip(". !")
        match = re.search(r"\b(?:remove|erase|delete|take out)\s+(?:the |a |an )?(.+?)"
                          r"(?:\s+from\s+(?:the |this |my |an? )?(?:attached )?(?:image|photo|picture)|$)", cleaned)
        if not match:
            raise ValueError("Say which object to remove, for example: remove the trash can from the image")
        target = match.group(1)
    if not isinstance(target, str):
        raise ValueError("Object name must be text")
    target = re.sub(r"\b(?:please|in the background|in the foreground)\b", "", target.lower()).strip(" ,.")
    target = target.replace("trashcan", "trash can")
    if not 2 <= len(target) <= 80 or not re.fullmatch(r"[a-z0-9][a-z0-9 '-]*", target):
        raise ValueError("Choose one object name of 2–80 characters")
    return target


def select_box(boxes, scores, minimum=0.22):
    candidates = sorted(((float(score), [float(value) for value in box])
                         for box, score in zip(boxes, scores) if float(score) >= minimum), reverse=True)
    if not candidates:
        raise ValueError("The requested object was not found with enough confidence")
    if len(candidates) > 1 and candidates[1][0] >= candidates[0][0] - 0.05:
        raise ValueError("Several matching objects were found; provide a bounding box to select one")
    return candidates[0][1], candidates[0][0]


def _open_image(payload):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    with Image.open(BytesIO(payload)) as opened:
        if opened.format not in ("JPEG", "PNG", "WEBP"):
            raise ValueError("Upload a JPEG, PNG, or WebP image")
        if opened.width * opened.height > MAX_PIXELS or min(opened.size) < 256:
            raise ValueError("Image must be at least 256 px per side and at most 20 megapixels")
        return ImageOps.exif_transpose(opened).convert("RGB")


def _detect(image, target):
    import torch
    from transformers import AutoProcessor, AutoModelForZeroShotObjectDetection
    cache = os.environ.get("EDIT_MODEL_DIR", "/app/data/multimodal/edit-models")
    preview = image.copy()
    preview.thumbnail((1024, 1024))
    torch.set_num_threads(max(1, min(8, (os.cpu_count() or 4) // 2)))
    processor = AutoProcessor.from_pretrained(DINO_ID, cache_dir=cache)
    model = AutoModelForZeroShotObjectDetection.from_pretrained(DINO_ID, cache_dir=cache,
                                                                 use_safetensors=True).eval()
    inputs = processor(images=preview, text=[[target]], return_tensors="pt")
    with torch.inference_mode():
        output = model(**inputs)
    result = processor.post_process_grounded_object_detection(
        output, threshold=0.22, text_threshold=0.22,
        target_sizes=[(preview.height, preview.width)])[0]
    box, score = select_box(result["boxes"].tolist(), result["scores"].tolist())
    del model, processor, inputs, output, result
    gc.collect()
    return preview, box, score


def _segment(preview, box):
    import torch
    from PIL import Image
    from transformers import SamModel, SamProcessor
    cache = os.environ.get("EDIT_MODEL_DIR", "/app/data/multimodal/edit-models")
    processor = SamProcessor.from_pretrained(SAM_ID, cache_dir=cache)
    model = SamModel.from_pretrained(SAM_ID, cache_dir=cache, use_safetensors=True).eval()
    inputs = processor(images=preview, input_boxes=[[box]], return_tensors="pt")
    with torch.inference_mode():
        output = model(**inputs, multimask_output=False)
    masks = processor.image_processor.post_process_masks(
        output.pred_masks.cpu(), inputs["original_sizes"].cpu(), inputs["reshaped_input_sizes"].cpu())
    mask = masks[0][0][0].cpu().numpy().astype("uint8") * 255
    result = Image.fromarray(mask, "L")
    del model, processor, inputs, output, masks, mask
    gc.collect()
    return result


def prepare(job, payload, update):
    from PIL import Image
    request = job["request"]
    target = target_from_prompt(request.get("prompt"), request.get("target"))
    image = _open_image(payload)
    manual_box = request.get("box")
    if manual_box is not None:
        if (not isinstance(manual_box, list) or len(manual_box) != 4 or
                any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in manual_box)):
            raise ValueError("box must contain four image coordinates")
        if not (0 <= manual_box[0] < manual_box[2] <= image.width and
                0 <= manual_box[1] < manual_box[3] <= image.height):
            raise ValueError("box is outside the image")
        preview = image.copy()
        preview.thumbnail((1024, 1024))
        box = [manual_box[0] * preview.width / image.width,
               manual_box[1] * preview.height / image.height,
               manual_box[2] * preview.width / image.width,
               manual_box[3] * preview.height / image.height]
        score = None
    else:
        preview, box, score = _detect(image, target)
    update(job["id"], stage="segmenting " + target)
    mask = _segment(preview, box).resize(image.size, Image.Resampling.NEAREST)
    coverage = sum(1 for value in mask.resize((128, 128)).getdata() if value > 128) / (128 * 128)
    if coverage < 0.0001 or coverage > 0.45:
        raise ValueError("Object mask was empty or covered too much of the image")
    return {"image": image, "mask": mask, "target": target,
            "box": [round(box[0] * image.width / preview.width), round(box[1] * image.height / preview.height),
                    round(box[2] * image.width / preview.width), round(box[3] * image.height / preview.height)],
            "confidence": round(score, 3) if score is not None else None}


def crop_for_edit(image, mask):
    from PIL import Image, ImageFilter
    bbox = mask.getbbox()
    if not bbox:
        raise ValueError("Segmentation produced an empty mask")
    object_size = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
    side = min(min(image.size), max(512, int(object_size * 2.2)))
    if side < object_size * 1.1:
        raise ValueError("Object is too large for the current inpainting workflow")
    center_x, center_y = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    left = max(0, min(image.width - side, round(center_x - side / 2)))
    top = max(0, min(image.height - side, round(center_y - side / 2)))
    crop_box = (left, top, left + side, top + side)
    crop = image.crop(crop_box).resize((512, 512), Image.Resampling.LANCZOS)
    crop_mask = mask.crop(crop_box).resize((512, 512), Image.Resampling.NEAREST)
    crop_mask = crop_mask.filter(ImageFilter.MaxFilter(11))
    return crop_box, crop, crop_mask


def workflow(checkpoint, image_name, mask_name, target, steps):
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
        "5": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "6": {"class_type": "LoadImageMask", "inputs": {"image": mask_name, "channel": "red"}},
        "7": {"class_type": "VAEEncodeForInpaint", "inputs": {"pixels": ["5", 0], "vae": ["4", 2],
                                                       "mask": ["6", 0], "grow_mask_by": 6}},
        "8": {"class_type": "CLIPTextEncode", "inputs": {"text": "clean natural continuation of the surrounding scene, seamless background, matching lighting and perspective", "clip": ["4", 1]}},
        "9": {"class_type": "CLIPTextEncode", "inputs": {"text": target + ", object, artifact, distortion", "clip": ["4", 1]}},
        "10": {"class_type": "KSampler", "inputs": {"seed": int(time.time_ns() % (2**53)),
                  "steps": steps, "cfg": 7, "sampler_name": "euler", "scheduler": "normal", "denoise": 1,
                  "model": ["4", 0], "positive": ["8", 0], "negative": ["9", 0], "latent_image": ["7", 0]}},
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["4", 2]}},
        "12": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ai-switcher-edit", "images": ["11", 0]}},
    }


def private_workflow(checkpoint, crop, mask, target, steps):
    def encoded(image):
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("ascii")
    graph = workflow(checkpoint, "", "", target, steps)
    graph["5"] = {"class_type": "AISwitcherPrivateImageAndMask",
                  "inputs": {"image_base64": encoded(crop), "mask_base64": encoded(mask)}}
    del graph["6"]
    graph["7"]["inputs"]["mask"] = ["5", 1]
    graph["12"] = {"class_type": "AISwitcherPrivateImageResult", "inputs": {"images": ["11", 0]}}
    return graph


def render(job, prepared, update, render_workflow):
    from PIL import Image, ImageFilter
    request = job["request"]
    checkpoint = request.get("checkpoint")
    steps = request.get("steps", 22)
    if not isinstance(checkpoint, str) or Path(checkpoint).name != checkpoint or not checkpoint.endswith(".safetensors"):
        raise ValueError("Select an installed inpainting checkpoint")
    if not isinstance(steps, int) or isinstance(steps, bool) or not 1 <= steps <= 60:
        raise ValueError("steps must be 1–60")
    model = Path(os.environ.get("COMFY_MODEL_DIR", "/app/media_models")) / "checkpoints" / checkpoint
    if not model.is_file() or "inpaint" not in checkpoint.lower():
        raise ValueError("A compatible inpainting checkpoint is required")
    input_dir = Path(os.environ.get("COMFY_INPUT_DIR", "/app/media_input"))
    crop_box, crop, mask = crop_for_edit(prepared["image"], prepared["mask"])
    if request.get("_private"):
        result, raw, mime = render_workflow(job, private_workflow(checkpoint, crop, mask,
                                                                  prepared["target"], steps), update,
                                            output_node="12", private_key="private_images")
        paths = []
    else:
        input_name = job["id"] + "-source.png"
        mask_name = job["id"] + "-mask.png"
        paths = [input_dir / input_name, input_dir / mask_name]
    try:
        if not request.get("_private"):
            for picture, path in zip((crop, mask), paths):
                picture.save(path, format="PNG")
                path.chmod(0o644)
            result, raw, mime = render_workflow(job, workflow(checkpoint, input_name, mask_name,
                                                              prepared["target"], steps), update, output_node="12")
        with Image.open(BytesIO(raw)) as generated:
            edited_crop = generated.convert("RGB").resize((crop_box[2] - crop_box[0],
                                                             crop_box[3] - crop_box[1]), Image.Resampling.LANCZOS)
        feather = mask.resize(edited_crop.size, Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(2.5))
        original = prepared["image"].copy()
        old_crop = original.crop(crop_box)
        original.paste(Image.composite(edited_crop, old_crop, feather), crop_box[:2])
        buffer = BytesIO()
        original.save(buffer, format="PNG")
        return {**result, "checkpoint": checkpoint, "target": prepared["target"],
                "box": prepared["box"], "detection_confidence": prepared["confidence"],
                "width": original.width, "height": original.height}, buffer.getvalue(), "image/png"
    finally:
        for path in paths:
            path.unlink(missing_ok=True)
