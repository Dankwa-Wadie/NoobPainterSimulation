"""Wan 2.2 (TI2V 5B, local): a short clip of real motion from a still and a prompt.

Used by paintanim.py: the motion comes from here, then every frame is repainted,
so the model's small wobbles read as the painting's boil. Runs on the same local
ComfyUI as localimage.py (which starts it and frees it), with Wan 2.2 5B, the UMT5
text encoder (both GGUF, Q4_K_M) and the Wan 2.2 VAE: ~8.5 GB on disk.

On an M4 Pro with 24 GB, keep clips short and small (see DEFAULTS).
"""
from __future__ import annotations

import json
import logging
import random
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

LOG = logging.getLogger("mken.wanvideo")

DEFAULTS: dict[str, Any] = {
    "unet": "Wan2.2-TI2V-5B-Q4_K_M.gguf",
    "text_encoder": "umt5-xxl-encoder-Q4_K_M.gguf",
    "vae": "wan2.2_vae.safetensors",
    # 480x864 is the largest tried that stays clean on an M4 Pro with 24 GB: the
    # model's own 704x1280 ran 48 min and came apart into stripes and wrong colours
    # after the first frame. Every frame is repainted at 720x1280, which hides the
    # softness. 33 frames (~1.4 s) in about 14 min.
    "width": 480, "height": 864,
    "frames": 33,                        # 4n+1
    "steps": 16, "cfg": 5.0, "shift": 8.0,
    "negative": ("blurry, distorted, deformed, morphing face, extra limbs, flicker, "
                 "static, still image, text, watermark"),
    "timeout": 5400,                     # s: a clip can take most of an hour here
}


def settings(config: dict[str, Any] | None = None) -> dict[str, Any]:
    return {**DEFAULTS, **((config or {}).get("wanvideo") or {})}


def installed(cfg: dict[str, Any] | None = None) -> bool:
    import localimage
    cfg = settings(cfg)
    models = localimage.COMFY_DIR / "models"
    return all((models / sub / cfg[k]).exists()
               for k, sub in (("unet", "unet"), ("text_encoder", "text_encoders"), ("vae", "vae")))


def workflow(prompt: str, image_name: str, cfg: dict[str, Any], seed: int) -> dict[str, Any]:
    return {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": cfg["unet"]}},
        "2": {"class_type": "CLIPLoaderGGUF", "inputs": {"clip_name": cfg["text_encoder"], "type": "wan"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": cfg["vae"]}},
        "4": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": cfg["shift"]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": cfg["negative"]}},
        "7": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "8": {"class_type": "Wan22ImageToVideoLatent",
              "inputs": {"vae": ["3", 0], "width": cfg["width"], "height": cfg["height"],
                         "length": cfg["frames"], "batch_size": 1, "start_image": ["7", 0]}},
        "9": {"class_type": "KSampler",
              "inputs": {"model": ["4", 0], "positive": ["5", 0], "negative": ["6", 0],
                         "latent_image": ["8", 0], "seed": seed, "steps": cfg["steps"],
                         "cfg": cfg["cfg"], "sampler_name": "uni_pc", "scheduler": "simple",
                         "denoise": 1.0}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
        "11": {"class_type": "SaveImage", "inputs": {"images": ["10", 0], "filename_prefix": "wanclip"}},
    }


def generate(start: Path, prompt: str, out_dir: Path, config: dict[str, Any] | None = None,
             seed: int | None = None) -> list[Path]:
    """Animate `start` with the motion in `prompt`, in ComfyUI's turn (localimage.comfy_turn)."""
    import localimage
    lcfg = localimage.settings(config)
    localimage.ensure_server(lcfg)
    with localimage.comfy_turn(lcfg, "a video clip"):
        return _generate(start, prompt, out_dir, config, seed)


def _generate(start: Path, prompt: str, out_dir: Path, config: dict[str, Any] | None = None,
              seed: int | None = None) -> list[Path]:
    """Animate `start` with the motion in `prompt`; the frames as PNGs in `out_dir`."""
    import localimage
    import progress
    cfg = settings(config)
    if not installed(cfg):
        raise RuntimeError("Wan 2.2 isn't installed (see wanvideo.py for the three files)")
    lcfg = localimage.settings(config)
    localimage.ensure_server(lcfg)
    url = lcfg["url"]
    # hand the start picture to ComfyUI through its input folder
    name = f"wanstart-{uuid.uuid4().hex[:8]}.png"
    inputs = localimage.COMFY_DIR / "input"
    inputs.mkdir(exist_ok=True)
    from PIL import Image, ImageOps
    ImageOps.fit(Image.open(start).convert("RGB"), (cfg["width"], cfg["height"]),
                 Image.LANCZOS).save(inputs / name)
    seed = random.randrange(2**31) if seed is None else seed
    queued = localimage._post(url + "/prompt", {"prompt": workflow(prompt, name, cfg, seed)})
    if queued.get("node_errors"):
        raise RuntimeError(f"ComfyUI rejected the Wan graph: {queued['node_errors']}")
    pid = queued["prompt_id"]
    began, end = time.time(), time.time() + float(cfg["timeout"])
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        while time.time() < end:
            progress.tick(time.time() - began, expected=1800.0)
            hist = json.loads(localimage._get(f"{url}/history/{pid}")).get(pid)
            if hist:
                status = hist.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError(f"Wan failed: {str(status.get('messages'))[-400:]}")
                frames = [img for node in hist.get("outputs", {}).values() for img in node.get("images", [])]
                if frames:
                    paths = []
                    for i, img in enumerate(frames):
                        q = urllib.parse.urlencode({k: img[k] for k in ("filename", "subfolder", "type")})
                        p = out_dir / f"{i + 1:04d}.png"
                        p.write_bytes(localimage._get(f"{url}/view?{q}", timeout=60))
                        (localimage.COMFY_DIR / "output" / img.get("subfolder", "") / img["filename"]
                         ).unlink(missing_ok=True)
                        paths.append(p)
                    LOG.info("wan: %d frames in %.0f s", len(paths), time.time() - began)
                    return paths
            time.sleep(5)
        raise TimeoutError(f"no clip after {cfg['timeout']} s")
    finally:
        (inputs / name).unlink(missing_ok=True)
        localimage.release(lcfg)
