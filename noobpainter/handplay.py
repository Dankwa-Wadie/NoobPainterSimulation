"""Handplay: the painting playback with a hand making every stroke.

brushwork.py records each stroke's path and its moment on the painter's
clock (hand time), and the easel saves the canvas every few seconds of it.
For each video frame this module

  1. turns video time into hand time: a few chosen strokes (`hero`) play near
     real time, the rest is fast-forwarded, the finished painting holds;
  2. takes the last saved canvas and reveals each stroke in progress along
     its path up to the brush tip, from the next saved canvas (so paint
     appears under the brush, not a frame at a time);
  3. sets the hand sprite with its brush tip on that point, its motion
     smoothed so a fast-forwarded hand swoops rather than jitters, lifted
     (bigger, its shadow further off) between strokes.

Frames are piped to ffmpeg; nothing is written per frame.
"""
from __future__ import annotations

import bisect
import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

LOG = logging.getLogger("mken.handplay")

ROOT = Path(__file__).resolve().parent.parent          # the repo root (assets/, vendor/)
HAND = ROOT / "assets" / "hand" / "hand.json"

DEFAULTS: dict[str, Any] = {
    "fps": 25,
    "hero_strokes": 10,        # strokes played near real time, from the start and the end
    "hero_secs": 0.7,          # video seconds per hero stroke
    "hold": 2.5,               # the finished painting, hand gone
    "smooth": 0.12,            # s: how far the hand lags its target in the fast parts
    "hand_width": 0.55,        # sprite width as a share of the frame's short side (its width, in portrait)
    "shadow": (18, 26, 0.32),  # offset px, blur px, opacity
    # The guess-the-painting opening: the finished painting blurred past
    # recognition, easing only a little, under a question.
    "teaser": 3.0,             # s; 0 for none
    # A fair hint: the subject's shape and silhouette read, its details don't.
    # (0.11 left only colour, which made the guess impossible rather than fun.)
    "teaser_blur": 0.035,      # blur radius as a share of the frame width, at the start
    "teaser_ease": 0.8,        # the blur it eases to, as a share of the start
    # The camera: a close-up that glides after the brush, so only brushwork is
    # seen until it pulls back from the finished painting.
    "zoom": 2.0,
    # Closer still while the subject itself is painted: a big subject (a balloon
    # filling the canvas) otherwise shows its shape even at 2x.
    "subject_zoom": 2.8,
    "camera_smooth": 0.7,      # s: the camera lags the hand by about this much
    "pullback": 1.6,           # s from the close-up to the whole painting
    "upside_down": False,      # paint it upside down, then turn it the right way up
    "turn": 1.3,               # s for that half turn
    # After the reveal the painting can come alive for a few seconds: its depth
    # (Depth Anything, as for the carvings) drifts the near against the far in a
    # slow sway. 0 is off.
    # The hand holding the brush, pencil or tool (assets/hand), and the left hand on the
    # ruler: off unless asked for (`--hand`); the paint, compass and ruler show either way.
    "hand_overlay": False,
    "alive": 0.0,
    "alive_sway": 0.010,       # the nearest point's drift, as a share of the frame width
    "teaser_text": "Guess what I'm painting",
    "ending_text": "Did you guess it?",
}
FONT = ROOT / "assets" / "fonts" / "Montserrat-Italic-VF.ttf"
MUSIC = ROOT / "assets" / "music" / "classical"


@dataclass
class Shot:
    t: float
    frame: Path


def load_frames(frames: Path) -> list[Shot]:
    rows = (frames / "frames.tsv").read_text(encoding="utf-8").splitlines()[1:]
    shots = []
    for row in rows:
        name, secs, *_ = row.split("\t")
        shots.append(Shot(float(secs), frames / name))
    return shots


# --- timing ---------------------------------------------------------------------

def schedule(strokes: list[dict[str, Any]], seconds: float, cfg: dict[str, Any]
             ) -> tuple[np.ndarray, np.ndarray]:
    """Breakpoints (video s, hand s) for a piecewise-linear clock.

    Hero strokes at the start and the end get `hero_secs` each; the rest of
    the painting shares what is left, evenly by hand time.
    """
    timed = [s for s in strokes if s.get("t0") is not None]
    if not timed:
        raise ValueError("no timed strokes")
    n = min(int(cfg["hero_strokes"]), len(timed) // 4)
    head, tail = timed[:n // 2], timed[len(timed) - (n - n // 2):]
    end = timed[-1]["t1"]
    hero = float(cfg["hero_secs"])
    fast = max(1.0, seconds - float(cfg["hold"]) - hero * (len(head) + len(tail)))
    video, hand = [0.0], [0.0]

    def to(v: float, h: float) -> None:
        video.append(video[-1] + v)
        hand.append(h)
    for s in head:                                    # first marks, at the pace of a hand
        if s["t0"] > hand[-1]:
            to(0.25, s["t0"])                         # the walk to the canvas
        to(hero, s["t1"])
    middle_from = hand[-1]
    middle_to = tail[0]["t0"] if tail else end
    to(fast, middle_to)
    for s in tail:                                    # the last touches
        to(hero, s["t1"])
    to(float(cfg["hold"]), end + 1e-3)
    LOG.info("clock: %.0f s of hand time in %.1f s (fast part x%.0f)",
             end, video[-1], (middle_to - middle_from) / fast)
    return np.array(video), np.array(hand)


# --- drawing --------------------------------------------------------------------

def _along(pts: np.ndarray, f: float) -> tuple[np.ndarray, list[tuple[float, float]]]:
    """The point a share `f` along a polyline, and the path up to it."""
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    total = seg.sum()
    if total <= 0:
        return pts[0], [tuple(pts[0])]
    goal, run = f * total, 0.0
    path = [tuple(pts[0])]
    for i, d in enumerate(seg):
        if run + d >= goal:
            p = pts[i] + (pts[i + 1] - pts[i]) * ((goal - run) / max(d, 1e-9))
            path.append(tuple(p))
            return p, path
        run += d
        path.append(tuple(pts[i + 1]))
    return pts[-1], path


class Player:
    def __init__(self, frames: Path, record: dict[str, Any], size: tuple[int, int],
                 hand: dict[str, Any] | None, cfg: dict[str, Any]):
        self.cfg, self.size = cfg, size
        self.shots = load_frames(frames)
        self.times = [s.t for s in self.shots]
        self.strokes = [s for s in record["strokes"] if s.get("t0") is not None]
        self.starts = [s["t0"] for s in self.strokes]
        self.scale = size[0] / 1000.0                  # canvas units -> px
        self.cache: dict[Path, Image.Image] = {}
        self.hand = None
        if hand:
            img = Image.open(hand["file"]).convert("RGBA")
            # sized for the close-up: hand_width of the video's short side once zoomed
            # (its width in portrait; by the width, a landscape video's hand was huge)
            k = min(size) * float(cfg["hand_width"]) / float(cfg.get("zoom") or 1) / img.width
            self.hand = img.resize((int(img.width * k), int(img.height * k)), Image.LANCZOS)
            self.tip = (hand["tip"][0] * k, hand["tip"][1] * k)
            off, blur, alpha = cfg["shadow"]
            a = self.hand.getchannel("A").filter(ImageFilter.GaussianBlur(blur))
            self.shadow = Image.new("RGBA", self.hand.size, (20, 14, 10, 0))
            self.shadow.putalpha(a.point(lambda v: int(v * alpha)))
            self.shadow_off = off
            # The bristles: the paint-coloured pixels near the tip, recoloured
            # to the paint on the brush.
            arr = np.asarray(self.hand, dtype=np.float64)
            yy, xx = np.mgrid[0:arr.shape[0], 0:arr.shape[1]]
            near = np.hypot(xx - self.tip[0], yy - self.tip[1]) < self.hand.width * 0.11
            r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
            painted = (b > r + 25) & (b > g + 10) & (arr[..., 3] > 40)
            self.bristle = (near & painted).astype(np.float64)[..., None]
            self.bristle_lum = (arr[..., :3].mean(-1, keepdims=True) / 255.0)
            self.tinted: dict[tuple[int, int, int], Image.Image] = {}
        self.colour: tuple[int, int, int] | None = None
        self.on_subject = False
        self.pivot = None
        self.ruler = None
        self.ruling = False
        self.ruler_at = None
        self.flat = None
        if cfg.get("ruler") and cfg.get("hand_overlay") and (HAND.parent / "flat.png").exists():
            flat = Image.open(HAND.parent / "flat.png").convert("RGBA")
            # the flat hand, in proportion to the drawing hand (hand_width)
            k = min(size) * 0.42 * float(cfg["hand_width"]) / 0.55 / float(cfg.get("zoom") or 1) / flat.width
            self.flat = flat.resize((int(flat.width * k), int(flat.height * k)), Image.LANCZOS)
        # a compass's leg length: just long enough for its widest arc
        spans = [np.hypot(x - s["pivot"][0], y - s["pivot"][1])
                 for s in self.strokes if s.get("pivot") for x, y in s["pts"][::8]]
        self.leg = (max(spans) * self.scale * 0.56) if spans else None
        self.pos: np.ndarray | None = None

    def _hand_in(self, colour: tuple[int, int, int] | None) -> Image.Image:
        """The hand with its bristles in `colour`."""
        if colour is None or not self.bristle.any():
            return self.hand
        key = tuple(int(c) // 16 * 16 for c in colour)
        if key not in self.tinted:
            if len(self.tinted) > 64:
                self.tinted.clear()
            arr = np.asarray(self.hand, dtype=np.float64).copy()
            paint = np.array(key, dtype=np.float64)[None, None] * (0.55 + 0.9 * self.bristle_lum)
            arr[..., :3] = arr[..., :3] * (1 - self.bristle) + np.clip(paint, 0, 255) * self.bristle
            self.tinted[key] = Image.fromarray(arr.astype(np.uint8), "RGBA")
        return self.tinted[key]

    def _img(self, p: Path) -> Image.Image:
        if p not in self.cache:
            if len(self.cache) > 6:
                self.cache.pop(next(iter(self.cache)))
            self.cache[p] = Image.open(p).convert("RGB").resize(self.size, Image.LANCZOS)
        return self.cache[p]

    def canvas(self, t: float) -> tuple[Image.Image, np.ndarray | None, bool]:
        """The canvas at hand time t, the brush tip (px) and whether it is on the canvas."""
        i = max(0, bisect.bisect_right(self.times, t) - 1)
        base = self._img(self.shots[i].frame)
        if i + 1 >= len(self.shots):
            return base, None, False
        t_a, nxt = self.shots[i].t, self._img(self.shots[i + 1].frame)
        mask = Image.new("L", self.size, 0)
        draw = ImageDraw.Draw(mask)
        tip, down = None, False
        j = bisect.bisect_left(self.starts, t_a - 60)
        while j < len(self.strokes) and self.strokes[j]["t0"] <= t:
            s = self.strokes[j]
            j += 1
            if s["t1"] <= t_a:
                continue
            f = min(1.0, (t - s["t0"]) / max(1e-6, s["t1"] - s["t0"]))
            pts = np.array(s["pts"], dtype=np.float64) * self.scale
            p, path = _along(pts, f)
            w = max(2, int(s["width"] * self.scale * 1.4))
            if len(path) > 1:
                draw.line(path, fill=255, width=w, joint="curve")
            r = w // 2
            draw.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=255)
            tip, down = p, f < 1.0
            self.pivot = s.get("pivot")
            # short lines (< 25 units) are drawn freehand: the ruler stays where it was
            if s.get("ruler") is not None and np.hypot(*np.subtract(s["pts"][-1], s["pts"][0])) >= 25:
                self.ruler = (np.array(s["pts"][0]) * self.scale, s["ruler"])
                self.ruling = f < 1.0                  # held still while the pencil is on it
            self.on_subject = bool(s.get("subject"))
            self.colour = tuple(int(c * 255) for c in s.get("rgb", (0, 0, 0)))
        if tip is None:                                # between strokes: toward the next one
            if j < len(self.strokes):
                tip = np.array(self.strokes[j]["pts"][0], dtype=np.float64) * self.scale
                self.pivot = self.strokes[j].get("pivot")
                upcoming = self.strokes[j]           # (not `nxt`: that is the next canvas image)
                if upcoming.get("ruler") is not None and np.hypot(*np.subtract(upcoming["pts"][-1], upcoming["pts"][0])) >= 25:
                    self.ruler = (np.array(upcoming["pts"][0]) * self.scale, upcoming["ruler"])   # set it for the next line
                self.ruling = False
        out = Image.composite(nxt, base, mask.filter(ImageFilter.GaussianBlur(1.2)))
        return out, tip, down

    def frame(self, t: float, dt: float) -> Image.Image:
        img, tip, down = self.canvas(t)
        if self.cfg.get("upside_down"):              # the canvas turned on the easel
            img = img.rotate(180)
            if tip is not None:
                tip = np.array([self.size[0] - tip[0], self.size[1] - tip[1]])
        if self.cfg.get("ruler") and self.ruler is not None:
            # The ruler stays down and glides to each new line (it flickered when it
            # only showed during a line); the flat left hand rides on it, holding it.
            (rx, ry), rang = self.ruler
            # still while a line is ruled; between lines it is picked up and set down
            # for the next one, quickly, as a hand does
            k = 1.0 if self.ruling else 1.0 - np.exp(-dt / 0.18)
            if self.ruler_at is None:
                self.ruler_at = np.array([rx, ry, rang])
            else:
                da = (rang - self.ruler_at[2] + np.pi / 2) % np.pi - np.pi / 2   # a ruler has no front
                self.ruler_at = self.ruler_at + np.array([rx - self.ruler_at[0], ry - self.ruler_at[1], da]) * k
            x, y, a = self.ruler_at
            img = draw_ruler(img, ((x, y), a), min(self.size), holder=self.flat, pencil=tip)
        if self.cfg.get("compass") and tip is not None and self.pivot is not None:
            # the lead is exactly on the line (no smoothing: a compass can't lag)
            self.pos = tip
            pivot = np.array(self.pivot, dtype=np.float64) * self.scale
            return draw_compass(img, pivot, tip, min(self.size), lifted=not down, leg=self.leg)
        if self.hand is None or tip is None:
            self.pos = None
            return img
        k = 1.0 - np.exp(-dt / max(1e-3, float(self.cfg["smooth"])))
        self.pos = tip if self.pos is None else self.pos + (tip - self.pos) * k
        lift = 0.0 if down else 1.0
        x, y = self.pos[0] - self.tip[0], self.pos[1] - self.tip[1]
        img = img.copy()
        so = int(self.shadow_off * (1 + 1.5 * lift))
        img.paste(self.shadow, (int(x + so), int(y + so)), self.shadow)
        hand = self._hand_in(self.colour)
        img.paste(hand, (int(x), int(y - 6 * lift)), hand)
        return img


def caption(img: Image.Image, text: str, y: float = 0.12, alpha: float = 1.0) -> Image.Image:
    """Big white words on a soft dark band, near the top."""
    from PIL import ImageFont
    if not text or alpha <= 0:
        return img
    W, H = img.size
    size = int(min(W, H) * 0.072)                   # by the short side: landscape too

    def bold(px: int) -> Any:
        font = ImageFont.truetype(str(FONT), px)
        try:
            font.set_variation_by_name("ExtraBold Italic")
        except Exception:                           # noqa: BLE001 - static fonts have none
            pass
        return font
    font = bold(size)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    box = d.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    while tw > W * 0.9 and size > 20:
        size = int(size * 0.92)
        font = bold(size)
        box = d.textbbox((0, 0), text, font=font)
        tw, th = box[2] - box[0], box[3] - box[1]
    cx, cy = (W - tw) / 2, H * y
    pad = size * 0.5
    d.rounded_rectangle([cx - pad, cy - pad * 0.6, cx + tw + pad, cy + th + pad * 1.1],
                        radius=int(pad), fill=(10, 8, 6, int(150 * alpha)))
    d.text((cx - box[0], cy - box[1]), text, font=font, fill=(255, 255, 255, int(255 * alpha)),
           stroke_width=max(1, size // 18), stroke_fill=(0, 0, 0, int(200 * alpha)))
    out = img.convert("RGBA")
    out.alpha_composite(layer)
    return out.convert("RGB")


def teaser_frame(final: Image.Image, f: float, cfg: dict[str, Any]) -> Image.Image:
    """The finished painting blurred; `f` 0..1 through the teaser."""
    start = final.width * float(cfg["teaser_blur"])
    radius = start * (1 - (1 - float(cfg["teaser_ease"])) * f)
    img = final.filter(ImageFilter.GaussianBlur(radius))
    return caption(img, cfg["teaser_text"], alpha=min(1.0, f * 4 + 0.25))


CALM_LRA = 10.0      # loudness range (LU) over the stretch used: steady enough to sit under art


def measure_track(path: Path, seconds: float = 50.0) -> dict[str, float]:
    """Leading silence, loudness and loudness range over the first `seconds`."""
    import re
    out = subprocess.run(["ffmpeg", "-v", "info", "-t", f"{seconds:g}", "-i", str(path), "-af",
                          "silencedetect=noise=-45dB:d=0.3,ebur128", "-f", "null", "-"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    lead = 0.0
    start, end = re.search(r"silence_start: ([\d.]+)", out), re.search(r"silence_end: ([\d.]+)", out)
    if start and float(start.group(1)) < 0.2 and end:
        lead = float(end.group(1))
    lufs, lra = re.findall(r"I:\s+(-?[\d.]+) LUFS", out), re.findall(r"LRA:\s+([\d.]+) LU", out)
    return {"lead": round(lead, 2), "lufs": float(lufs[-1]) if lufs else -99.0,
            "lra": float(lra[-1]) if lra else 99.0}


def pick_music(seconds: float, seed: int = 0) -> Path | None:
    """A calm, licence-checked classical recording at least `seconds` long, if any.

    Only steady pieces (loudness range <= CALM_LRA over the opening): the library
    had let in a Dvorak symphony movement swinging 23 LU and a string quartet at
    15, and a painting shouldn't be scored by a sudden fortissimo."""
    manifest = MUSIC / "manifest.json"
    if not manifest.exists():
        return None
    rows = json.loads(manifest.read_text(encoding="utf-8"))
    changed, seen, tracks = False, set(), []
    for m in rows:
        f = MUSIC / m["file"]
        if not f.exists() or m["file"] in seen or m.get("seconds", 0) < seconds:
            continue
        seen.add(m["file"])
        if "lra" not in m:                              # measured once, kept in the manifest
            m.update(measure_track(f))
            changed = True
        # and no voices: a live quartet recording had someone talking at 16-20 s.
        # Unscanned tracks are left out (tools/fetch_classical.py scans new ones).
        if m["lra"] <= CALM_LRA and m.get("voices") == []:
            tracks.append(m)
    if changed:
        manifest.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    if not tracks:
        return None
    return MUSIC / tracks[seed % len(tracks)]["file"]


def choose_music(seconds: float, seed: int, config: dict[str, Any] | None) -> Path | None:
    """The music for a video, by `painting.music`: "library" (the default: a calm track
    from the classical library, if it has been fetched), "none", or a file of one's own
    (looped if shorter than the video)."""
    choice = str(((config or {}).get("painting") or {}).get("music") or "library")
    if choice == "none":
        return None
    if choice != "library":
        own = Path(choice)
        return own if own.is_file() else None
    return pick_music(seconds, seed)


def music_start(path: Path) -> float:
    """Where the music begins (after any leading silence), from the manifest."""
    try:
        rows = json.loads((MUSIC / "manifest.json").read_text(encoding="utf-8"))
        return float(next(m.get("lead", 0.0) for m in rows if m["file"] == path.name))
    except (OSError, ValueError, StopIteration):
        return 0.0


def draw_compass(img: Image.Image, pivot: np.ndarray, tip: np.ndarray, width: int,
                 lifted: bool = False, leg: float | None = None) -> Image.Image:
    """A drawing compass from above: the needle on `pivot`, the pencil lead on `tip`,
    the legs meeting at a hinge with a knurled handle, and a soft shadow. Drawn, not
    a picture, so needle and lead sit exactly where the arc is."""
    p, q = np.asarray(pivot, dtype=float), np.asarray(tip, dtype=float)
    span = np.linalg.norm(q - p)
    # Fixed legs, adjustable angle, as a real compass: nearly closed for small
    # circles, wide open for big ones. (Stretching the legs to each arc kept the
    # angle the same throughout.)
    leg = max(leg or span * 0.62, span / 2 + 1)
    mid = (p + q) / 2
    normal = np.array([-(q - p)[1], (q - p)[0]]) / max(span, 1e-6)
    if normal[1] > 0:                                   # the hinge leans up the page
        normal = -normal
    rise = np.sqrt(max(leg * leg - (span / 2) ** 2, 0.0)) * 0.45   # foreshortened height
    hinge = mid + normal * rise
    u = width / 1080
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ds, d = ImageDraw.Draw(shadow), ImageDraw.Draw(layer)
    off = np.array([14, 18]) * u * (1.6 if lifted else 1.0)

    def legline(draw: ImageDraw.ImageDraw, a: np.ndarray, b: np.ndarray, w: float, fill: tuple) -> None:
        draw.line([tuple(a), tuple(b)], fill=fill, width=max(1, int(w)))
    for a, b in ((hinge, p), (hinge, q)):
        legline(ds, a + off, b + off, 11 * u, (0, 0, 0, 70))
    ds.ellipse([*(hinge + off - 16 * u), *(hinge + off + 16 * u)], fill=(0, 0, 0, 70))
    # the needle leg: polished steel, a fine point
    legline(d, hinge, p, 10 * u, (92, 96, 104, 255))
    legline(d, hinge, p, 5 * u, (190, 196, 205, 255))
    # the pencil leg: steel to a clamp, then a wooden pencil stub and its lead
    clamp = hinge + (q - hinge) * 0.72
    legline(d, hinge, clamp, 10 * u, (92, 96, 104, 255))
    legline(d, hinge, clamp, 5 * u, (190, 196, 205, 255))
    legline(d, clamp, q - (q - clamp) * 0.25, 9 * u, (214, 168, 92, 255))
    legline(d, q - (q - clamp) * 0.25, q, 5 * u, (60, 58, 56, 255))
    d.ellipse([*(clamp - 7 * u), *(clamp + 7 * u)], fill=(70, 74, 82, 255))
    d.ellipse([*(p - 3 * u), *(p + 3 * u)], fill=(40, 40, 44, 255))
    # the hinge and its knurled handle
    d.ellipse([*(hinge - 15 * u), *(hinge + 15 * u)], fill=(78, 82, 90, 255), outline=(40, 42, 48, 255))
    top = hinge + normal * 38 * u
    legline(d, hinge, top, 12 * u, (60, 62, 70, 255))
    for k in range(4):
        a = hinge + normal * (16 + k * 6) * u
        d.line([tuple(a - np.array([normal[1], -normal[0]]) * 6 * u),
                tuple(a + np.array([normal[1], -normal[0]]) * 6 * u)], fill=(120, 124, 132, 255), width=1)
    out = img.convert("RGBA")
    out.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(6 * u)))
    out.alpha_composite(layer)
    return out.convert("RGB")


def draw_ruler(img: Image.Image, ruler: tuple, width: int, holder: Image.Image | None = None,
               pencil: np.ndarray | None = None) -> Image.Image:
    """A clear plastic ruler laid with its edge on the line being ruled; `holder`, a
    flat left hand, presses it down on its body, toward the far end from the pencil."""
    (x, y), ang = ruler
    u = width / 1080
    d = np.array([np.cos(ang), np.sin(ang)])
    if d[0] < 0:                                       # "along" always runs left to right
        d = -d
    n = np.array([-d[1], d[0]])
    # the ruler's body lies away from the pencil hand, which reaches in from the
    # upper right: the pencil runs along the near edge
    if n @ np.array([0.7, -0.7]) > 0:
        n = -n
    a, b = np.array([x, y]) - d * 420 * u, np.array([x, y]) + d * 420 * u
    body = [tuple(a), tuple(b), tuple(b + n * 64 * u), tuple(a + n * 64 * u)]
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(layer)
    dr.polygon([tuple(np.array(p) + np.array([8, 10]) * u) for p in body], fill=(0, 0, 0, 40))
    dr.polygon(body, fill=(225, 235, 240, 110), outline=(120, 135, 145, 220))
    for k in range(-40, 41):                          # ticks every 10 px, longer every 50
        p = np.array([x, y]) + d * k * 10 * u
        dr.line([tuple(p), tuple(p + n * (16 if k % 5 == 0 else 8) * u)], fill=(60, 70, 80, 200), width=1)
    out = img.convert("RGBA")
    out.alpha_composite(layer)
    if holder is not None:
        # the palm over the ruler's body, 250 px along from the line's start
        # the left hand presses the ruler's body to the LEFT of where the pencil works,
        # as a right-handed person does (at either end, the hands met over the line)
        along = float((np.asarray(pencil) - np.array([x, y])) @ d) if pencil is not None else 0.0
        at = np.array([x, y]) + d * (along - 300 * u) + n * 34 * u
        # a wrist turns, it doesn't spin: within 35 degrees of upright
        turn = float(np.clip(np.degrees(np.arctan2(d[1], d[0])), -35, 35))
        turned = holder.rotate(-turn, expand=True, resample=Image.BICUBIC)
        pos = (int(at[0] - turned.width * 0.5), int(at[1] - turned.height * 0.35))
        shade = Image.new("RGBA", turned.size, (20, 14, 10, 0))
        shade.putalpha(turned.getchannel("A").point(lambda v: int(v * 0.3)).filter(ImageFilter.GaussianBlur(10)))
        tmp = Image.new("RGBA", out.size, (0, 0, 0, 0))
        tmp.paste(shade, (pos[0] + int(12 * u), pos[1] + int(14 * u)), shade)
        tmp.paste(turned, pos, turned)
        out.alpha_composite(tmp)
    return out.convert("RGB")


class Camera:
    """A crop of the full-size frame that glides after a target and can pull back."""

    def __init__(self, full: tuple[int, int], out: tuple[int, int], zoom: float, smooth: float):
        self.full, self.out, self.zoom, self.smooth = full, out, zoom, smooth
        self.c = np.array(full, dtype=np.float64) / 2

    def view(self, img: Image.Image, target: np.ndarray | None, dt: float,
             zoom: float | None = None) -> Image.Image:
        z = max(1.0, self.zoom if zoom is None else zoom)
        if target is not None:
            k = 1.0 - np.exp(-dt / max(1e-3, self.smooth))
            self.c = self.c + (np.asarray(target, dtype=np.float64) - self.c) * k
        W, H = self.full
        w, h = W / z, H / z
        x = min(max(self.c[0] - w / 2, 0), W - w)
        y = min(max(self.c[1] - h / 2, 0), H - h)
        return img.resize(self.out, Image.BILINEAR, box=(x, y, x + w, y + h))


def _ease(f: float) -> float:
    f = min(1.0, max(0.0, f))
    return f * f * (3 - 2 * f)


class Alive:
    """A finished picture swaying gently in depth: near parts drift against far ones."""

    def __init__(self, picture: Image.Image, sway: float):
        import carving
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "final.png"
            picture.save(p)
            depth = carving.depth(p)                       # 0 far .. 1 near
        from scipy.ndimage import gaussian_filter
        self.depth = gaussian_filter(depth, 3)            # soft: no tearing at edges
        self.rgb = np.asarray(picture, dtype=np.float32)
        h, w = self.depth.shape
        self.yy, self.xx = np.mgrid[0:h, 0:w].astype(np.float32)
        self.amp = sway * w

    def frame(self, t: float) -> Image.Image:
        from scipy.ndimage import map_coordinates
        # a slow figure-of-eight: ~5 s across, half that up and down
        dx = self.amp * np.sin(2 * np.pi * t / 5.0)
        dy = 0.5 * self.amp * np.sin(4 * np.pi * t / 5.0)
        shift = self.depth - 0.5                           # the middle stays put
        xs, ys = self.xx - dx * shift, self.yy - dy * shift
        out = np.stack([map_coordinates(self.rgb[..., c], [ys, xs], order=1, mode="nearest")
                        for c in range(3)], axis=-1)
        return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def render(frames: Path, strokes: Path, out: Path, seconds: float = 50.0,
           size: tuple[int, int] = (1080, 1920), music: Path | None = None,
           hand: dict[str, Any] | None = None, config: dict[str, Any] | None = None) -> Path:
    """The playback video, `seconds` long, with the hand and an optional music bed."""
    record = json.loads(Path(strokes).read_text(encoding="utf-8"))
    # the style's own playback wishes (a pencil drawing: its hand, a gentler zoom)
    words = {}
    if record.get("medium") in ("pencil", "pen", "compass"):   # it's drawn, not painted
        words["teaser_text"] = "Guess what I'm drawing"
    if record.get("medium") == "carving":
        words["teaser_text"] = "Guess what I'm carving"
    if record.get("medium") == "moulding":
        words["teaser_text"] = "Guess what I'm sculpting"
    cfg = {**DEFAULTS, **words, **(record.get("playback") or {}),
           **((config or {}).get("handplay") or {})}
    which = HAND.with_name(f"{cfg['hand']}.json") if cfg.get("hand") else HAND
    if hand is None and cfg.get("hand_overlay") and which.exists():
        hand = json.loads(which.read_text(encoding="utf-8"))
        hand["file"] = str(which.parent / hand["file"])
    zoom = float(cfg.get("zoom") or 1)
    full = (int(size[0] * zoom) // 2 * 2, int(size[1] * zoom) // 2 * 2)
    player = Player(Path(frames), record, full, hand, cfg)
    camera = Camera(full, size, zoom, float(cfg["camera_smooth"]))
    teaser = float(cfg["teaser"])
    flip = bool(cfg.get("upside_down"))
    reveal = float(cfg["pullback"]) + (float(cfg["turn"]) if flip else 0.0)
    alive_secs = float(cfg.get("alive") or 0)
    video, clock = schedule(player.strokes, seconds - teaser,
                            {**cfg, "hold": float(cfg["hold"]) + reveal + alive_secs})
    fps = int(cfg["fps"])
    total = teaser + video[-1]
    final = player._img(player.shots[-1].frame)       # the right way up
    done_at = teaser + video[-2]                       # the last breakpoint: the painting done
    hold_from = done_at + reveal
    alive = None
    if alive_secs:                                     # (graded with every other frame below)
        alive = Alive(final.resize(size, Image.LANCZOS), float(cfg["alive_sway"]))
        alive_from = hold_from + 0.6                       # a beat of stillness, then it breathes
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{size[0]}x{size[1]}", "-r", str(fps), "-i", "-"]
    if music:
        lead = music_start(Path(music))                 # no dead air before the first note
        cmd += ["-stream_loop", "-1", "-ss", f"{lead:.2f}", "-i", str(music), "-filter_complex",
                f"[1:a]atrim=0:{total:.2f},asetpts=PTS-STARTPTS,afade=t=in:d=1.0,"
                f"afade=t=out:st={max(0, total - 3):.2f}:d=3,loudnorm=I=-16:TP=-1.5:LRA=11[a]",
                "-map", "0:v", "-map", "[a]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-t", f"{total:.3f}", "-movflags", "+faststart", str(out)]
    import progress
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    count = int(total * fps)
    preview = Path(frames).parent / "preview.jpg"
    import brushwork
    lut = brushwork.grade_lut(record.get("grade"))      # the style's levels, every frame
    live_zoom = last_zoom = zoom
    try:
        for k in range(count):
            v = k / fps
            if v < teaser:
                img = teaser_frame(final.resize(size, Image.BILINEAR), v / teaser, cfg)
            elif v < done_at:
                t = float(np.interp(v - teaser, video, clock))
                frame = player.frame(t, 1.0 / fps)
                goal = float(cfg["subject_zoom"]) if player.on_subject else zoom
                live_zoom += (goal - live_zoom) * (1 - np.exp(-(1.0 / fps) / 1.2))
                img = camera.view(frame, player.pos, 1.0 / fps, live_zoom)
                last_zoom = live_zoom
            else:
                # The reveal: pull back from the close-up, turn it the right way up, hold.
                back = float(cfg["pullback"])
                pull = _ease((v - done_at) / back) if back > 0 else 1.0     # 0: no camera move
                shown = final.rotate(180) if flip else final
                img = camera.view(shown, None, 1.0 / fps, last_zoom + (1 - last_zoom) * pull)
                if flip and v >= done_at + float(cfg["pullback"]):
                    turn = _ease((v - done_at - float(cfg["pullback"])) / float(cfg["turn"]))
                    # from upside down (0) to the right way up (180 turned): it must
                    # end on the upright painting, not back where it started
                    img = shown.resize(size, Image.BILINEAR).rotate(
                        180 * turn, resample=Image.BICUBIC, fillcolor=(12, 10, 9))
                if alive is not None and v >= alive_from:
                    ease = min(1.0, (v - alive_from) / 1.5)   # the sway grows in, never jolts
                    img = alive.frame((v - alive_from) * ease)
                if v >= hold_from:
                    img = caption(img, cfg["ending_text"], alpha=min(1.0, (v - hold_from) * 2))
            if lut:
                img = img.point(lut)
            proc.stdin.write(img.tobytes())
            if k % fps == 0:
                progress.step(k, count, f"Second {k // fps} of {int(total)}")
                if k % (fps * 4) == 0:
                    img.resize((img.width // 2, img.height // 2)).save(preview, quality=80)
                    progress.set_preview(preview)
    finally:
        proc.stdin.close()
        proc.wait()
    if proc.returncode:
        raise RuntimeError("ffmpeg failed")
    return out


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Painting playback with a hand on the brush.")
    ap.add_argument("painting_dir", help="a brushwork.paint output folder")
    ap.add_argument("out")
    ap.add_argument("--seconds", type=float, default=50.0)
    ap.add_argument("--music", help="a recording; default: one from the classical library")
    ap.add_argument("--no-music", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    d = Path(a.painting_dir)
    first = next(iter(sorted((d / "frames").glob("*.png"))))
    w, h = Image.open(first).size
    size = (1080, int(round(1080 * h / w / 2)) * 2)
    music = None if a.no_music else (Path(a.music) if a.music else pick_music(a.seconds))
    if music is None and not a.no_music:
        LOG.warning("no music: the classical library is empty (tools/fetch_classical.py)")
    render(d / "frames", d / "strokes.json", Path(a.out), a.seconds, size, music)


if __name__ == "__main__":
    main()
