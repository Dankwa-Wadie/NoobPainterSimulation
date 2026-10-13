"""Risograph finish: turn any picture into a two- or three-ink riso print.

The look follows the riso films in sevenevesai/riso-windowseat (MIT): a few
spot inks laid over off-white paper, each a halftone screen at its own angle,
each drum a little out of register, the ink patchy where the drum ran dry, and
the paper's tooth showing through. Here it is a filter, so it works on the
local Qwen pictures (sketchbook.py) or any frame.

How a picture is separated: paper and inks are treated as filters, so a pixel
is paper * prod(ink_i ** coverage_i). In absorbance (-log) space that is linear,
and each pixel's ink coverages are a least-squares fit, clipped to [0, 1].

Everything is seeded, so the same picture and seed always print the same.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageFilter

# Riso drum colours (sRGB approximations of the published swatches).
INKS: dict[str, str] = {
    "black": "#2b2b2b", "blue": "#0078bf", "fluorescent_pink": "#ff48b0",
    "yellow": "#ffe800", "red": "#ff665e", "teal": "#00838a", "green": "#00a95c",
    "orange": "#ff6c2f", "purple": "#765ba7", "federal_blue": "#3d5588",
}
PALETTES: dict[str, list[str]] = {
    "pink_blue": ["fluorescent_pink", "blue"],
    "pink_blue_yellow": ["yellow", "fluorescent_pink", "blue"],
    "teal_orange": ["orange", "teal"],
    "night": ["fluorescent_pink", "federal_blue"],
    "classic": ["yellow", "red", "black"],
}
DEFAULTS: dict[str, Any] = {
    "palette": "pink_blue",
    "paper": "#f4efe4",
    "lpi": 9.0,               # halftone cells per 100 px of output; bigger is coarser
    "angles": [15, 75, 45, 0],  # one screen angle per ink, in order
    "misregister": 4.0,       # px: how far each drum may sit off
    "patchiness": 0.18,       # 0..1: how much the ink coverage breathes
    "grain": 0.06,            # 0..1: paper tooth
    "contrast": 1.15,         # pushes coverages apart before screening
}


def settings(config: dict[str, Any] | None = None) -> dict[str, Any]:
    return {**DEFAULTS, **((config or {}).get("riso") or {})}


def _rgb(hexcode: str) -> np.ndarray:
    h = hexcode.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float64) / 255.0


def inks_for(cfg: dict[str, Any]) -> list[np.ndarray]:
    names = cfg.get("inks") or PALETTES[cfg["palette"]]
    return [_rgb(INKS.get(n, n)) for n in names]


def separate(rgb: np.ndarray, inks: list[np.ndarray], paper: np.ndarray) -> np.ndarray:
    """Per-pixel coverage of each ink, shape (H, W, n), in [0, 1]."""
    # No real ink blocks a channel completely; without this floor a pure channel
    # (blue's red = 0) looks infinitely strong and its coverage collapses to ~0.
    target = -np.log(np.clip(rgb / paper, 0.02, 1.0))                  # (H, W, 3)
    basis = np.stack([-np.log(np.clip(i / paper, 0.12, 1.0)) for i in inks], axis=1)  # (3, n)
    cover = np.clip(target @ np.linalg.pinv(basis).T, 0.0, 1.0)        # (H, W, n)
    # Projected gradient steps: least squares with every coverage kept in [0, 1].
    step = 1.0 / np.linalg.norm(basis, 2) ** 2
    for _ in range(40):
        cover = np.clip(cover - step * ((cover @ basis.T - target) @ basis), 0.0, 1.0)
    return cover


def _noise(shape: tuple[int, int], scale: float, rng: np.random.Generator) -> np.ndarray:
    """Smooth value noise in [0, 1] with features about `scale` px across."""
    h, w = shape
    small = rng.random((max(2, int(h / scale) + 2), max(2, int(w / scale) + 2)))
    img = Image.fromarray((small * 255).astype(np.uint8)).resize((w, h), Image.BICUBIC)
    return np.asarray(img, dtype=np.float64) / 255.0


def screen(cover: np.ndarray, angle: float, cell: float, rng: np.random.Generator) -> np.ndarray:
    """Amplitude-modulated round dots: 1 where ink lands."""
    h, w = cover.shape
    y, x = np.mgrid[0:h, 0:w].astype(np.float64)
    a = np.deg2rad(angle)
    u = (x * np.cos(a) + y * np.sin(a)) * (2 * np.pi / cell)
    v = (-x * np.sin(a) + y * np.cos(a)) * (2 * np.pi / cell)
    spot = (np.cos(u) + np.cos(v)) * 0.25 + 0.5                        # 1 at dot centres
    rough = (rng.random((h, w)) - 0.5) * 0.12                          # ragged dot edges
    return (cover + rough > 1.0 - spot).astype(np.float64)


def _shift(plate: np.ndarray, dx: int, dy: int) -> np.ndarray:
    return np.roll(np.roll(plate, dy, axis=0), dx, axis=1)


def print_array(rgb: np.ndarray, cfg: dict[str, Any], seed: int = 0) -> np.ndarray:
    """rgb float (H, W, 3) in [0, 1] -> the riso print, same shape."""
    rng = np.random.default_rng(seed)
    h, w, _ = rgb.shape
    paper, inks = _rgb(cfg["paper"]), inks_for(cfg)
    cover = separate(rgb, inks, paper)
    c = float(cfg["contrast"])
    cover = np.clip((cover - 0.5) * c + 0.5, 0.0, 1.0)
    cell = 100.0 / float(cfg["lpi"])
    angles = list(cfg["angles"])
    out = np.ones((h, w, 3)) * paper
    grain = float(cfg["grain"])
    if grain:
        out *= 1.0 - grain * (rng.random((h, w, 1)) * 0.6 + _noise((h, w), 3, rng)[..., None] * 0.4)
    reach = float(cfg["misregister"])
    for k, ink in enumerate(inks):
        dots = screen(cover[..., k], angles[k % len(angles)], cell, rng)
        # A riso drum leaves a slightly soft dot; blur by a fraction of a pixel.
        dots = np.asarray(Image.fromarray((dots * 255).astype(np.uint8))
                          .filter(ImageFilter.GaussianBlur(0.6)), dtype=np.float64) / 255.0
        patch = 1.0 - float(cfg["patchiness"]) * _noise((h, w), max(h, w) / 6, rng)
        density = dots * patch * (0.88 + 0.12 * rng.random((h, w)))
        dx, dy = (int(round(d)) for d in rng.uniform(-reach, reach, 2))
        density = _shift(density, dx, dy)[..., None]
        out *= 1.0 - density * (1.0 - ink)                             # inks multiply like filters
    return np.clip(out, 0.0, 1.0)


def apply(src: Path | str, dst: Path | str, config: dict[str, Any] | None = None,
          size: tuple[int, int] | None = None, seed: int = 0) -> Path:
    """Print `src` to `dst`. `size` upscales first so the dots stay crisp at frame size."""
    cfg = settings(config)
    img = Image.open(src).convert("RGB")
    if size and img.size != tuple(size):
        img = img.resize(tuple(size), Image.LANCZOS)
    rgb = np.asarray(img, dtype=np.float64) / 255.0
    out = print_array(rgb, cfg, seed=seed)
    dst = Path(dst)
    Image.fromarray((out * 255 + 0.5).astype(np.uint8)).save(dst)
    return dst


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Give a picture a risograph finish.")
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--palette", default=DEFAULTS["palette"], choices=sorted(PALETTES))
    ap.add_argument("--lpi", type=float, default=DEFAULTS["lpi"])
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    apply(a.src, a.dst, {"riso": {"palette": a.palette, "lpi": a.lpi}}, seed=a.seed)


if __name__ == "__main__":
    main()
