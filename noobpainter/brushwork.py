"""Brushwork: a picture painted stroke by stroke, every stroke on record.

oilpaint.py covers areas with the easel's own `work` passes, which are quick
but say nothing about where the brush went. Here every stroke is planned in
Python, so its path and its moment on the painter's clock are known, and a
playback can put a hand on it (handplay.py).

The plan is layered, the way a painter refines (after Hertzmann's painterly
rendering, 1998): big brushes first, then smaller ones, each layer laying
strokes only where the canvas so far still differs from the reference. Between
layers the easel paints what has been planned and the result is compared, so
each layer answers the real paint, not a guess at it.

A stroke starts where the error is largest in its cell, runs along the
picture's forms (across the brightness gradient), and stops where the
reference turns away from its colour. Each layer's stroke colours are pooled
into a few piles mixed from the tube box (oilpaint.match).

The script prints `S <n> <t0> <t1>` around every stroke and `D <t0> <t1>` for
trips to the palette, in hand-time seconds (the easel's `hand_secs()`), so
strokes.json can be lined up with the frames' hand times.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageFilter

import oilpaint

LOG = logging.getLogger("mken.brushwork")

DEFAULTS: dict[str, Any] = {
    # The playback is a close-up that follows the brush (handplay "zoom" 2), so
    # frames are twice the video's width to stay sharp, and the easel paints
    # at about that size so there is detail to see.
    "width": 2000,                  # px the easel paints at
    "frame_width": 2160,
    # s of hand time between frames: ~6 MB each at 2160 (a 4 h painting every
    # 4 s at 1080 was 27 GB); the playback reveals each stroke between frames.
    "frames_every": 30.0,
    # One layer per brush, broad to fine: (kind, width in canvas units, error
    # threshold in CIELAB distance, piles, longest stroke in units, most strokes).
    # The budget keeps the strokes that fix the most; uncapped, the fine round
    # alone planned 35,000 strokes for no visible gain.
    # The first layer is never capped: it has to cover the canvas.
    "layers": [["flat", 44, 0, 8, 260, 0], ["filbert", 22, 9, 12, 180, 1800],
               ["filbert", 10, 11, 16, 110, 2200], ["round", 4.5, 14, 16, 60, 1000]],
    "strokes_per_dip": 4,
    "seed": 11,
    "style": "",
    # What the style presets below set (defaults: a plain painting from the reference).
    "palette": "classic",           # oilpaint.PALETTES: which tubes, which easel box
    "ground": [["lead white", 1]],  # the priming, a pile recipe
    "saturate": 1.0,                # chroma scale of the reference
    "contrast": 1.0,                # lightness spread of the reference, about mid grey
    "posterize": 0,                 # >0: flatten the reference to this many colours first
    "tint": None,                   # [[shadow rgb], [light rgb], amount]: dramatic colour
    "lay": 1.0,                     # paint a full brush lays (4+ is impasto)
    "contour": None,                # [width, recipe, most strokes]: dark lines along edges
    "prompt": "",                   # how the reference picture should be drawn (localimage)
    # Keep the subject hidden: after the first layer's rough block-in, every
    # stroke on the background goes first and every stroke on the subject
    # (found with rembg) is saved for the end.
    "subject_last": True,
    "medium": "oil",                # "pencil" (plan_drawing) or "pen" (plan_pen, lineart.py)
    "drawing": None,
    "pen": None,                    # pen line art: {"kind": maze|waves|continuous, ...}
    "compass": None,                # compass spiral: {"spacing", "lead"}
    "playback": None,               # handplay settings the style wants (zoom, hand)
    # Finishing passes (learned for the caricature and B&W styles from
    # a folder of example pictures by stylefit.py), run after the subject, before contours:
    # Levels for the video, as when a painting is photographed: [black in, white in,
    # gamma]. Paint never renders darker than ~40/255 (bone black under the
    # varnish's sheen), so digital-looking styles set the darkest paint to black.
    "grade": None,
    "blend": 0,                     # badger passes over the canvas: airbrush smoothness
    "accents": None,                # {"dark": L, "light": L, "width": w}: true blacks and whites
    "hatch": None,                  # {"below": L, "cross": L, "spacing": u, "length": u,
                                    #  "width": w, "recipe": [...]}: charcoal/manga hatching
}

# Painting styles. Each is chosen for a look that still reads at a glance:
# the guess-the-painting teaser only works if the finished painting is clear.
STYLES: dict[str, dict[str, Any]] = {
    "impressionist": {
        "palette": "modern", "ground": [["lead white", 6], ["yellow ochre", 1]],
        "saturate": 1.25, "contrast": 1.05,
        "layers": [["flat", 36, 0, 8, 110, 0], ["filbert", 18, 8, 12, 60, 2200],
                   ["filbert", 9, 10, 16, 34, 2800], ["round", 5, 12, 16, 18, 1400]],
        "prompt": ("impressionist oil painting, plein air, dappled sunlight, broken colour, "
                   "bright clear palette, simple strong composition, one clear subject"),
    },
    "van_gogh": {
        "palette": "modern", "ground": [["lead white", 3], ["yellow ochre", 1]],
        "saturate": 1.35, "contrast": 1.15, "lay": 4.0,
        "layers": [["flat", 40, 0, 8, 240, 0], ["filbert", 20, 8, 12, 170, 1900],
                   ["filbert", 11, 10, 16, 120, 2400]],
        "contour": [4.0, [["ultramarine blue", 2], ["vine black", 1]], 900],
        "prompt": ("post-impressionist oil painting in the style of Vincent van Gogh, swirling "
                   "rhythmic brushwork, bold outlines, vivid complementary colours, "
                   "one clear subject"),
    },
    "old_master": {
        "palette": "classic", "ground": [["raw umber", 1], ["lead white", 1]],
        "saturate": 1.0, "contrast": 1.2,
        "prompt": ("baroque oil painting, dramatic chiaroscuro, warm candlelight from one side, "
                   "dark background, one clear subject, old master style"),
    },
    "pop": {
        "palette": "modern", "ground": [["zinc white", 1]],
        "saturate": 1.5, "contrast": 1.2, "posterize": 7,
        "layers": [["flat", 40, 0, 8, 200, 0], ["flat", 20, 6, 8, 130, 1600],
                   ["round", 8, 8, 8, 60, 1200]],
        "contour": [5.0, [["vine black", 1]], 1100],
        "prompt": ("bold pop art illustration, flat bright colour areas, thick black outlines, "
                   "simple iconic composition, one clear subject on a plain background"),
    },
    # Baroque, after Caravaggio and Rembrandt: a near-black ground, one strong light,
    # warm glowing flesh against deep shadow; then the painting comes alive for a
    # few seconds after the reveal (handplay "alive": a depth-parallax sway).
    "baroque": {
        "palette": "classic", "ground": [["raw umber", 2], ["bone black", 1]],
        "saturate": 1.05, "contrast": 1.3, "grade": [18, 250, 0.95],
        "layers": [["flat", 40, 0, 8, 200, 0], ["filbert", 20, 7, 12, 140, 2000],
                   ["filbert", 9, 9, 16, 80, 2600], ["round", 4, 11, 16, 40, 1500]],
        "playback": {"alive": 4.0},
        "prompt": ("baroque oil painting in the manner of Caravaggio and Rembrandt, dramatic "
                   "chiaroscuro, a single warm light source from the upper left, deep near-black "
                   "background, rich glowing skin tones, velvet and gold, one figure, "
                   "museum masterpiece"),
    },
    # From the references in a folder of example pictures (2026-10-12): style only, the
    # prompts describe original, fictional characters, never those pictures' people.
    "caricature": {
        "palette": "modern", "ground": [["lead white", 2], ["raw umber", 1]],
        "saturate": 1.2, "contrast": 1.25,
        "layers": [["flat", 40, 0, 8, 200, 0], ["filbert", 20, 8, 12, 140, 1800],
                   ["filbert", 9, 10, 16, 80, 2600], ["round", 4, 12, 16, 40, 1400]],
        "contour": [2.5, [["raw umber", 1], ["vine black", 2]], 600],
        "grade": [38, 245, 0.95],
        # Pushed hard: a milder prompt drew a realistic portrait, not a caricature.
        "prompt": ("extreme caricature, the head three times too big for the tiny body and "
                   "thin neck, enormous bulging eyes, huge nose and lips, exaggerated "
                   "expression, smooth airbrushed digital painting like a professional "
                   "caricature artist, dramatic rim light, plain dark gradient background, "
                   "full head and shoulders, one original fictional character, not a real person"),
    },
    "bw_exaggerated": {
        "palette": "mono", "ground": [["zinc white", 3], ["raw umber", 1]],
        "saturate": 0.0, "contrast": 1.45,
        "layers": [["flat", 40, 0, 8, 200, 0], ["filbert", 18, 8, 10, 120, 1900],
                   ["filbert", 9, 9, 12, 70, 2400], ["round", 4, 11, 12, 36, 1300]],
        "contour": [3.0, [["vine black", 1]], 900],
        # stylefit (2026-10-12): plain brushwork with a light grade beat every hatching
        # and accent variant against the folder (style 1.67 vs 2.28; fidelity 0.49 vs 0.37)
        "grade": [20, 252, 0.85],
        "prompt": ("black and white exaggerated portrait, distorted proportions, intense "
                   "unblinking stare, heavy brow, deep shadows, very high contrast, ink and "
                   "charcoal, manga horror shading, plain background, one original fictional "
                   "character, not a real person, monochrome"),
    },
    # Graphite on paper (after an example pencil drawing): hard construction
    # lines, hatching from HB to 8B and black chalk for the deepest darks, long
    # vertical strokes in the background, the eraser lifting the highlights.
    "pencil": {
        "medium": "pencil", "palette": "mono", "saturate": 0.0, "contrast": 1.35,
        "grade": [55, 236, 1.0],                          # graphite is grey: bring its darks down
        "drawing": {
            "construct": ["2H", 0.35, 500],               # grade, pressure, most lines
            # grade, darker than L, cross-hatch below L, spacing, length, pressure, most
            # lines: unbudgeted, the darks alone planned 500,000 strokes
            # tone, ruled line by line against a ruler: grade, darker than L, cross-hatch
            # below L, spacing, pressure, most lines (the area hatch laid dozens of lines
            # in one operation, which no hand motion could match)
            # Tone engraved in long ruled lines across the whole picture, one direction
            # per layer (the square-by-square shading left an obvious block pattern):
            # grade, darker than L, angle (rad), spacing, pressure, most lines
            "engraved": [["2B", 85, 0.78, 4.4, 0.7, 4200], ["4B", 66, -0.78, 4.0, 0.85, 4600],
                         ["8B", 48, 0.18, 3.4, 1.0, 5000], ["chalk", 32, -1.25, 3.0, 1.0, 4200]],
            "ruler_length": 300,
            # then lines with character over the tone (fewer: the tone does the darkening)
            "passes": [["2B", 62, 40, 9, 44, 0.7, 700], ["6B", 42, 26, 8, 52, 0.85, 900],
                       ["8B", 28, 16, 7, 60, 0.95, 700]],
            "flat_angle": 1.5708,                         # untextured areas: vertical strokes
            "calm": 18,                                   # long parallel strokes, not scribbles
            "sets": [0.0, 0.85],                          # along the form, then a steady cross
            "accents": ["4B", 0.8, 700],
            "erase": [88, 7, 900],                        # lighter than L, width, most strokes
        },
        "playback": {"zoom": 1.6, "hand": "pencil", "ruler": True},
        "prompt": ("detailed graphite pencil drawing on white paper, exaggerated distorted "
                   "portrait, heavy dark hatching, dramatic shadows, intense stare, eerie mood, "
                   "one original fictional character, not a real person, black and white"),
    },
    # Pen line art (lineart.py): every mark one pen stroke, so the hand matches the line.
    "maze": {
        "medium": "pen", "palette": "modern", "ground": [["zinc white", 1]],
        "saturate": 0.0, "contrast": 1.4, "grade": [45, 240, 1.0],
        # The maze is real and solvable; its solution isn't drawn (not wanted, 2026-10-12).
        "pen": {"kind": "maze", "width": 1.6, "per_dip": 14, "pressure": [0.6, 0.6],
                "solution": False},
        "playback": {"zoom": 1.6, "hand": "pen"},
        "prompt": ("bold high-contrast black and white illustration, one clear subject with a "
                   "strong recognisable silhouette, dramatic lighting, plain white background"),
    },
    "waves": {
        "medium": "pen", "palette": "modern", "ground": [["zinc white", 1]],
        "saturate": 0.0, "contrast": 1.4, "grade": [45, 240, 1.0],
        "pen": {"kind": "waves", "width": 1.8, "per_dip": 8, "pressure": [0.35, 1.0],
                "args": {"amplitude": 4.6}},
        "playback": {"zoom": 1.6, "hand": "pen"},
        "prompt": ("bold high-contrast black and white portrait illustration, one clear subject, "
                   "strong light and shadow, plain light background"),
    },
    "continuous": {
        "medium": "pen", "palette": "modern", "ground": [["zinc white", 1]],
        "saturate": 0.0, "contrast": 1.4, "grade": [45, 240, 1.0],
        "pen": {"kind": "continuous", "width": 1.8, "per_dip": 8, "pressure": [0.35, 1.0],
                "args": {"amplitude": 4.6}},
        "subject_last": False,                      # one unbroken line can't be reordered
        "playback": {"zoom": 1.6, "hand": "pen"},
        "prompt": ("bold high-contrast black and white portrait illustration, one clear subject, "
                   "strong light and shadow, plain light background"),
    },
    # Relief carving (carving.py): cut from a flat block in Blender, not painted.
    "relief_wood": {
        "medium": "carving", "material": "wood",
        "playback": {"zoom": 1.5, "hand": "carving"},
        "prompt": ("one clear subject with a strong recognisable silhouette, simple composition, "
                   "plain background, front view, sculptural forms"),
    },
    "relief_marble": {
        "medium": "carving", "material": "marble",
        "playback": {"zoom": 1.5, "hand": "carving"},
        "prompt": ("one clear subject with a strong recognisable silhouette, simple composition, "
                   "plain background, front view, classical sculptural forms"),
    },
    "relief_clay": {                                 # clay is moulded, not carved
        "medium": "carving", "process": "mould", "material": "clay",
        "playback": {"zoom": 1.5, "hand": "mould"},
        "prompt": ("one clear subject with a strong recognisable silhouette, simple composition, "
                   "plain background, front view, sculptural forms"),
    },
    # A pencil in a compass: the picture drawn as one spiral of compass arcs from
    # the outside in, its weight carrying the picture (lineart.spiral).
    "compass_spiral": {
        # plain white paper: the strong grade turned a warm ground tan
        "medium": "compass", "palette": "mono", "ground": [["zinc white", 1]],
        # graphite rings this far apart build little tone: a strong grade (60 left it pale)
        "saturate": 0.0, "contrast": 1.35, "grade": [118, 236, 0.8],
        "subject_last": False,                      # a spiral can't be reordered
        "compass": {"spacing": 6.0, "lead": "8B", "inward": "auto"},
        "playback": {"zoom": 1.6, "hand": "none", "compass": True},
        "prompt": ("high-contrast black and white portrait illustration, one clear subject filling "
                   "the frame, strong light and shadow, plain light background"),
    },
    # (bizarre_manga is drawn in ink, then coloured, by manga.py, not painted in oil)
    "contemporary": {
        "palette": "modern", "ground": [["zinc white", 1]],
        "saturate": 1.3, "contrast": 1.1,
        "contour": [2.5, [["raw umber", 1], ["vine black", 1]], 700],
        "prompt": ("contemporary painting, confident expressive brushwork, vivid modern colour, "
                   "crisp shapes, strong light, one clear subject"),
    },
}


def settings(config: dict[str, Any] | None = None) -> dict[str, Any]:
    own = (config or {}).get("brushwork") or {}
    style = own.get("style") or DEFAULTS["style"]
    if style and style not in STYLES:
        raise ValueError(f"unknown style {style!r} (styles: {', '.join(STYLES)})")
    return {**DEFAULTS, **STYLES.get(style, {}), **own}


# --- the reference --------------------------------------------------------------

def prepare(ref: np.ndarray, cfg: dict[str, Any], seed: int = 0) -> np.ndarray:
    """The reference as the style wants it: more vivid, more contrast, or flattened."""
    lab = oilpaint._lab(ref)
    if cfg.get("posterize"):
        flat = ref.reshape(-1, 3)
        rng = np.random.default_rng(seed)
        sample = flat[rng.choice(len(flat), min(len(flat), 40000), replace=False)]
        _, centres = oilpaint.kmeans(sample, int(cfg["posterize"]), seed)
        c_lab = oilpaint._lab(centres)
        labels = np.argmin(((lab.reshape(-1, 1, 3) - c_lab[None]) ** 2).sum(-1), axis=1)
        # Smooth the region labels, not the colours: a per-channel filter on
        # the colours invents in-between ones and the flat areas go muddy.
        idx = Image.fromarray(labels.reshape(ref.shape[:2]).astype(np.uint8))
        labels = np.asarray(idx.filter(ImageFilter.ModeFilter(5)))
        ref = centres[labels]
        lab = oilpaint._lab(ref)
    if cfg.get("tint"):
        # dramatic colour: shadows pushed toward one hue, lights toward another
        # (the purple-shaded skin and gold lights of bold manga colour)
        shade, light, amount = cfg["tint"]
        lum = lab[..., 0:1] / 100.0
        w_dark = np.clip(1.0 - lum * 1.6, 0, 1) * amount
        w_light = np.clip(lum * 1.6 - 0.6, 0, 1) * amount
        ref = np.clip(ref * (1 - w_dark - w_light) + np.array(shade) * w_dark
                      + np.array(light) * w_light, 0, 1)
        lab = oilpaint._lab(ref)
    sat, con = float(cfg.get("saturate", 1)), float(cfg.get("contrast", 1))
    if sat == 1 and con == 1:
        return ref
    lab = lab.copy()
    lab[..., 0] = np.clip(50 + (lab[..., 0] - 50) * con, 0, 100)
    lab[..., 1:] *= sat
    return _rgb(lab)


def _rgb(lab: np.ndarray) -> np.ndarray:
    """CIELAB (D65) -> sRGB in [0, 1], clipped."""
    fy = (lab[..., 0] + 16) / 116
    fx, fz = fy + lab[..., 1] / 500, fy - lab[..., 2] / 200
    def inv(f: np.ndarray) -> np.ndarray:
        return np.where(f ** 3 > 216 / 24389, f ** 3, (116 * f - 16) / (24389 / 27))
    xyz = np.stack([inv(fx) * 0.95047, inv(fy), inv(fz) * 1.08883], axis=-1)
    m = np.array([[3.2406, -0.9689, 0.0557], [-1.5372, 1.8758, -0.2040], [-0.4986, 0.0415, 1.0570]])
    c = np.clip(xyz @ m, 0, 1)
    return np.clip(np.where(c > 0.0031308, 1.055 * c ** (1 / 2.4) - 0.055, 12.92 * c), 0, 1)


# --- planning a layer -----------------------------------------------------------

def _sample(img: np.ndarray, x: float, y: float) -> np.ndarray:
    h, w = img.shape[:2]
    return img[min(h - 1, max(0, int(y))), min(w - 1, max(0, int(x)))]


def plan_layer(ref: np.ndarray, canvas: np.ndarray | None, brush: float, threshold: float,
               longest: float, rng: np.random.Generator, budget: int = 0
               ) -> list[dict[str, Any]]:
    """Strokes for one layer. ref/canvas are float RGB at 1 px per canvas unit."""
    h, w, _ = ref.shape
    blur = np.asarray(Image.fromarray((ref * 255).astype(np.uint8))
                      .filter(ImageFilter.GaussianBlur(brush * 0.5)), dtype=np.float64) / 255.0
    lab = oilpaint._lab(blur)
    err = (np.full((h, w), 1e3) if canvas is None
           else np.sqrt(((oilpaint._lab(canvas) - lab) ** 2).sum(-1)))
    lum = lab[..., 0]
    gy, gx = np.gradient(lum)
    step = max(2.0, brush * 0.5)
    grid = max(3, int(brush * 0.8))
    strokes = []
    for cy in range(0, h, grid):
        for cx in range(0, w, grid):
            cell = err[cy:cy + grid, cx:cx + grid]
            if cell.mean() <= threshold:
                continue
            iy, ix = np.unravel_index(int(np.argmax(cell)), cell.shape)
            x, y = float(cx + ix), float(cy + iy)
            colour = _sample(blur, x, y)
            c_lab = _sample(lab, x, y)
            pts, heading = [(x, y)], None
            while len(pts) * step < longest:
                gxv, gyv = _sample(gx, x, y), _sample(gy, x, y)
                if np.hypot(gxv, gyv) < 0.4:              # flat: carry on as we were
                    if heading is None:
                        heading = rng.uniform(0, np.pi)
                    dx, dy = np.cos(heading), np.sin(heading)
                else:
                    dx, dy = -gyv, gxv                    # along the form
                    norm = np.hypot(dx, dy)
                    dx, dy = dx / norm, dy / norm
                    if heading is not None and dx * np.cos(heading) + dy * np.sin(heading) < 0:
                        dx, dy = -dx, -dy                 # don't double back
                    heading = np.arctan2(dy, dx)
                x, y = x + dx * step, y + dy * step
                if not (0 <= x < w and 0 <= y < h):
                    break
                if len(pts) >= 3 and np.linalg.norm(_sample(lab, x, y) - c_lab) > threshold + 6:
                    break
                pts.append((x, y))
            if len(pts) < 2:                              # a dab: a short tick along the form
                a = rng.uniform(0, np.pi)
                pts.append((x + np.cos(a) * brush, y + np.sin(a) * brush))
            strokes.append({"pts": [(round(px, 1), round(py, 1)) for px, py in pts],
                            "rgb": colour.tolist(), "err": float(cell.mean())})
    if budget and len(strokes) > budget:
        # Drawn by error, not the worst first: the worst alone crowd onto a few
        # bright edges and leave whole areas unanswered.
        w = np.array([s["err"] for s in strokes]) + 1e-6
        keep = rng.choice(len(strokes), budget, replace=False, p=w / w.sum())
        strokes = [strokes[i] for i in sorted(keep)]
    return strokes


def plan_contours(ref: np.ndarray, width: float, budget: int, rng: np.random.Generator
                  ) -> list[dict[str, Any]]:
    """Dark lines traced along the reference's strongest edges."""
    lum = oilpaint._lab(np.asarray(Image.fromarray((ref * 255).astype(np.uint8))
                                   .filter(ImageFilter.GaussianBlur(1.5)), dtype=np.float64) / 255.0)[..., 0]
    gy, gx = np.gradient(lum)
    mag = np.hypot(gx, gy)
    h, w = mag.shape
    strong = np.percentile(mag, 94)
    used = np.zeros((h, w), dtype=bool)
    ys, xs = np.nonzero(mag > strong)
    order = rng.permutation(len(xs))
    step, out = max(2.0, width), []
    for k in order:
        if len(out) >= budget:
            break
        x, y = float(xs[k]), float(ys[k])
        if used[int(y), int(x)]:
            continue
        pts, heading = [(x, y)], None
        for _ in range(40):
            gxv, gyv = _sample(gx, x, y), _sample(gy, x, y)
            n = np.hypot(gxv, gyv)
            if n < strong * 0.5:
                break
            dx, dy = -gyv / n, gxv / n
            if heading is not None and dx * heading[0] + dy * heading[1] < 0:
                dx, dy = -dx, -dy
            heading = (dx, dy)
            x, y = x + dx * step, y + dy * step
            if not (0 <= x < w and 0 <= y < h) or used[int(y), int(x)]:
                break
            pts.append((x, y))
        if len(pts) < 4:
            continue
        for px, py in pts:                              # no second line on top of this one
            used[max(0, int(py) - 4):int(py) + 5, max(0, int(px) - 4):int(px) + 5] = True
        out.append({"pts": [(round(px, 1), round(py, 1)) for px, py in pts], "rgb": [0, 0, 0],
                    "err": 0.0, "pile": 0})
    return out


def plan_accents(ref: np.ndarray, canvas: np.ndarray | None, dark: float, light: float,
                 width: float, budget: int, rng: np.random.Generator) -> list[dict[str, Any]]:
    """Short strokes forcing true blacks (pile 0) and highlight whites (pile 1).

    Pooling a layer's colours into a few piles averages the extremes away: a
    painting never reached the deep blacks or the sharp highlights its
    reference has. These put them back where the reference is that dark or
    that light and the canvas so far is not.
    """
    lab = oilpaint._lab(ref)
    L = lab[..., 0]
    have = oilpaint._lab(canvas)[..., 0] if canvas is not None else np.full_like(L, 50.0)
    gy, gx = np.gradient(L)
    h, w = L.shape
    step = max(2.0, width * 0.6)
    out = []
    for want, pile, test in ((dark, 0, lambda v: v < dark), (light, 1, lambda v: v > light)):
        ys, xs = np.nonzero(test(L) & (np.abs(have - L) > 8))
        if not len(xs):
            continue
        pick = rng.choice(len(xs), min(len(xs), budget // 2), replace=False)
        for k in pick:
            x, y = float(xs[k]), float(ys[k])
            pts = [(x, y)]
            for _ in range(int(rng.integers(2, 6))):
                gxv, gyv = _sample(gx, x, y), _sample(gy, x, y)
                n = np.hypot(gxv, gyv) or 1.0
                x, y = x - gyv / n * step, y + gxv / n * step
                if not (0 <= x < w and 0 <= y < h) or not test(_sample(L, x, y)):
                    break
                pts.append((x, y))
            if len(pts) == 1:
                pts.append((x + step, y))
            out.append({"pts": [(round(a, 1), round(b, 1)) for a, b in pts], "pile": pile,
                        "rgb": [0.05, 0.05, 0.05] if pile == 0 else [0.97, 0.96, 0.93], "err": 0.0})
    return out


def plan_hatching(ref: np.ndarray, below: float, cross: float, spacing: float, length: float,
                  rng: np.random.Generator, flat_angle: float = -0.8, calm: float = 2.0,
                  sets: tuple[float, float] = (0.5, -0.6), budget: int = 0
                  ) -> list[dict[str, Any]]:
    """Parallel hatching over the darks, cross-hatched in the deepest, along the form.

    `calm` blurs the picture before the direction is read: large for a draughtsman's
    long parallel strokes, small for strokes that follow every wrinkle. `sets` are
    the two hatching directions, offset from the form's. `budget` keeps that many,
    drawn by darkness."""
    def lightness(radius: float) -> np.ndarray:
        img = Image.fromarray((ref * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(radius))
        return oilpaint._lab(np.asarray(img, dtype=np.float64) / 255.0)[..., 0]
    L = lightness(2)
    gy, gx = np.gradient(lightness(calm))
    h, w = L.shape
    out = []
    for cy in np.arange(spacing / 2, h, spacing):
        for cx in np.arange(spacing / 2, w, spacing):
            x, y = cx + rng.uniform(-spacing / 3, spacing / 3), cy + rng.uniform(-spacing / 3, spacing / 3)
            v = _sample(L, x, y)
            if v >= below:
                continue
            gxv, gyv = _sample(gx, x, y), _sample(gy, x, y)
            base = np.arctan2(gxv, -gyv) if np.hypot(gxv, gyv) > 0.3 else flat_angle
            # darker: longer and more of them; deepest: a second set across
            for a in [base + sets[0]] + ([base + sets[1]] if v < cross else []):
                half = length * (0.5 + 0.5 * (below - v) / below) / 2
                dx, dy = np.cos(a) * half, np.sin(a) * half
                out.append({"pts": [(round(x - dx, 1), round(y - dy, 1)), (round(x, 1), round(y, 1)),
                                    (round(x + dx, 1), round(y + dy, 1))],
                            "pile": 0, "rgb": [0.05, 0.05, 0.05], "err": float(below - v)})
    if budget and len(out) > budget:
        w = np.array([o["err"] for o in out]) + 1.0
        out = [out[i] for i in rng.choice(len(out), budget, replace=False, p=w / w.sum())]
    order = rng.permutation(len(out))
    return [out[i] for i in order]


def grade_lut(grade: list[float] | None) -> list[int] | None:
    """A 256-entry levels curve for PIL's Image.point (per channel), or None."""
    if not grade:
        return None
    lo, hi, gamma = float(grade[0]), float(grade[1]), float(grade[2] if len(grade) > 2 else 1.0)
    x = np.clip((np.arange(256) - lo) / max(1.0, hi - lo), 0, 1) ** gamma
    return list((x * 255 + 0.5).astype(int)) * 3


def apply_grade(img: Image.Image, grade: list[float] | None) -> Image.Image:
    lut = grade_lut(grade)
    return img.point(lut) if lut else img


def band_map(ref: np.ndarray, cells: int = 250) -> tuple[str, int, int]:
    """The reference's lightness as a Lua string, one letter per cell, 'A' + L/4."""
    h = int(round(cells * ref.shape[0] / ref.shape[1]))
    small = np.asarray(Image.fromarray((ref * 255).astype(np.uint8)).resize((cells, h), Image.LANCZOS),
                       dtype=np.float64) / 255.0
    L = np.clip(oilpaint._lab(small)[..., 0] / 4, 0, 25).astype(int)
    return "".join(chr(65 + v) for v in L.ravel()), cells, h


BAND_FN = """
function band(below)
  local limit = math.floor(below / 4)
  return mask(function(x, y)
    local cx = math.min(BW - 1, math.floor(x / W * BW))
    local cy = math.min(BH - 1, math.floor(y / H * BH))
    return (string.byte(BANDS, cy * BW + cx + 1) - 65) < limit and 1 or 0
  end):soften(2)
end
"""


def plan_shading(ref: np.ndarray, below: float, cross: float, tile: float, calm: float,
                 flat_angle: float) -> list[dict[str, Any]]:
    """Tiles to shade with the pencil's own area hatch, darker than `below`.

    Each tile gets one hatch, along its dominant form (or `flat_angle` where the
    picture is flat), and a second across it where it is darker than `cross`. The
    tile's `pts` is the path the hand sweeps while it shades."""
    L = oilpaint._lab(ref)[..., 0]
    soft = oilpaint._lab(np.asarray(Image.fromarray((ref * 255).astype(np.uint8))
                                    .filter(ImageFilter.GaussianBlur(calm)), dtype=np.float64) / 255.0)[..., 0]
    gy, gx = np.gradient(soft)
    h, w = L.shape
    out = []
    for y in np.arange(0, h, tile):
        for x in np.arange(0, w, tile):
            cell = L[int(y):int(y + tile), int(x):int(x + tile)]
            dark = cell < below
            if dark.mean() < 0.03:
                continue
            ex, ey = gx[int(y):int(y + tile), int(x):int(x + tile)], gy[int(y):int(y + tile), int(x):int(x + tile)]
            jxx, jyy, jxy = (ex * ex).mean(), (ey * ey).mean(), (ex * ey).mean()
            strength = np.hypot(jxx - jyy, 2 * jxy) / (jxx + jyy + 1e-9)
            # along the form: perpendicular to the dominant gradient
            angle = 0.5 * np.arctan2(2 * jxy, jxx - jyy) + np.pi / 2 if strength > 0.3 else flat_angle
            t = float(tile)
            out.append({"pts": [(round(x + 0.1 * t, 1), round(y + 0.25 * t, 1)),
                                (round(x + 0.9 * t, 1), round(y + 0.5 * t, 1)),
                                (round(x + 0.1 * t, 1), round(y + 0.75 * t, 1))],
                        "tile": [round(float(x), 1), round(float(y), 1), t],
                        "angle": round(float(angle), 3),
                        "cross": bool(np.median(cell[dark]) < cross),
                        "pile": 0, "rgb": [0.1, 0.1, 0.1], "err": float(below - cell.mean())})
    return out


def plan_ruled(ref: np.ndarray, below: float, cross: float, spacing: float, tile: float,
               calm: float, flat_angle: float, budget: int, rng: np.random.Generator
               ) -> list[dict[str, Any]]:
    """Ruled hatching: in each square the picture is darker than `below`, parallel
    pencil lines laid against a ruler at the square's form angle, kept to where it
    is that dark; a second set across them where it is darker than `cross`.

    Every line is its own stroke, so the playback can lay the ruler and run the
    pencil along each one. (The easel's area hatch laid dozens of lines in one
    operation, and the hand could only wave over the square.) Lines run a little
    past their square at random, so the squares don't show as a grid."""
    L = oilpaint._lab(ref)[..., 0]
    h, w = L.shape
    out = []
    for t in plan_shading(ref, below, cross, tile, calm, flat_angle):
        x0, y0, size = t["tile"]
        # each square's angle wanders a little: identical angles made the grid show
        base = t["angle"] + rng.normal(0, 0.12)
        sets = [base] + ([base + 0.9] if t["cross"] else [])
        for k, ang in enumerate(sets):
            limit = below if k == 0 else cross
            d = np.array([np.cos(ang), np.sin(ang)])
            n = np.array([-d[1], d[0]])
            c = np.array([x0 + size / 2, y0 + size / 2])
            reach = size * 0.75
            for off in np.arange(-reach, reach, spacing) + rng.uniform(0, spacing):
                ts = np.arange(-reach, reach, 2.0)
                pts = c + off * n + ts[:, None] * d
                over = rng.uniform(0, 8)
                inside = ((pts[:, 0] >= x0 - over) & (pts[:, 0] < x0 + size + over) &
                          (pts[:, 1] >= y0 - over) & (pts[:, 1] < y0 + size + over) &
                          (pts[:, 0] >= 0) & (pts[:, 0] < w) & (pts[:, 1] >= 0) & (pts[:, 1] < h))
                ok = np.zeros(len(pts), dtype=bool)
                ok[inside] = L[pts[inside, 1].astype(int), pts[inside, 0].astype(int)] < limit
                idx = np.nonzero(ok)[0]
                if not len(idx):
                    continue
                for run in np.split(idx, np.nonzero(np.diff(idx) > 1)[0] + 1):
                    if len(run) < 3:                       # shorter than ~6 units: no line
                        continue
                    a, b = pts[run[0]], pts[run[-1]]
                    dark = float(limit - L[int(c[1]) if 0 <= c[1] < h else 0, int(c[0]) if 0 <= c[0] < w else 0])
                    out.append({"pts": [(round(a[0], 1), round(a[1], 1)), (round(b[0], 1), round(b[1], 1))],
                                "ruler": round(float(ang), 3), "pile": 0, "rgb": [0.2, 0.2, 0.2],
                                "err": max(1.0, dark)})
    if budget and len(out) > budget:
        wgt = np.array([o["err"] for o in out])
        keep = sorted(rng.choice(len(out), budget, replace=False, p=wgt / wgt.sum()))
        out = [out[i] for i in keep]
    return out


def plan_ruled_lines(ref: np.ndarray, below: float, angle: float, spacing: float,
                     longest: float, budget: int, rng: np.random.Generator) -> list[dict[str, Any]]:
    """Engraver's hatching: parallel ruled lines across the whole picture at one
    angle, each drawn only where the picture is darker than `below` and broken
    where it lightens. No squares, so no square edges: tone builds smoothly, and
    the layers' different angles cross as cross-hatching. Runs are cut at `longest`
    (about a ruler). Spacing and angle wander a little, as a hand's do."""
    L = oilpaint._lab(np.asarray(Image.fromarray((ref * 255).astype(np.uint8))
                                 .filter(ImageFilter.GaussianBlur(1.5)), dtype=np.float64) / 255.0)[..., 0]
    h, w = L.shape
    reach = np.hypot(w, h) / 2
    c = np.array([w / 2, h / 2])
    out = []
    off = -reach
    while off < reach:
        a = angle + rng.normal(0, 0.015)
        d = np.array([np.cos(a), np.sin(a)])
        n = np.array([-d[1], d[0]])
        ts = np.arange(-reach, reach, 2.0)
        pts = c + off * n + ts[:, None] * d
        inside = (pts[:, 0] >= 0) & (pts[:, 0] < w) & (pts[:, 1] >= 0) & (pts[:, 1] < h)
        ok = np.zeros(len(pts), dtype=bool)
        ok[inside] = L[pts[inside, 1].astype(int), pts[inside, 0].astype(int)] < below
        idx = np.nonzero(ok)[0]
        for run in (np.split(idx, np.nonzero(np.diff(idx) > 2)[0] + 1) if len(idx) else []):
            if len(run) < 4:
                continue
            step = max(4, int(longest / 2))
            for i in range(0, len(run), step):
                part = run[i:i + step]
                if len(part) < 4:
                    continue
                p0, p1 = pts[part[0]], pts[part[-1]]
                dark = float(below - L[pts[part, 1].astype(int), pts[part, 0].astype(int)].mean())
                out.append({"pts": [(round(p0[0], 1), round(p0[1], 1)), (round(p1[0], 1), round(p1[1], 1))],
                            "ruler": round(float(a), 3), "pile": 0, "rgb": [0.2, 0.2, 0.2],
                            "err": max(1.0, dark)})
        off += spacing * rng.uniform(0.8, 1.2)
    if budget and len(out) > budget:                 # thin evenly, favouring the darkest
        wgt = np.array([o["err"] for o in out])
        keep = sorted(rng.choice(len(out), budget, replace=False, p=wgt / wgt.sum()))
        out = [out[i] for i in keep]
    return out


def plan_drawing(ref: np.ndarray, cfg: dict[str, Any], rng: np.random.Generator
                 ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """A pencil drawing: (groups to order subject-last, finishing groups).

    Tone first, the way a draughtsman shades, then lines: single hatching lines
    alone (12,000 of them) only scratched the paper pale grey, so each darkness
    band is shaded with the pencil's area hatch, and the lines come after."""
    d = cfg["drawing"]
    groups, finish = [], []
    grade, pressure, most = d["construct"]
    lines = plan_contours(ref, 2.0, int(most), rng)
    bands, bw, bh = band_map(ref)
    groups.append({"layer": 0, "tool": grade, "pressure": float(pressure), "width": 1.6,
                   "strokes": lines, "bands": (bands, bw, bh)})
    for grade, below, angle, spacing, pressure, most in d.get("engraved") or []:
        lines = plan_ruled_lines(ref, float(below), float(angle), float(spacing),
                                 float(d.get("ruler_length", 300)), int(most), rng)
        # ruled in order across the page, line after line, as the ruler is moved down
        groups.append({"layer": len(groups), "tool": grade, "ruled": True, "pressure": float(pressure),
                       "width": 2.0, "strokes": lines})
    for grade, below, cross, spacing, pressure, most in d.get("ruled") or []:
        lines = plan_ruled(ref, float(below), float(cross), float(spacing), float(d.get("tile", 120)),
                           float(d.get("calm", 2.0)), float(d.get("flat_angle", -0.8)), int(most), rng)
        groups.append({"layer": len(groups), "tool": grade, "ruled": True, "pressure": float(pressure),
                       "width": 2.0, "strokes": painter_order(lines, float(d.get("tile", 120)))})
    for k, (grade, below, cross, spacing, length, pressure) in enumerate(d.get("shade") or []):
        tiles = plan_shading(ref, float(below), float(cross), float(d.get("tile", 90)),
                             float(d.get("calm", 2.0)), float(d.get("flat_angle", -0.8)))
        groups.append({"layer": len(groups), "tool": grade, "shade": [float(below), float(spacing),
                                                                       float(length)],
                       "pressure": float(pressure), "width": float(d.get("tile", 90)) * 0.5,
                       "strokes": painter_order(tiles, float(d.get("tile", 90)))})
    for n, (grade, below, cross, spacing, length, pressure, *most) in enumerate(d["passes"], start=1):
        hatch = plan_hatching(ref, float(below), float(cross), float(spacing), float(length), rng,
                              float(d.get("flat_angle", -0.8)), float(d.get("calm", 2.0)),
                              tuple(d.get("sets", (0.5, -0.6))), int(most[0]) if most else 0)
        groups.append({"layer": len(groups), "tool": grade, "pressure": float(pressure), "width": 2.0,
                       "strokes": painter_order([{**h, "pile": 0} for h in hatch], 120)})
    grade, pressure, most = d["accents"]
    finish.append({"layer": len(groups), "tool": grade, "pressure": float(pressure), "width": 1.8,
                   "strokes": plan_contours(ref, 1.5, int(most), rng)})
    lighter, width, most = d["erase"]
    lights = [a for a in plan_accents(ref, None, -1, float(lighter), float(width), int(most) * 2, rng)
              if a["pile"] == 1]
    finish.append({"layer": len(groups) + 1, "tool": "eraser", "width": float(width),
                   "strokes": lights})
    finish.append({"layer": len(groups) + 2, "tool": "fix", "strokes": []})
    for g in groups + finish:
        for st in g["strokes"]:
            st.update(layer=g["layer"], brush=g["tool"], width=g["width"])
    LOG.info("drawing: %s", ", ".join(f"{g['tool']} {len(g['strokes'])}" for g in groups + finish))
    return groups, finish


def plan_pen(ref: np.ndarray, cfg: dict[str, Any], rng: np.random.Generator
             ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pen line art (lineart.py): every line one or more pen strokes the hand follows."""
    import lineart
    p = cfg["pen"]
    art = lineart.KINDS[p["kind"]](ref, rng, **(p.get("args") or {}))
    longest = int(p.get("segment", 24))              # points per stroke: the pen re-inks between
    dark = lineart.darkness(ref, blur=2)
    lo, hi = p.get("pressure", [0.6, 0.6])           # pressure from light to dark: thicker in the darks

    def strokes_of(lines: list[list[tuple[float, float]]]) -> list[dict[str, Any]]:
        out = []
        h, w = dark.shape
        for line in lines:
            for i in range(0, max(1, len(line) - 1), longest):
                part = line[i:i + longest + 1]       # consecutive parts share an end: no gaps
                if len(part) >= 2:
                    d = float(np.mean([dark[min(h - 1, max(0, int(y))), min(w - 1, max(0, int(x)))]
                                       for x, y in part]))
                    out.append({"pts": [(round(x, 1), round(y, 1)) for x, y in part],
                                "pile": 0, "rgb": [0.05, 0.05, 0.05], "err": 0.0,
                                "pr": round(lo + (hi - lo) * d, 2)})
        return out
    ink = [[tuple(r) for r in p.get("ink", [["vine black", 1]])]]
    lines = strokes_of(art["lines"])
    if p["kind"] == "maze":                          # wall by wall, swept down the page
        lines = painter_order(lines, 60)
    group = {"layer": 0, "strokes": lines, "recipes": ink, "kind": p.get("tool", "rigger"),
             "width": float(p.get("width", 1.6)), "thin": False, "lay": 1.0,
             "per_dip": int(p.get("per_dip", 12)), "pile_extra": ", medium=0.35", "pen": True}
    finish = []
    if art.get("solution") and p.get("solution", True):
        finish.append({"layer": 1, "strokes": strokes_of([art["solution"]]),
                       "recipes": [[tuple(r) for r in p.get("solution_ink", [["cadmium red", 1]])]],
                       "kind": p.get("tool", "rigger"), "width": float(p.get("width", 1.6)) * 3.0,
                       "thin": False, "lay": 1.0, "per_dip": 6, "pile_extra": ", medium=0.35",
                       "pen": True})
        for st in finish[-1]["strokes"]:
            st["pr"] = 0.85                          # the red path bold and even
    for g in [group] + finish:
        for st in g["strokes"]:
            st.update(layer=g["layer"], brush=g["kind"], width=g["width"])
    LOG.info("pen %s: %d strokes%s", p["kind"], len(lines),
             f", solution {len(finish[0]['strokes'])}" if finish else "")
    return [group], finish


def plan_compass(ref: np.ndarray, cfg: dict[str, Any], rng: np.random.Generator
                 ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """A compass spiral in pencil (lineart.spiral): each half-circle one compass arc,
    split into short strokes that carry the pressure along them and the arc's
    centre (`pivot`), where the playback sets the compass needle."""
    import lineart
    c = cfg["compass"]
    inward = c.get("inward", "auto")
    if inward == "auto":
        # Outside in when the subject fills the middle (it would show first drawn
        # centre-out); centre-out, the natural way, when the middle is light or plain.
        dark = lineart.darkness(ref, blur=4)
        h, w = dark.shape
        yy, xx = np.mgrid[0:h, 0:w]
        middle = np.hypot(xx - w / 2, yy - h / 2) < w * 0.18
        inward = bool(dark[middle].mean() > max(0.25, dark.mean() * 1.15))
        LOG.info("compass spiral: middle %.2f vs whole %.2f -> %s", dark[middle].mean(), dark.mean(),
                 "outside in" if inward else "centre out")
    art = lineart.spiral(ref, rng, float(c.get("spacing", 6.0)), inward=bool(inward))
    # Each half-circle one unbroken stroke, as a compass draws it: split into short
    # strokes, the pencil's touch-down dots lined up across the rings as spokes.
    # The pressure eases at both ends to soften the joins on the middle line.
    strokes = []
    for arc in art["arcs"]:
        pts, prs = arc["pts"], list(arc["pressure"])
        # an arc that would barely mark anywhere is left out: over blank paper they
        # only made a long, empty opening
        if len(pts) < 2 or max(prs) < float(c.get("faintest", 0.35)):
            continue
        ease = min(4, len(prs) // 4)
        for k in range(ease):
            f = 0.45 + 0.55 * k / max(1, ease)
            prs[k] *= f
            prs[-1 - k] *= f
        strokes.append({"pts": pts, "prs": [round(v, 2) for v in prs], "pivot": arc["pivot"],
                        "pile": 0, "rgb": [0.2, 0.2, 0.2], "err": 0.0})
    group = {"layer": 0, "tool": c.get("lead", "8B"), "pressure": 0.6, "width": 1.6, "strokes": strokes}
    for st in strokes:
        st.update(layer=0, brush="compass", width=1.6)
    LOG.info("compass spiral: %d arcs, %d strokes", len(art["arcs"]), len(strokes))
    return [group], []


def drawing_lua(g: dict[str, Any], first: int) -> list[str]:
    """Chunks for one drawing pass: a pencil, the black chalk, the eraser or fixative."""
    tool = g["tool"]
    if tool == "fix":
        return ["--@ chunk", "fix()"]
    head = []
    if g.get("bands"):                              # the band map, once, with the first lines
        text, bw, bh = g["bands"]
        head = ["--@ chunk", f'BANDS = "{text}"', f"BW, BH = {bw}, {bh}", BAND_FN.strip()]
    if g.get("shade"):
        below, spacing, length = g["shade"]
        make = "chalk()" if tool == "chalk" else f'pencil("{tool}")'
        out = ["--@ chunk", f"P = {make}", f"M = band({below:g})",
               "function T(n, x, y, size, pr, a, c)",
               "  local t0 = hand_secs()",
               "  local m = M * rect(x, y, size, size)",
               f"  P:hatch(m, {{angle=a, spacing={spacing:g}, length={length:g}, pressure=pr}})",
               f"  if c then P:hatch(m, {{angle=a + 0.85, spacing={spacing:g}, length={length:g}, pressure=pr}}) end",
               "  if P.sharpen then P:sharpen() end",
               '  print("S", n, t0, hand_secs())',
               "end"]
        pr = g.get("pressure", 0.6)
        for i in range(0, len(g["strokes"]), 40):
            out.append("--@ chunk")
            for j, st in enumerate(g["strokes"][i:i + 40]):
                x, y, size = st["tile"]
                out.append(f"T({first + i + j}, {x:g}, {y:g}, {size:g}, {pr:g}, {st['angle']:g}, "
                           f"{'true' if st['cross'] else 'false'})")
        return out
    if tool == "eraser":
        draw = f"erase(pts, {{strength=0.85, width={g['width']:g}}})"
    else:
        make = "chalk()" if tool == "chalk" else f'pencil("{tool}")'
        if g.get("ruled"):                           # straight, against the ruler
            draw = ("P:rule(pts[1], pts[#pts], {pressure=pr})\n  LINES = LINES + 1\n"
                    "  if LINES % 40 == 0 and P.sharpen then P:sharpen() end")
            out = head + ["--@ chunk", f"P = {make}", "LINES = 0",
                          "function S(n, pts, pr)", "  local t = hand_secs()", f"  {draw}",
                          '  print("S", n, t, hand_secs())', "end"]
            size = max(80, -(-len(g["strokes"]) // 25))
            for i in range(0, len(g["strokes"]), size):
                out.append("--@ chunk")
                for j, st in enumerate(g["strokes"][i:i + size]):
                    pts = ", ".join(f"{{{x:g}, {y:g}}}" for x, y in st["pts"])
                    out.append(f"S({first + i + j}, {{{pts}}}, {g.get('pressure', 0.6):g})")
            return out
        # LINES, not W: W is the canvas's width, and clobbering it sent every
        # band() lookup to the picture's right edge
        draw = "P:line(pts, {pressure=pr})\n  LINES = LINES + 1\n  if LINES % 40 == 0 and P.sharpen then P:sharpen() end"
    out = head + ["--@ chunk"]
    if tool != "eraser":
        out += [f"P = {make}", "LINES = 0"]
    out += ["function S(n, pts, pr)", "  local t = hand_secs()", f"  {draw}",
            '  print("S", n, t, hand_secs())', "end"]
    pr = g.get("pressure", 0.6)
    size = max(80, -(-len(g["strokes"]) // 25))
    for i in range(0, len(g["strokes"]), size):
        out.append("--@ chunk")
        for j, st in enumerate(g["strokes"][i:i + size]):
            pts = ", ".join(f"{{{x:g}, {y:g}}}" for x, y in st["pts"])
            mine = ("{" + ", ".join(f"{v:g}" for v in st["prs"]) + "}") if st.get("prs") else f"{pr:g}"
            out.append(f"S({first + i + j}, {{{pts}}}, {mine})")
    return out


def subject_mask(ref: np.ndarray) -> np.ndarray:
    """Where the subject is (bool, same size as ref): rembg's mask, or a centre oval."""
    h, w = ref.shape[:2]
    try:
        from rembg import remove
        m = remove(Image.fromarray((ref * 255).astype(np.uint8)), only_mask=True)
        mask = np.asarray(m.convert("L").filter(ImageFilter.MaxFilter(9)), dtype=np.float64) > 127
        if 0.02 < mask.mean() < 0.85:
            return mask
        LOG.info("subject mask covers %.0f%% of the picture; using a centre oval", mask.mean() * 100)
    except Exception as e:                          # noqa: BLE001 - the oval is a fair stand-in
        LOG.warning("no subject mask (%s); using a centre oval", e)
    yy, xx = np.mgrid[0:h, 0:w]
    return ((xx - w / 2) / (w * 0.32)) ** 2 + ((yy - h / 2) / (h * 0.28)) ** 2 < 1


def painter_order(strokes: list[dict[str, Any]], band: float) -> list[dict[str, Any]]:
    """One pile at a time, swept across the canvas in bands, as a hand works."""
    def key(s: dict[str, Any]) -> tuple[int, int, float]:
        x, y = s["pts"][0]
        row = int(y // band)
        return (s["pile"], row, x if row % 2 == 0 else -x)
    return sorted(strokes, key=key)


def assign_piles(strokes: list[dict[str, Any]], n: int, table: list[dict[str, Any]],
                 seed: int) -> list[list[tuple[str, float]]]:
    """Pool the layer's stroke colours into `n` piles; tag each stroke with its pile."""
    if not strokes:
        return []
    rgb = np.array([s["rgb"] for s in strokes])
    k = min(n, len(strokes))
    labels, centres = oilpaint.kmeans(rgb, k, seed)
    recipes = [oilpaint.match(c, table) for c in centres]
    for s, lab in zip(strokes, labels):
        s["pile"] = int(lab)
    return recipes


# --- Lua ------------------------------------------------------------------------

def layer_lua(strokes: list[dict[str, Any]], recipes: list[list[tuple[str, float]]],
              kind: str, width: float, layer: int, first: int, per_dip: int,
              thin: bool, lay: float = 1.0, pile_extra: str = "", pen: bool = False) -> list[str]:
    """Chunks for one layer; strokes are numbered from `first`.

    `pen`: a pointed nib at even pressure, no press-down or lift-off swell: on
    walls a few units long the swell had turned every maze line into a blob."""
    extra = (", turps=0.45" if thin else "") + pile_extra
    if pen:
        tool = f'brush{{kind="{kind}", width={width:g}, point=1}}'
    else:
        tool = (f'brush{{kind="{kind}", width={width:g}, lay={lay:g}}}' if lay != 1
                else f'brush("{kind}", {width:g})')
    ramps = "{0.02, 0.02}" if pen else "{0.12, 0.25}"
    out = ["--@ chunk", f"b = {tool}",
           "P = {" + ", ".join(oilpaint._recipe_lua(r, extra) for r in recipes) + "}",
           "function S(n, p, pts, pr)",
           "  if p ~= CUR or LEFT <= 0 then",
           "    local t = hand_secs()",
           "    if p ~= CUR then b:reload(P[p], 0.85) else b:load(P[p], 0.7) end",
           f"    CUR, LEFT = p, {per_dip}",
           '    print("D", t, hand_secs())',
           "  end",
           "  local t = hand_secs()",
           f"  b:stroke(pts, {{pressure=pr, ramps={ramps}}})",
           "  LEFT = LEFT - 1",
           '  print("S", n, t, hand_secs())',
           "end",
           "CUR, LEFT = nil, 0"]
    # the easel saves a frame at every chunk's end: keep a layer to ~25 chunks
    chunk_size = max(60, -(-len(strokes) // 25))
    for i in range(0, len(strokes), chunk_size):
        out.append("--@ chunk")
        for j, s in enumerate(strokes[i:i + chunk_size]):
            pts = ", ".join(f"{{{x:g}, {y:g}}}" for x, y in s["pts"])
            if "pr" in s:                           # the stroke's own pressure (pen line art)
                pr = f"{s['pr']:.2f}"
            else:
                pr = "{0.85, 0.35}" if layer == 0 else "{0.75, 0.25}"
            out.append(f"S({first + i + j}, {s['pile'] + 1}, {{{pts}}}, {pr})")
    return out


def _group_lua(g: dict[str, Any], first: int) -> list[str]:
    if g.get("wait"):                                # time passing, hands off: paint dries
        return ["--@ chunk", f"wait({int(g['wait'])})"]
    if g.get("tool"):
        return drawing_lua(g, first)
    if g.get("blend"):                              # a badger over the canvas, tile by tile
        out, t = [], 250
        H = g["height"]
        for k in range(int(g["blend"])):
            for y in np.arange(0, H, t):
                out.append("--@ chunk")
                for x in np.arange(0, 1000, t):
                    # overlapping and feathered: hard-edged tiles left a visible grid
                    out.append(f"blend(rect({x - 60:g}, {y - 60:g}, {t + 120}, {t + 120}):soften(50), "
                               f"{{angle={0.6 + k * 1.1:.2f}}})")
        return out
    return layer_lua(g["strokes"], g["recipes"], g["kind"], g["width"], g["layer"], first,
                     g["per_dip"], thin=g["thin"], lay=g["lay"], pile_extra=g.get("pile_extra", ""),
                     pen=bool(g.get("pen")))


def reorder(groups: list[dict[str, Any]], subject: np.ndarray | None) -> list[dict[str, Any]]:
    """The painting order: the block-in, then every layer's background, then the subject."""
    if subject is None or len(groups) < 2:
        return groups
    h, w = subject.shape

    def on_subject(s: dict[str, Any]) -> bool:
        xs = [min(w - 1, max(0, int(x))) for x, _ in s["pts"]]
        ys = [min(h - 1, max(0, int(y))) for _, y in s["pts"]]
        return bool(subject[ys, xs].mean() > 0.3)
    head, rest = groups[:1], groups[1:]
    back, front = [], []
    for g in rest:
        mine = [s for s in g["strokes"] if not on_subject(s)]
        theirs = [s for s in g["strokes"] if on_subject(s)]
        for part, into in ((mine, back), (theirs, front)):
            if part:
                into.append({**g, "strokes": part})
    for g in front:
        for s in g["strokes"]:
            s["subject"] = True
    LOG.info("subject last: %d background strokes, then %d on the subject",
             sum(len(g["strokes"]) for g in back), sum(len(g["strokes"]) for g in front))
    return head + back + front


def _parse_log(text: str) -> tuple[dict[int, tuple[float, float]], list[tuple[float, float]]]:
    strokes, dips = {}, []
    for line in text.splitlines():
        parts = line.split("\t")
        if parts[0] == "S" and len(parts) == 4:
            strokes[int(parts[1])] = (float(parts[2]), float(parts[3]))
        elif parts[0] == "D" and len(parts) == 3:
            dips.append((float(parts[1]), float(parts[2])))
    return strokes, dips


def _run(lua: Path, png: Path, width: int, frames: Path | None = None,
         every: float = 4.0, frame_width: int = 1080, expect: float = 0.0) -> str:
    """Run the easel. With frames and `expect` (s of hand time), report progress as it paints."""
    import subprocess
    import tempfile
    import time
    import progress
    cmd = [str(oilpaint.EASEL), "run", str(lua), "--out", str(png), "--width", str(width)]
    if frames:
        cmd += ["--frames-every", f"{every:g}", "--frames-dir", str(frames),
                "--frame-width", str(frame_width)]
    with tempfile.TemporaryFile("w+") as out:
        proc = subprocess.Popen(cmd, cwd=oilpaint.VENDOR, stdout=out, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        started, shown = time.time(), ""
        while proc.poll() is None:
            time.sleep(3)
            if time.time() - started > 6 * 3600:
                proc.kill()
                raise RuntimeError("easel ran over six hours")
            if frames and expect and (frames / "frames.tsv").exists():
                rows = (frames / "frames.tsv").read_text(encoding="utf-8").splitlines()
                last = rows[-1].split("\t") if len(rows) > 1 else []
                if len(last) > 1 and last[0] != shown:
                    shown = last[0]
                    hand = float(last[1])
                    progress.fraction(min(0.98, hand / expect),
                                      f"{hand / 60:.0f} of about {expect / 60:.0f} minutes "
                                      f"of brushwork")
                    progress.set_preview(frames / last[0])
        out.seek(0)
        text = out.read()
    if proc.returncode != 0 or "failed:" in text:
        raise RuntimeError(f"easel failed:\n{text[-2000:]}")
    return text


def paint(picture: Path | str, out_dir: Path | str, config: dict[str, Any] | None = None
          ) -> dict[str, Any]:
    """Paint `picture` stroke by stroke. Returns painting, frames, strokes.json and the Lua."""
    cfg = settings(config)
    if cfg.get("medium") == "carving":                 # carved in Blender, not painted
        import carving
        return carving.carve(picture, out_dir, {"frame_size": [1440, 2560], **cfg})   # a Short's, unless given
    if not oilpaint.installed():
        raise RuntimeError(f"the easel isn't built ({oilpaint.EASEL})")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    img = Image.open(picture).convert("RGB")
    aspect = img.width / img.height
    H = int(round(1000 / aspect))
    ref = np.asarray(img.resize((1000, H), Image.LANCZOS), dtype=np.float64) / 255.0
    ref = prepare(ref, cfg, int(cfg["seed"]))
    Image.fromarray((ref * 255).astype(np.uint8)).save(out / "reference.png")
    palette = cfg["palette"]
    table = oilpaint.swatches(palette)
    rng = np.random.default_rng(int(cfg["seed"]))
    ground = oilpaint._recipe_lua([tuple(r) for r in cfg["ground"]])[len("pile"):]
    drawing = cfg.get("medium") == "pencil"
    pen = cfg.get("medium") == "pen"
    compass = cfg.get("medium") == "compass"
    # a drawing wants paper: a fine linen under a thick, rolled ground, no weave to speak of
    paper = drawing or pen or compass
    apply = 'um=160, apply="roller"' if paper else 'um=80, apply="knife", texture=0.25'
    lua = [*oilpaint.head(palette), "--@ chunk",
           f"canvas{{size=400, aspect={img.width}/{img.height}, linen={60 if paper else 18}, "
           f"seed={int(cfg['seed'])}, ground={{{{pile={ground}, {apply}}}}}}}"]
    import progress
    plan: list[dict[str, Any]] = []
    canvas = None
    work = out / "layers"
    work.mkdir(exist_ok=True)
    head_lines = list(lua)
    groups: list[dict[str, Any]] = []              # one per layer, in painting order
    layers = len(cfg["layers"]) + (1 if cfg.get("contour") else 0)
    progress.stage("plan", f"{cfg.get('style') or 'plain'} style, {layers} layers", total=layers)
    for n, (kind, width, threshold, piles, longest, budget) in enumerate(
            [] if (drawing or pen or compass) else cfg["layers"]):
        strokes = plan_layer(ref, canvas, float(width), float(threshold), float(longest), rng,
                             int(budget))
        recipes = assign_piles(strokes, int(piles), table, int(cfg["seed"]) + n)
        strokes = painter_order(strokes, float(width) * 4)
        group = {"layer": n, "strokes": strokes, "recipes": recipes, "kind": kind,
                 "width": float(width), "thin": n == 0, "per_dip": int(cfg["strokes_per_dip"]),
                 "lay": 1.0 if n == 0 else float(cfg["lay"])}
        groups.append(group)
        lua += _group_lua(group, len(plan))
        for s in strokes:
            s.update(layer=n, brush=kind, width=float(width))
        plan += strokes
        LOG.info("layer %d (%s %g): %d strokes, %d piles", n, kind, width, len(strokes), len(recipes))
        progress.step(n + 1, detail=f"Layer {n + 1}: {len(strokes):,} strokes with a {kind} "
                                    f"brush, {len(recipes)} colours mixed")
        if n < len(cfg["layers"]) - 1:                    # see what the paint really did
            script = work / f"upto-{n}.lua"
            script.write_text("\n".join(lua) + "\n", encoding="utf-8")
            png = work / f"upto-{n}.png"
            _run(script, png, 1000)
            canvas = np.asarray(Image.open(png).convert("RGB").resize((1000, H), Image.LANCZOS),
                                dtype=np.float64) / 255.0
            progress.set_preview(png)
    finish: list[dict[str, Any]] = []
    n_fin = len(cfg["layers"]) + 1
    if drawing:
        groups, finish = plan_drawing(ref, cfg, rng)
        cfg = {**cfg, "blend": 0, "accents": None, "hatch": None, "contour": None}
    if compass:
        groups, finish = plan_compass(ref, cfg, rng)
        cfg = {**cfg, "blend": 0, "accents": None, "hatch": None, "contour": None}
    if pen:
        groups, finish = plan_pen(ref, cfg, rng)
        cfg = {**cfg, "blend": 0, "accents": None, "hatch": None, "contour": None}
    if cfg.get("blend"):
        finish.append({"blend": int(cfg["blend"]), "height": H, "strokes": []})
    if cfg.get("accents"):
        a = cfg["accents"]
        dots = plan_accents(ref, canvas, float(a["dark"]), float(a["light"]), float(a["width"]),
                            int(a.get("budget", 1600)), rng)
        white = cfg.get("white") or oilpaint.PALETTES[palette]["white"]
        finish.append({"layer": n_fin, "strokes": dots, "kind": "round", "width": float(a["width"]),
                       "recipes": [[("vine black", 1)] if palette != "classic" else [("bone black", 1)],
                                   [(white, 1)]],
                       "thin": False, "lay": 1.5, "per_dip": 6})
        LOG.info("accents: %d strokes", len(dots))
    if cfg.get("hatch"):
        hc = cfg["hatch"]
        lines = plan_hatching(ref, float(hc["below"]), float(hc["cross"]), float(hc["spacing"]),
                              float(hc["length"]), rng, budget=int(hc.get("budget", 0)))
        recipe = [tuple(r) for r in hc.get("recipe", [["vine black", 1]])]
        finish.append({"layer": n_fin, "strokes": lines, "kind": "round", "width": float(hc["width"]),
                       "recipes": [recipe], "thin": False, "lay": 1.0, "per_dip": 10, "ink": True})
        LOG.info("hatching: %d strokes", len(lines))
    for g in finish:
        for st in g["strokes"]:
            st.update(layer=g.get("layer", n_fin), brush=g.get("kind", "badger"),
                      width=float(g.get("width", 40)))
    if cfg.get("contour"):
        width, recipe, budget = cfg["contour"]
        lines = plan_contours(ref, float(width), int(budget), rng)
        n = len(cfg["layers"])
        groups.append({"layer": n, "strokes": lines, "recipes": [[tuple(r) for r in recipe]],
                       "kind": "round", "width": float(width), "thin": False, "lay": 1.0,
                       "per_dip": int(cfg["strokes_per_dip"]) * 2, "ink": True})
        for c in lines:
            c.update(layer=n, brush="round", width=float(width))
        LOG.info("contours (round %g): %d lines", width, len(lines))
    lines_group = groups.pop() if cfg.get("contour") else None
    groups = reorder(groups, subject_mask(ref) if cfg.get("subject_last") else None)
    groups += finish + ([lines_group] if lines_group else [])
    if cfg.get("ink_first"):
        # As manga is made: the black-and-white inking first (outlines, then hatching),
        # the ink left to dry, then the colour over it in thin washes that let it show.
        ink = sorted((g for g in groups if g.get("ink")), key=lambda g: g is not lines_group)
        colour = [g for g in groups if not g.get("ink")]
        for g in colour:
            if g.get("strokes"):
                # thin transparent washes (turpentine), so the ink beneath stays visible
                g.update(thin=False, lay=1.0, pile_extra=g.get("pile_extra", "") + ", turps=0.8")
        groups = ink + [{"wait": 4320, "strokes": []}] + colour
    plan, lua = [], list(head_lines)
    for g in groups:
        lua += _group_lua(g, len(plan))
        plan += g["strokes"]
    script = out / "painting.lua"
    script.write_text("\n".join(lua) + "\n", encoding="utf-8")
    frames = out / "frames"
    shutil.rmtree(frames, ignore_errors=True)
    png = out / "painting.png"
    want_frames = cfg.get("frames", True)            # stylefit's quick tests skip them
    progress.stage("paint", f"{len(plan):,} strokes, one at a time")
    # Never fill the disk: frames are ~6 MB each at 2160 px. A bizarre-manga test with
    # thousands of tiny ink strokes wrote 3,335 of them (22 GB) and filled the disk.
    import shutil as _sh
    free = _sh.disk_usage(out).free / 1e9
    if want_frames and free < float(cfg.get("min_free_gb", 8)):
        raise RuntimeError(f"only {free:.1f} GB of disk free; a painting needs at least "
                           f"{cfg.get('min_free_gb', 8)} GB for its frames")
    hand_secs = len(plan) * 1.7
    every = max(float(cfg["frames_every"]), hand_secs / float(cfg.get("max_frames", 600)))
    # ~1.7 s of hand time a stroke, palette trips included (measured on the tests)
    log = _run(script, png, int(cfg["width"]), frames if want_frames else None,
               every, int(cfg["frame_width"]), expect=hand_secs)
    progress.set_preview(png)
    times, dips = _parse_log(log)
    for i, s in enumerate(plan):
        s["t0"], s["t1"] = times.get(i, (None, None))
    record = {"aspect": aspect, "height_units": 1000 / aspect, "strokes": plan,
              "dips": dips, "layers": cfg["layers"], "style": cfg.get("style") or "",
              "grade": cfg.get("grade"), "medium": cfg.get("medium", "oil"),
              "playback": cfg.get("playback") or {}}
    (out / "strokes.json").write_text(json.dumps(record), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    return {"painting": png, "frames": frames, "strokes": out / "strokes.json", "lua": script}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Paint a picture stroke by stroke.")
    ap.add_argument("picture")
    ap.add_argument("out_dir")
    ap.add_argument("--style", default="", choices=["", *STYLES])
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    print(paint(a.picture, a.out_dir, {"brushwork": {"style": a.style}}))


if __name__ == "__main__":
    main()
