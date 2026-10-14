"""
Local image generation through ComfyUI, for the sentences Wikimedia Commons
has no photo for.

Model: Qwen-Image-2512 (Apache 2.0), quantised to GGUF so it fits alongside
everything else on the Mac. Qwen-Image-2.1 is deliberately NOT used: its
Qwen Research License forbids commercial use, which a monetised channel is.

ComfyUI is the user's standalone install in ~/ComfyUI-Installs with the
ComfyUI-GGUF custom node. When it isn't already running we start it headless
on its own port and leave it up — loading the model is the slow part, so the
next picture (and the next video) reuses the warm server.
"""
from __future__ import annotations

import contextlib
import json
import os
import logging
import random
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

LOG = logging.getLogger("localimage")

COMFY_HOME = Path(os.environ.get("NOOBPAINTER_COMFY_HOME", Path.home() / "ComfyUI-Installs" / "ComfyUI"))
COMFY_DIR = COMFY_HOME / "ComfyUI"
COMFY_PYTHON = (COMFY_HOME / "standalone-env" / "Scripts" / "python.exe" if os.name == "nt"
                else COMFY_HOME / "standalone-env" / "bin" / "python3")

DEFAULTS: dict[str, Any] = {
    "url": "http://127.0.0.1:8189",
    # Q4 with the 8-step add-on: about 3 minutes a picture on a 24 GB Apple Silicon Mac.
    "unet": "qwen-image-2512-Q4_K_M.gguf",
    "text_encoder": "Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf",
    "vae": "qwen_image_vae.safetensors",
    # The Lightning add-on distils 20+ steps down to 8 (cfg must then be 1).
    "lora": "Qwen-Image-2512-Lightning-8steps-V1.0-bf16.safetensors",
    "width": 1024, "height": 576,       # 16:9; the frame scales it up
    "steps": 8, "cfg": 1.0, "shift": 3.1,
    "timeout": 600,                     # per picture; a stuck one is cancelled and skipped
    # The pipeline passes each style's own look and negative prompt; these are fallbacks.
    "style": "detailed illustration, one clear subject, clean composition, no text, no watermark",
    "negative": "text, letters, watermark, logo, caption, blurry, deformed",
}

MODEL_DIRS = {"unet": "unet", "text_encoder": "text_encoders", "vae": "vae", "lora": "loras"}


def settings(config: dict[str, Any] | None) -> dict[str, Any]:
    local = ((config or {}).get("longform") or {}).get("local_image") or {}
    return {**DEFAULTS, **local}


def missing_files(cfg: dict[str, Any]) -> list[str]:
    """Model files not on disk yet — generation is off until this is empty."""
    return [cfg[k] for k, sub in MODEL_DIRS.items()
            if cfg.get(k) and not (COMFY_DIR / "models" / sub / cfg[k]).exists()]


def installed(cfg: dict[str, Any]) -> bool:
    return (COMFY_PYTHON.exists()
            and (COMFY_DIR / "custom_nodes" / "ComfyUI-GGUF").exists()
            and not missing_files(cfg))


def _get(url: str, timeout: float = 10) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:     # noqa: S310 (localhost)
        return r.read()


def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:          # noqa: S310
        return json.loads(r.read())


def running(cfg: dict[str, Any]) -> bool:
    try:
        _get(cfg["url"] + "/system_stats", timeout=3)
        return True
    except (urllib.error.URLError, OSError):
        return False


def ensure_server(cfg: dict[str, Any], wait: float = 120) -> None:
    """Start ComfyUI headless if nothing answers on the configured port."""
    if running(cfg):
        return
    port = urllib.parse.urlparse(cfg["url"]).port or 8189
    log = COMFY_HOME / "logs" / "pipeline-comfyui.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    LOG.info("starting ComfyUI on port %d", port)
    subprocess.Popen(                                           # noqa: S603
        [str(COMFY_PYTHON), "main.py", "--listen", "127.0.0.1", "--port", str(port),
         "--disable-auto-launch"],
        cwd=COMFY_DIR, stdout=log.open("ab"), stderr=subprocess.STDOUT,
        start_new_session=True)
    end = time.time() + wait
    while time.time() < end:
        if running(cfg):
            return
        time.sleep(2)
    raise RuntimeError(f"ComfyUI did not start within {wait:.0f}s (see {log})")


def interrupt(cfg: dict[str, Any]) -> None:
    """Cancel the running render and clear the queue, so a stuck scene can't
    hold up the next one."""
    if not running(cfg):
        return
    for path, body in (("/interrupt", {}), ("/queue", {"clear": True})):
        req = urllib.request.Request(cfg["url"] + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15):   # noqa: S310 (localhost)
                pass
        except (urllib.error.URLError, OSError) as exc:
            LOG.warning("could not interrupt ComfyUI (%s)", exc)


def shutdown(cfg: dict[str, Any]) -> None:
    """
    Stop ComfyUI entirely once the scenes are drawn, so its memory goes back
    to the rest of the episode (voice, ffmpeg, HyperFrames' browser). The next
    scene request starts it again; that costs ~30 s, against ~13 GB held idle.
    """
    import signal
    port = urllib.parse.urlparse(cfg["url"]).port or 8189
    try:
        out = subprocess.run(["lsof", "-tiTCP:%d" % port, "-sTCP:LISTEN"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return
    for pid in out:
        try:
            cmd = subprocess.run(["ps", "-o", "command=", "-p", pid], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=5).stdout
            if "main.py" in cmd and "ComfyUI" in cmd:     # only ever our ComfyUI
                os.kill(int(pid), signal.SIGTERM)
                LOG.info("ComfyUI stopped to free memory")
        except (OSError, ValueError, subprocess.SubprocessError):
            continue


def release(cfg: dict[str, Any]) -> None:
    """
    Unload the model and free its memory, leaving ComfyUI itself running.
    On a 24 GB Mac a resident 10+ GB model pushes everything else to swap.
    """
    if not running(cfg):
        return
    # /free answers with an empty body, not JSON, so don't parse it.
    req = urllib.request.Request(
        cfg["url"] + "/free", data=json.dumps({"unload_models": True, "free_memory": True}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30):           # noqa: S310 (localhost)
            pass
    except (urllib.error.URLError, OSError) as exc:
        LOG.warning("could not free ComfyUI memory (%s)", exc)


def workflow(prompt: str, cfg: dict[str, Any], seed: int) -> dict[str, Any]:
    """The standard Qwen-Image graph, with GGUF loaders."""
    graph = {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": cfg["unet"]}},
        "2": {"class_type": "CLIPLoaderGGUF",
              "inputs": {"clip_name": cfg["text_encoder"], "type": "qwen_image"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": cfg["vae"]}},
        "11": {"class_type": "LoraLoaderModelOnly",
               "inputs": {"model": ["1", 0], "lora_name": cfg["lora"], "strength_model": 1.0}},
        "4": {"class_type": "ModelSamplingAuraFlow",
              "inputs": {"model": ["11" if cfg.get("lora") else "1", 0], "shift": cfg["shift"]}},
        "5": {"class_type": "CLIPTextEncode",
              "inputs": {"clip": ["2", 0], "text": f"{prompt}. {cfg['style']}"}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"clip": ["2", 0], "text": cfg["negative"]}},
        "7": {"class_type": "EmptySD3LatentImage",
              "inputs": {"width": cfg["width"], "height": cfg["height"], "batch_size": 1}},
        "8": {"class_type": "KSampler",
              "inputs": {"model": ["4", 0], "positive": ["5", 0], "negative": ["6", 0],
                         "latent_image": ["7", 0], "seed": seed, "steps": cfg["steps"],
                         "cfg": cfg["cfg"], "sampler_name": "euler", "scheduler": "simple",
                         "denoise": 1.0}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage",
               "inputs": {"images": ["9", 0], "filename_prefix": "miscken"}},
    }
    if not cfg.get("lora"):
        del graph["11"]
    return graph


LOCK_FILE = COMFY_HOME / "pipeline-gpu.lock"


@contextlib.contextmanager
def comfy_turn(cfg: dict[str, Any] | None = None, what: str = "a render"):
    """
    Wait for ComfyUI to be free, then hold it for one job.

    Several jobs (two Studio sessions, a Short and a video clip) share one ComfyUI
    on one GPU. Without turns, a job's render queued behind another's and ran out
    its own timeout while it waited. The file lock serialises our jobs (and frees
    itself if a job dies); waiting for an empty queue as well covers work queued
    by anything that doesn't take the lock. Timeouts start once it's our turn.
    """
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_FILE, "a+", encoding="utf-8")
    began, told = time.time(), False
    try:
        while True:
            try:
                _lock(handle)
                break
            except (BlockingIOError, PermissionError, OSError):
                if not told:
                    LOG.info("ComfyUI is busy with another job; %s waits its turn", what)
                    told = True
                time.sleep(3)
        if cfg and running(cfg):
            while _queue_length(cfg) > 0:
                if not told:
                    LOG.info("ComfyUI is still working on something else; %s waits", what)
                    told = True
                time.sleep(5)
        if told:
            LOG.info("%s starts after waiting %.0f s", what, time.time() - began)
        yield
    finally:
        try:
            _unlock(handle)
        finally:
            handle.close()


def _lock(handle) -> None:
    """Take the file lock without waiting (raises OSError if another job holds it)."""
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_UN)


def _queue_length(cfg: dict[str, Any]) -> int:
    try:
        q = json.loads(_get(cfg["url"] + "/queue", timeout=5))
        return len(q.get("queue_running") or []) + len(q.get("queue_pending") or [])
    except (urllib.error.URLError, OSError, ValueError):
        return 0


def generate(prompt: str, out: Path, config: dict[str, Any] | None = None,
             seed: int | None = None) -> Path:
    """Render one picture for `prompt` into `out` (PNG), in ComfyUI's turn. Raises on failure."""
    cfg = settings(config)
    if not installed(cfg):
        raise RuntimeError(f"local image model not installed: {missing_files(cfg)}")
    ensure_server(cfg)
    with comfy_turn(cfg, "a picture"):
        try:
            return _generate(prompt, out, config, seed)
        except Exception:
            if running(cfg):
                raise
            # ComfyUI went away mid-scene — Pause shuts it down to free its
            # memory. Start it again and draw this scene from the top.
            LOG.info("ComfyUI was stopped mid-scene (paused?) — restarting it")
            ensure_server(cfg)
            return _generate(prompt, out, config, seed)


def _generate(prompt: str, out: Path, config: dict[str, Any] | None = None,
              seed: int | None = None) -> Path:
    """Render one picture for `prompt` into `out` (PNG). Raises on failure."""
    cfg = settings(config)
    if not installed(cfg):
        raise RuntimeError(f"local image model not installed: {missing_files(cfg)}")
    ensure_server(cfg)
    seed = random.randrange(2**31) if seed is None else seed
    queued = _post(cfg["url"] + "/prompt", {"prompt": workflow(prompt, cfg, seed)})
    if queued.get("node_errors"):
        raise RuntimeError(f"ComfyUI rejected the graph: {queued['node_errors']}")
    pid = queued["prompt_id"]
    import progress
    began = time.time()
    end = began + float(cfg["timeout"])
    while time.time() < end:
        progress.tick(time.time() - began)
        hist = json.loads(_get(f"{cfg['url']}/history/{pid}")).get(pid)
        if hist:
            status = hist.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI failed: {str(status.get('messages'))[-300:]}")
            for node in hist.get("outputs", {}).values():
                for img in node.get("images", []):
                    q = urllib.parse.urlencode({k: img[k] for k in
                                                ("filename", "subfolder", "type")})
                    out = Path(out)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(_get(f"{cfg['url']}/view?{q}", timeout=60))
                    # ComfyUI keeps its own copy of every picture forever;
                    # ours is saved, so drop theirs.
                    if img.get("type") == "output":
                        (COMFY_DIR / "output" / img.get("subfolder", "") / img["filename"]
                         ).unlink(missing_ok=True)
                    return out
        time.sleep(2)
    raise TimeoutError(f"no picture after {cfg['timeout']}s")


# The model can't draw readable writing, and fills any open page with fake
# text. A sentence about a document gets the document shut, in its room; the
# key-line card carries the actual words.
DOCUMENT_WORDS = ("log", "logbook", "diary", "journal", "letter", "note", "entry",
                  "entries", "page", "book", "record", "report", "telegram",
                  "manuscript", "message", "inscription", "sign", "map")


def is_document(text: str) -> bool:
    import re
    words = set(re.findall(r"[a-z]+", text.lower()))
    return any(w in words for w in DOCUMENT_WORDS)


def prompt_for(sentence: str, search: str, topic: str) -> str:
    """What to draw: the sentence's own subject, in the video's context."""
    if is_document(f"{search} {sentence}"):
        context = f" ({topic})" if topic else ""
        return (f"A closed, worn leather-bound book lying shut on an old wooden desk "
                f"beside a dim oil lamp, in the room where the story happened{context}. "
                f"The book is closed; no pages, paper or writing visible")
    subject = (search or "").strip() or sentence.strip()
    context = f" (context: {topic})" if topic and topic.lower() not in subject.lower() else ""
    return (f"{subject}{context}. Scene: {sentence.strip()} "
            f"Show objects, places and people, never readable pages or writing")
