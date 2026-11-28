"""RAM-only ComfyUI input/output nodes for private switcher image jobs."""
import base64
from fractions import Fraction
from io import BytesIO

import av
import numpy as np
from PIL import Image
import torch


class PrivateImageAndMask:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"image_base64": ("STRING", {"multiline": True}),
                             "mask_base64": ("STRING", {"multiline": True})}}

    RETURN_TYPES = ("IMAGE", "MASK")
    FUNCTION = "load"
    CATEGORY = "AI Switcher/private"

    def load(self, image_base64, mask_base64):
        with Image.open(BytesIO(base64.b64decode(image_base64, validate=True))) as source:
            image = source.convert("RGB")
        with Image.open(BytesIO(base64.b64decode(mask_base64, validate=True))) as source:
            mask = source.convert("L")
        if image.size != mask.size or image.width * image.height > 4_000_000:
            raise ValueError("Invalid private image and mask dimensions")
        pixels = torch.from_numpy(np.array(image).astype(np.float32) / 255).unsqueeze(0)
        alpha = torch.from_numpy(np.array(mask).astype(np.float32) / 255)
        return pixels, alpha


class PrivateImageResult:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"images": ("IMAGE",)}}

    RETURN_TYPES = ()
    FUNCTION = "return_image"
    OUTPUT_NODE = True
    CATEGORY = "AI Switcher/private"

    def return_image(self, images):
        if images.shape[0] != 1 or images.shape[1] * images.shape[2] > 4_000_000:
            raise ValueError("Private image output exceeds the size limit")
        image = Image.fromarray(np.clip(images[0].cpu().numpy() * 255, 0, 255).astype(np.uint8))
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return {"ui": {"private_images": [base64.b64encode(buffer.getvalue()).decode("ascii")]}}


class PrivateVideoResult:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"images": ("IMAGE",), "fps": ("INT", {"default": 16, "min": 1, "max": 24})}}

    RETURN_TYPES = ()
    FUNCTION = "return_video"
    OUTPUT_NODE = True
    CATEGORY = "AI Switcher/private"

    def return_video(self, images, fps):
        if images.shape[0] > 81 or images.shape[1] * images.shape[2] > 832 * 480:
            raise ValueError("Private video output exceeds the size limit")
        buffer = BytesIO()
        with av.open(buffer, mode="w", format="webm") as container:
            stream = container.add_stream("libvpx-vp9", rate=Fraction(fps, 1))
            stream.width = images.shape[2]
            stream.height = images.shape[1]
            stream.pix_fmt = "yuv420p"
            stream.bit_rate = 0
            stream.options = {"crf": "32"}
            for image in images:
                pixels = np.clip(image.cpu().numpy()[:, :, :3] * 255, 0, 255).astype(np.uint8)
                frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
        if buffer.tell() > 128 * 1024 * 1024:
            raise ValueError("Private video output exceeds 128 MiB")
        return {"ui": {"private_videos": [base64.b64encode(buffer.getvalue()).decode("ascii")]}}


NODE_CLASS_MAPPINGS = {
    "AISwitcherPrivateImageAndMask": PrivateImageAndMask,
    "AISwitcherPrivateImageResult": PrivateImageResult,
    "AISwitcherPrivateVideoResult": PrivateVideoResult,
}
