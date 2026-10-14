"""Downloads the painter needs once, shown as they happen.

The first painting on a new computer fetches two models: rembg's subject finder (about
1 GB) and a depth model (about 100 MB). Left to their libraries, they show progress
only in the terminal, and the app's bar sat still for minutes, as if stuck (2026-10-14,
on Windows). Fetched here instead, the app shows what is downloading, how far it has
got and about how long is left; the libraries then find the files already there.
"""
from __future__ import annotations

import logging
import os
import shutil
import ssl
import time
import urllib.request
from pathlib import Path

LOG = logging.getLogger("noobpainter.downloads")

try:                                   # python.org builds ship no CA bundle of their own
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _CTX = ssl.create_default_context()

RMBG_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/bria-rmbg-2.0.onnx"
DEPTH_REPO = "depth-anything/Depth-Anything-V2-Small-hf"
DEPTH_FILES = ("config.json", "preprocessor_config.json", "model.safetensors")
DEPTH_DIR = Path.home() / ".cache" / "noobpainter" / "depth-anything-v2-small"


def _mb(n: float) -> str:
    return f"{n / 1e6:,.0f} MB"


def _left(secs: float) -> str:
    if secs < 90:
        return "under a minute and a half left"
    return f"about {secs / 60:.0f} minutes left"


def fetch(url: str, dest: Path, label: str) -> Path:
    """`url` to `dest`, unless it is there already, with its progress on the app's bar:
    "Downloading <label>: 567 of 1,020 MB, about 9 minutes left". A stopped download
    starts again next time (a part file is never mistaken for the whole)."""
    import progress
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    was = progress.snapshot() if progress.active() else None
    if was is not None:
        progress._set(stage="download", detail=f"Downloading {label} (needed once)")
        progress._percent(0.0)
    progress.report(f"  [download] {label}: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "NoobPainterSimulation"})
    began, shown = time.time(), 0.0
    try:
        with urllib.request.urlopen(req, timeout=60, context=_CTX) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - shown >= 1.0 or done == total:   # once a second is plenty
                    shown = now
                    rate = done / max(0.5, now - began)
                    if total:
                        detail = (f"Downloading {label} (needed once): {_mb(done)} of {_mb(total)}, "
                                  + _left((total - done) / max(1.0, rate)))
                        progress.fraction(done / total, detail)
                    else:
                        progress._set(detail=f"Downloading {label} (needed once): {_mb(done)}")
        if total and done < total:
            raise OSError(f"the download of {label} stopped at {_mb(done)} of {_mb(total)}")
        shutil.move(str(part), str(dest))
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        if was is not None:                      # back to where the painting was
            progress._set(stage=was.get("stage", ""), detail=was.get("detail", ""),
                          percent=was.get("percent", 0.0))
    progress.report(f"  [download] {label}: done ({_mb(dest.stat().st_size)}, {time.time() - began:.0f} s)")
    return dest


def rembg_model() -> None:
    """rembg's default model (bria-rmbg, about 1 GB), where rembg looks for it."""
    try:
        from rembg.sessions.bria_rmbg import BriaRmBgSession
    except ImportError:
        return                                   # no rembg: the callers fall back
    fname = f"{BriaRmBgSession.name()}.onnx"
    if BriaRmBgSession.resolve_existing(fname):
        return
    fetch(RMBG_URL, Path(BriaRmBgSession.model_dir()) / fname, "the subject finder")


def depth_model() -> str:
    """The depth model's folder (or its id, if an earlier version already cached it)."""
    hf = (Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
          / ("models--" + DEPTH_REPO.replace("/", "--")))
    if hf.exists() and not DEPTH_DIR.exists():
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        return DEPTH_REPO                        # downloaded before by transformers: use that
    for name in DEPTH_FILES:
        fetch(f"https://huggingface.co/{DEPTH_REPO}/resolve/main/{name}", DEPTH_DIR / name,
              "the depth model" if name.endswith(".safetensors") else f"the depth model ({name})")
    return str(DEPTH_DIR)
