"""Bizarre manga: a page made the way the manga artist makes it, stage by stage.

Following how manga artists work, stage by stage:

  1. blue rough      loose light-blue pencil: heads as ovals, gestures as long lines
  2. ink             brush-pen lines over the blue, thick and thin with tapered ends,
                     the face first (eyes, nose, lips, jaw), then hair and costume
  3. hatching        short groups of parallel strokes along the planes of the face
                     and in the folds
  4. blacks          solid black shapes filled in
  5. the blue goes   the rough fades out, as the blue disappears from a scanned page
  6. colour          flat marker colour laid region by region under the ink, pushed
                     to bold, unnatural colour (purple shadows, gold lights)

Original characters only. Every mark is drawn into the frame as it is made, so the
video shows the page come together from blank paper. Built on OpenCV, not the oil
simulator: manga is crisp ink and flat colour, which oil paint can't give.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

import cv2
import numpy as np

LOG = logging.getLogger("mken.manga")

DEFAULTS: dict[str, Any] = {
    "size": [1080, 1920], "fps": 30,
    # seconds per stage (blue, ink, hatching, blacks, blue fading, colour, hold)
    "timing": [3.0, 11.0, 4.0, 2.5, 1.2, 6.0, 3.0],
    "colours": 9,                                  # flat colour regions
    "tint": [[92, 40, 132], [250, 205, 80], 0.32],  # shadows to purple, lights to gold (RGB, amount)
    "saturate": 1.45,
    "hatch_budget": 1200,
    "hand_overlay": False,          # a hand holding the pencil, then the pen, on each stroke
}
PROMPT = ("bold manga colour illustration, an original fictional character, sharp angular face, "
          "strong jaw, defined lips, intense stare, ornate headwear and jewellery, dramatic pose "
          "looking over the shoulder, thick black ink outlines, hatching in the shadows, flat vivid "
          "colours, graphic patterned background, no logos, no text")
BLUE = np.array([110, 160, 230], np.float32)       # RGB of the rough pencil


# --- reading the picture ------------------------------------------------------------

def subject_mask(rgb: np.ndarray) -> np.ndarray | None:
    try:
        from PIL import Image
        from rembg import remove
        return np.asarray(remove(Image.fromarray(rgb), only_mask=True).convert("L")) > 127
    except Exception:                                # noqa: BLE001
        return None


def face_box(rgb: np.ndarray, mask: np.ndarray | None) -> tuple[int, int, int, int] | None:
    """The face: the largest patch of skin in the upper part of the figure. (The top of
    the silhouette was a tall hat; OpenCV 5 dropped its face finder.)"""
    h, w = rgb.shape[:2]
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    skin = ((hsv[..., 0] <= 25) | (hsv[..., 0] >= 170)) & (hsv[..., 1] > 40) & (hsv[..., 2] > 70)
    if mask is not None:
        skin &= mask
    skin[int(h * 0.7):] = False
    skin = cv2.morphologyEx(skin.astype(np.uint8), cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
    n, comp, stats, _ = cv2.connectedComponentsWithStats(skin)
    if n < 2:
        return None
    k = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x, y, bw, bh = (int(v) for v in stats[k, :4])
    return (x, y, bw, min(bh, int(bw * 1.4)))


def ink_lines(grey: np.ndarray, face: tuple | None) -> list[dict[str, Any]]:
    """The drawing's lines as strokes: contours of the dark line work, each with a
    thickness from how bold the line is, ordered face first (eyes, nose, lips, jaw),
    then outward from the face, as an inker works."""
    h, w = grey.shape
    blur = cv2.GaussianBlur(grey, (0, 0), 1.2)
    lines = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 15, 9)
    lines = cv2.morphologyEx(lines, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    skel = cv2.ximgproc.thinning(lines) if hasattr(cv2, "ximgproc") else cv2.Canny(blur, 70, 160)
    contours, _ = cv2.findContours(skel, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    dist = cv2.distanceTransform(lines, cv2.DIST_L2, 3)        # half-thickness of the line here
    fx, fy, fw, fh = face if face else (w // 3, h // 5, w // 3, h // 4)
    cx, cy = fx + fw / 2, fy + fh / 2
    out = []
    for c in contours:
        if len(c) < 10:
            continue
        pts = c[:, 0, :].astype(np.float32)
        pts = pts[:: max(1, len(pts) // 60)]                    # at most ~60 points a line
        width = float(np.clip(np.median(dist[pts[:, 1].astype(int), pts[:, 0].astype(int)]) * 2.2, 1.6, 9))
        mx, my = pts.mean(0)
        in_face = fx <= mx <= fx + fw and fy <= my <= fy + fh
        # face first: eyes (upper half of the face) before nose and lips, then the rest by distance
        order = (0, (my - fy) / max(1, fh)) if in_face else (1, np.hypot(mx - cx, my - cy))
        out.append({"pts": pts, "width": width, "order": order})
    out.sort(key=lambda s: s["order"])
    return out


def rough_lines(grey: np.ndarray, rng: np.random.Generator) -> list[np.ndarray]:
    """Loose blue pencil: the big shapes only (edges of a heavily blurred picture),
    a little wobbly, the way a rough is blocked in."""
    soft = cv2.GaussianBlur(grey, (0, 0), 6)
    edges = cv2.Canny(soft, 10, 30)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    out = []
    for c in sorted(contours, key=len, reverse=True)[:320]:
        if len(c) < 20:
            continue
        pts = c[:, 0, :].astype(np.float32)[::6]
        out.append(pts + rng.normal(0, 2.0, pts.shape).astype(np.float32))
    return out


def hatch_groups(grey: np.ndarray, rng: np.random.Generator, budget: int,
                 mask: np.ndarray | None = None) -> list[np.ndarray]:
    """Groups of 3-6 short parallel strokes in the mid-dark tones, along the planes."""
    h, w = grey.shape
    g = cv2.GaussianBlur(grey, (0, 0), 3).astype(np.float32)
    gx, gy = cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1)
    zone = (g > 45) & (g < 120)
    if mask is not None:                             # on the figure only, never the background
        zone &= cv2.erode(mask.astype(np.uint8), np.ones((15, 15), np.uint8)).astype(bool)
    ys, xs = np.nonzero(zone)
    if not len(xs):
        return []
    pick = rng.choice(len(xs), min(len(xs), budget // 4), replace=False)
    out = []
    for k in pick:
        x, y = float(xs[k]), float(ys[k])
        a = np.arctan2(gy[int(y), int(x)], gx[int(y), int(x)]) + np.pi / 2 + rng.normal(0, 0.15)
        d, n = np.array([np.cos(a), np.sin(a)]), np.array([-np.sin(a), np.cos(a)])
        ln = rng.uniform(9, 15)
        for j in range(int(rng.integers(4, 7))):    # a tight group of short parallel strokes
            c = np.array([x, y]) + n * j * 3.2
            out.append(np.array([c - d * ln / 2, c + d * ln / 2], np.float32))
    return out[:budget]


def flat_colour(rgb: np.ndarray, cfg: dict[str, Any], seed: int) -> tuple[np.ndarray, list[np.ndarray]]:
    """The picture in a few flat, bold colours, and its regions (largest first)."""
    h, w = rgb.shape[:2]
    small = cv2.resize(rgb, (w // 3, h // 3), interpolation=cv2.INTER_AREA).reshape(-1, 3).astype(np.float32)
    _, labels, centres = cv2.kmeans(small, int(cfg["colours"]), None,
                                     (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0), 3,
                                     cv2.KMEANS_PP_CENTERS)
    labels = cv2.resize(labels.reshape(h // 3, w // 3).astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST)
    labels = cv2.medianBlur(labels, 7)
    # bold colour: saturate, then shadows toward purple and lights toward gold
    hsv = cv2.cvtColor(centres.reshape(1, -1, 3).astype(np.uint8), cv2.COLOR_RGB2HSV).astype(np.float32)
    hsv[..., 1] = np.clip(hsv[..., 1] * float(cfg["saturate"]), 0, 255)
    cols = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB).reshape(-1, 3).astype(np.float32)
    shade, light, amt = cfg["tint"]
    lum = cols.mean(1, keepdims=True) / 255
    cols = cols * (1 - amt) + amt * np.where(lum < 0.5, np.array(shade), np.array(light))
    flat = cols[labels]
    regions = []
    for k in range(len(cols)):
        n, comp = cv2.connectedComponents((labels == k).astype(np.uint8))
        for c in range(1, n):
            m = comp == c
            if m.sum() > 400:
                regions.append(m)
    regions.sort(key=lambda m: -m.sum())
    return flat.astype(np.float32), regions


# --- drawing into the frame ---------------------------------------------------------

def draw_ink(layer: np.ndarray, pts: np.ndarray, width: float, frac: float = 1.0) -> None:
    """A brush-pen line: thickest in the middle, tapering to points at both ends."""
    n = max(2, int(len(pts) * frac))
    p = pts[:n]
    for i in range(len(p) - 1):
        t = i / max(1, len(pts) - 1)
        wdt = max(1, int(round(width * (0.35 + 0.65 * np.sin(np.pi * t)))))
        cv2.line(layer, tuple(int(v) for v in p[i]), tuple(int(v) for v in p[i + 1]), 255, wdt, cv2.LINE_AA)


class Hand:
    """A hand (assets/hand) whose tool tip follows the stroke being drawn."""

    def __init__(self, W: int):
        import json
        import handplay
        self.sprites = {}
        for tool in ("pencil", "pen"):
            meta = handplay.HAND.with_name(f"{tool}.json")
            if not meta.exists():
                continue
            m = json.loads(meta.read_text())
            img = cv2.imread(str(meta.parent / m["file"]), cv2.IMREAD_UNCHANGED)
            k = W * 0.42 / img.shape[1]
            img = cv2.resize(img, (int(img.shape[1] * k), int(img.shape[0] * k)), interpolation=cv2.INTER_AREA)
            rgba = cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA).astype(np.float32)
            self.sprites[tool] = (rgba, (m["tip"][0] * k, m["tip"][1] * k))
        self.pos = None

    def draw(self, frame: np.ndarray, tool: str, target: np.ndarray | None, dt: float) -> np.ndarray:
        if tool not in self.sprites:
            return frame
        if target is None:                       # between stages: the hand drifts away
            if self.pos is None:
                return frame
            target = self.pos + np.array([60.0, -90.0])
        k = 1 - np.exp(-dt / 0.08)
        self.pos = np.array(target, np.float32) if self.pos is None else self.pos + (np.asarray(target) - self.pos) * k
        rgba, tip = self.sprites[tool]
        H, W = frame.shape[:2]
        x, y = int(self.pos[0] - tip[0]), int(self.pos[1] - tip[1])
        for off, alpha, src in ((18, 0.28, None), (0, 1.0, rgba)):        # shadow, then hand
            hh, ww = rgba.shape[:2]
            x0, y0 = max(0, x + off), max(0, y + off)
            x1, y1 = min(W, x + off + ww), min(H, y + off + hh)
            if x0 >= x1 or y0 >= y1:
                continue
            sub = rgba[y0 - y - off:y1 - y - off, x0 - x - off:x1 - x - off]
            a = sub[..., 3:4] / 255 * alpha
            col = sub[..., :3] if src is not None else np.full_like(sub[..., :3], 30)
            frame[y0:y1, x0:x1] = frame[y0:y1, x0:x1] * (1 - a) + col * a
        return frame


def render(picture: Path, out: Path, config: dict[str, Any] | None = None, music: Path | None = None,
           seed: int = 0) -> Path:
    cfg = {**DEFAULTS, **((config or {}).get("manga") or {})}
    W, H = cfg["size"]
    rng = np.random.default_rng(seed)
    img = cv2.imread(str(picture))
    h, w = img.shape[:2]
    s = max(W / w, H / h)
    img = cv2.resize(img, (int(w * s + 0.5), int(h * s + 0.5)), interpolation=cv2.INTER_AREA)
    y0, x0 = (img.shape[0] - H) // 2, (img.shape[1] - W) // 2
    rgb = cv2.cvtColor(img[y0:y0 + H, x0:x0 + W], cv2.COLOR_BGR2RGB)
    grey = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    mask = subject_mask(rgb)
    face = face_box(rgb, mask)
    rough = rough_lines(grey, rng)
    ink = ink_lines(grey, face)
    hatch = hatch_groups(grey, rng, int(cfg["hatch_budget"]), mask)
    blacks = cv2.morphologyEx((cv2.GaussianBlur(grey, (0, 0), 2) < 38).astype(np.uint8),
                              cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n_b, comp_b = cv2.connectedComponents(blacks)
    black_parts = sorted([comp_b == c for c in range(1, n_b) if (comp_b == c).sum() > 300],
                         key=lambda m: -m.sum())
    flat, regions = flat_colour(rgb, cfg, seed)
    LOG.info("manga: face %s, %d rough, %d ink, %d hatch, %d blacks, %d colour regions",
             face, len(rough), len(ink), len(hatch), len(black_parts), len(regions))

    fps = int(cfg["fps"])
    t_blue, t_ink, t_hatch, t_black, t_fade, t_col, t_hold = (float(v) for v in cfg["timing"])
    # each stage plays its own marks across its own seconds
    plan = [("blue", rough, t_blue), ("ink", ink, t_ink), ("hatch", hatch, t_hatch),
            ("black", black_parts, t_black), ("fade", [], t_fade), ("colour", regions, t_col),
            ("hold", [], t_hold)]
    total = sum(p[2] for p in plan)
    paper = np.full((H, W, 3), 246, np.float32)
    blue_l = np.zeros((H, W), np.uint8)
    ink_l = np.zeros((H, W), np.uint8)
    col_mask = np.zeros((H, W), np.float32)

    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(fps), "-i", "-"]
    if music:
        import handplay
        cmd += ["-ss", f"{handplay.music_start(music):.2f}", "-i", str(music), "-filter_complex",
                f"[1:a]atrim=0:{total:.2f},asetpts=PTS-STARTPTS,afade=t=in:d=1.0,"
                f"afade=t=out:st={max(0, total - 2.5):.2f}:d=2.5,loudnorm=I=-16:TP=-1.5:LRA=11[a]",
                "-map", "0:v", "-map", "[a]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-t", f"{total:.3f}", "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    blue_alpha = 1.0
    hand = Hand(W) if cfg.get("hand_overlay") else None
    tip = None
    try:
        for name, items, secs in plan:
            frames = max(1, int(secs * fps))
            done = 0
            for k in range(frames):
                upto = int(round(len(items) * (k + 1) / frames)) if items else 0
                for it in items[done:upto]:
                    if name == "black":                   # a filled area: the brush at its middle
                        ys, xs = np.nonzero(it)
                        tip = np.array([xs.mean(), ys.mean()]) if len(xs) else tip
                    elif name in ("blue", "ink", "hatch"):
                        tip = (it["pts"] if isinstance(it, dict) else it)[-1]   # where the tool is now
                    if name == "blue":
                        cv2.polylines(blue_l, [it.astype(np.int32)], False, 255, 3, cv2.LINE_AA)
                    elif name == "ink":
                        draw_ink(ink_l, it["pts"], it["width"])
                    elif name == "hatch":
                        draw_ink(ink_l, it, 1.6)
                    elif name == "black":
                        ink_l[it] = 255
                    elif name == "colour":
                        col_mask[it] = 1.0
                done = upto
                if name == "fade":
                    blue_alpha = 1.0 - (k + 1) / frames
                # paper, then colour (soft-edged, like marker), then the blue rough, then the ink on top
                cm = cv2.GaussianBlur(col_mask, (0, 0), 1.5)[..., None]
                frame = paper * (1 - cm) + flat * cm
                b = (blue_l.astype(np.float32) / 255 * 0.8 * blue_alpha)[..., None]
                frame = frame * (1 - b) + BLUE * b
                i = (ink_l.astype(np.float32) / 255)[..., None]
                frame = frame * (1 - i) + 14 * i
                if hand is not None:
                    tool = "pencil" if name == "blue" else "pen"
                    live = name in ("blue", "ink", "hatch", "black")
                    frame = hand.draw(frame, tool, tip if live else None, 1.0 / fps)
                proc.stdin.write(np.clip(frame, 0, 255).astype(np.uint8).tobytes())
    finally:
        proc.stdin.close()
        proc.wait()
    if proc.returncode:
        raise RuntimeError("ffmpeg failed")
    return out
