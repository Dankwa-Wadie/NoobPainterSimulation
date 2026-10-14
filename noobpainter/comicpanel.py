"""Comic halftone: a manga panel in ink and screentone, that slowly drifts out of focus.

A black-and-white comic panel
fades in from white, holds sharp, then the camera pushes in slowly while a blur
spreads from the edges (the face stays sharpest longest), and it fades out.

  the look   ink lines and solid blacks kept crisp; mid-greys turned into a printed
             screentone (round dots on a 45-degree grid, their size by the tone);
             light areas left white.
  the motion fade in, hold, push in with a blur that grows from the edges, fade out.

Any picture works; drawn from an idea, it asks for a black-and-white manga
illustration of an original character.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

import cv2
import numpy as np

LOG = logging.getLogger("mken.comicpanel")

DEFAULTS: dict[str, Any] = {
    "seconds": 6.5, "fps": 30, "size": [1080, 1920],
    "fade_in": 0.6, "hold": 2.2, "fade_out": 0.6,
    "push": 1.22,                    # the zoom it ends on
    "blur": 22.0,                    # px at the frame's edges, at the end
    "cell": 7,                       # screentone dot spacing, px at 1080 wide
    "ink": 70, "white": 236,         # below: solid ink; above: paper; between: dots
    "paper_tone": 222,               # the light screen over the whole panel (196 read as grey)
}
PROMPT = ("black and white manga illustration, an original fictional character, bold ink "
          "lines, solid black shadows, screentone shading, dramatic expression, plain white "
          "background, comic panel, no logos, no emblems, no text")


def screentone(grey: np.ndarray, cell: int, ink: int, white: int, lines: float = 1.0) -> np.ndarray:
    """Ink and halftone: crisp blacks, dot-screened greys, white paper (uint8, 0..255)."""
    h, w = grey.shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    a = np.deg2rad(45)
    u = (xx * np.cos(a) + yy * np.sin(a)) / cell
    v = (-xx * np.sin(a) + yy * np.cos(a)) / cell
    # distance to the nearest dot centre, 0 at the centre .. ~0.7 at the corner
    d = np.hypot(u - np.round(u), v - np.round(v))
    # Fine texture (pencil hatching) softened into grey first, so it becomes dots,
    # as screentone replaces hatching; only strong outlines stay as ink.
    soft = cv2.GaussianBlur(grey, (0, 0), cell * 0.45).astype(np.float32)
    tone = np.clip((white - soft) / max(1, white - ink), 0, 1)    # 0 paper .. 1 ink
    radius = np.sqrt(tone / np.pi) * 1.05                      # dot area follows the tone
    dots = np.where(d < radius, 0, 255).astype(np.uint8)
    out = np.where(soft <= ink, 0, np.where(soft >= white, 255, dots)).astype(np.uint8)
    if lines > 0.05:
        edges = cv2.Canny(cv2.GaussianBlur(grey, (0, 0), 1.6), 90, 200)
        out[cv2.dilate(edges, np.ones((2, 2), np.uint8)) > 0] = 0
    return out


def panel(picture: Path, cfg: dict[str, Any]) -> np.ndarray:
    """The picture's tones (grey, float) at the video's size, with a light tone over the
    whole panel: a printed comic screens its background too, not only the figure."""
    W, H = cfg["size"]
    img = cv2.imread(str(picture))
    h, w = img.shape[:2]
    s = max(W / w, H / h)
    img = cv2.resize(img, (int(w * s + 0.5), int(h * s + 0.5)), interpolation=cv2.INTER_AREA)
    y0, x0 = (img.shape[0] - H) // 2, (img.shape[1] - W) // 2
    grey = cv2.cvtColor(img[y0:y0 + H, x0:x0 + W], cv2.COLOR_BGR2GRAY)
    grey = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(grey).astype(np.float32)
    # a soft vignette of tone: paper reads as light dots, a little darker at the edges
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    r = np.hypot((xx - W / 2) / (W / 2), (yy - H * 0.45) / (H / 2))
    paper = float(cfg.get("paper_tone", 178)) - 26 * np.clip(r, 0, 1)
    return np.minimum(grey, paper)


def frames(art: np.ndarray, cfg: dict[str, Any]):
    """Each frame of the clip (uint8 RGB), in order. The tones are moved and blurred,
    then screened afresh every frame: the picture softens, the dots stay crisp."""
    H, W = art.shape
    fps, total = int(cfg["fps"]), float(cfg["seconds"])
    fin, hold, fout = float(cfg["fade_in"]), float(cfg["hold"]), float(cfg["fade_out"])
    move = max(0.1, total - fin - hold)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    edge = np.clip(np.hypot((xx - W / 2) / (W / 2), (yy - H * 0.42) / (H / 2)), 0, 1) ** 1.6
    cell = int(cfg["cell"] * W / 1080)
    for k in range(int(total * fps)):
        t = k / fps
        f = max(0.0, min(1.0, (t - fin - hold) / move))
        f = f * f * (3 - 2 * f)
        z = 1.0 + (float(cfg["push"]) - 1.0) * f
        M = cv2.getRotationMatrix2D((W / 2, H * 0.42), 0, z)
        g = cv2.warpAffine(art, M, (W, H), flags=cv2.INTER_LINEAR, borderValue=255)
        if f > 0:
            r = float(cfg["blur"]) * f
            soft = cv2.GaussianBlur(g, (0, 0), max(0.1, r))
            mild = cv2.GaussianBlur(g, (0, 0), max(0.1, r * 0.25))
            g = mild * (1 - edge) + soft * edge
        # lines only while sharp: they fade as the blur grows, the dots carry on
        img = screentone(np.clip(g, 0, 255).astype(np.uint8), cell, int(cfg["ink"]), int(cfg["white"]),
                         lines=1.0 - f).astype(np.float32)
        fade = min(1.0, t / fin) if fin else 1.0
        if t > total - fout:
            fade = min(fade, (total - t) / fout)
        img = img * fade + 255 * (1 - fade)
        yield cv2.cvtColor(np.clip(img, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2RGB)


def render(picture: Path, out: Path, config: dict[str, Any] | None = None,
           music: Path | None = None) -> Path:
    cfg = {**DEFAULTS, **((config or {}).get("comicpanel") or {})}
    art = panel(picture, cfg)                    # tones; screened per frame
    W, H = cfg["size"]
    total = float(cfg["seconds"])
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(cfg["fps"]), "-i", "-"]
    if music:
        import handplay
        cmd += ["-stream_loop", "-1", "-ss", f"{handplay.music_start(music):.2f}", "-i", str(music), "-filter_complex",
                f"[1:a]atrim=0:{total:.2f},asetpts=PTS-STARTPTS,afade=t=in:d=0.6,"
                f"afade=t=out:st={max(0, total - 1.2):.2f}:d=1.2,loudnorm=I=-16:TP=-1.5:LRA=11[a]",
                "-map", "0:v", "-map", "[a]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-t", f"{total:.3f}", "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        for img in frames(art, cfg):
            proc.stdin.write(img.tobytes())
    finally:
        proc.stdin.close()
        proc.wait()
    if proc.returncode:
        raise RuntimeError("ffmpeg failed")
    return out
