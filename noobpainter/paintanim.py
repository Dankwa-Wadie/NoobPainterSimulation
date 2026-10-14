"""Painted animation: a picture that moves, every frame painted afresh.

After two kinds of clip the channel was shown (2026-10-13):

  van_gogh   a painting in motion, as in "Loving Vincent": the scene drifts in depth,
             skies and water swirl, and every held frame is painted again in short,
             thick, form-following strokes, so the brushwork boils.
  oil_morph  thick black oil paint on white: one figure smeared from pose to pose,
             each in-between painted in broad black strokes with a few white lifts.

No video model: the motion comes from the picture itself (local depth for parallax,
smooth flow fields for swirl, optical flow to morph between poses), and the paint
from a fast stroke renderer (OpenCV), at 12 paintings a second held for two frames
each, as both references do.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

import cv2
import numpy as np

LOG = logging.getLogger("mken.paintanim")

DEFAULTS: dict[str, Any] = {
    "seconds": 20.0,
    "fps": 24,
    "hold": 2,                       # each painting held this many frames ("on twos")
    "size": [1080, 1920],
    "work": [720, 1280],             # painted at this size, then scaled up
}
# What moves, when the video model makes the motion. Subtle on purpose (2026-10-13):
# Wan 2.2 5B on this Mac animates what is already in the picture well (breathing,
# drifting skies, a slight turn) but breaks into noise when asked to bring anything
# new into the frame (a hand, an object), whatever the file or size. Performances
# with real action are for a cloud model, case by case, with the user's go-ahead.
MOTION = {"van_gogh": "the stars twinkle and the sky slowly swirls, clouds drift, a gentle breeze "
                      "sways the scene, static camera",
          "oil_morph": "the person breathes slowly, blinks, and shifts their gaze slightly, subtle "
                       "natural movement, static camera"}
# chained beats for a longer living painting: each a small continuation of the last
BEATS = {"van_gogh": ["the stars twinkle and the swirls in the sky slowly turn, static camera",
                      "a gentle breeze sways the flowers and the clouds drift, static camera",
                      "the moon glows and the sky keeps slowly swirling, static camera"],
         "oil_morph": ["the person breathes slowly and blinks, static camera",
                       "the person shifts their gaze slightly to the side, static camera",
                       "the person looks back and breathes, static camera"]}
KINDS = {
    "van_gogh": {"spacing": 7, "length": [10, 22], "width": [4, 7], "jitter": 0.06,
                 "drift": 0.012, "swirl": 9.0, "underpaint": 0.35},
    "oil_morph": {"spacing": 8, "length": [10, 46], "width": [6, 18], "jitter": 0.0,
                  "drift": 0.0, "swirl": 0.0, "underpaint": 0.0},
}


def _field(shape: tuple[int, int], cells: int, rng: np.random.Generator) -> np.ndarray:
    """Smooth random field in -1..1 (low-res noise scaled up)."""
    h, w = shape
    small = rng.uniform(-1, 1, (max(2, h * cells // w), cells)).astype(np.float32)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)


def form_angles(img: np.ndarray, blur: float = 6.0) -> np.ndarray:
    """Stroke direction per pixel: along the forms (structure tensor, smoothed)."""
    g = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY).astype(np.float32)
    gx, gy = cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1)
    jxx, jyy, jxy = (cv2.GaussianBlur(a, (0, 0), blur) for a in (gx * gx, gy * gy, gx * gy))
    return 0.5 * np.arctan2(2 * jxy, jxx - jyy) + np.pi / 2


def paint(src: np.ndarray, angles: np.ndarray, kind: str, rng: np.random.Generator) -> np.ndarray:
    """One frame painted in strokes from `src` (RGB uint8). Fresh randomness each call:
    that is the boil."""
    k = KINDS[kind]
    h, w = src.shape[:2]
    if kind == "oil_morph":
        canvas = np.full_like(src, 246)
    else:
        canvas = cv2.GaussianBlur(src, (0, 0), 6)
        canvas = (canvas * k["underpaint"] + src.mean((0, 1)) * (1 - k["underpaint"])).astype(np.uint8)
    sp = k["spacing"]
    ys, xs = np.mgrid[sp // 2:h:sp, sp // 2:w:sp]
    xs = (xs + rng.uniform(-sp / 2, sp / 2, xs.shape)).clip(0, w - 1).astype(int).ravel()
    ys = (ys + rng.uniform(-sp / 2, sp / 2, ys.shape)).clip(0, h - 1).astype(int).ravel()
    order = rng.permutation(len(xs))
    xs, ys = xs[order], ys[order]
    grey = cv2.cvtColor(src, cv2.COLOR_RGB2GRAY)
    if kind == "oil_morph":
        # tones relative to the figure's own range: a dark reference put the whole
        # face in the black band and the features vanished
        fig = grey[grey < 215]
        if fig.size:
            lo, hi = np.percentile(fig, [5, 99])
            g = np.clip((grey.astype(np.float32) - lo) / max(1.0, hi - lo) * 200, 0, 200)
            grey = np.where(grey < 215, g, 255).astype(np.uint8)
            grey = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(grey)
    for x, y in zip(xs, ys):
        c = src[y, x].astype(np.float32)
        if kind == "oil_morph":
            v = float(grey[y, x])
            if v > 215 and not (src[y, x, 0] > 110 and src[y, x, 0] > 1.6 * src[y, x, 1]):
                continue                                     # paper stays bare
            # four tones of paint, following the picture's light (two tones turned the
            # figure into a flat black silhouette and lost the face)
            c = np.full(3, 14.0 if v < 55 else 48.0 if v < 105 else 105.0 if v < 160 else 175.0)
            c += rng.normal(0, 6)
            r, g, b = (float(v) for v in src[y, x])
            if r > 110 and r > g * 1.6 and r > b * 1.6:      # one colour survives: red
                c = np.array([min(255, r * 1.1), g * 0.6, b * 0.6])
        else:
            c = c * (1 + rng.normal(0, k["jitter"], 3))
        a = angles[y, x] + rng.normal(0, 0.12)
        ln = rng.uniform(*k["length"]) / 2
        wd = int(rng.uniform(*k["width"]))
        dx, dy = np.cos(a) * ln, np.sin(a) * ln
        p, q = (int(x - dx), int(y - dy)), (int(x + dx), int(y + dy))
        cv2.line(canvas, p, q, tuple(int(v) for v in np.clip(c, 0, 255)), wd, cv2.LINE_AA)
        # a lighter ridge along one side: the paint's thickness catching the light
        hi = np.clip(c * 1.18 + 12, 0, 255)
        off = (int(-np.sin(a) * wd * 0.3), int(np.cos(a) * wd * 0.3))
        cv2.line(canvas, (p[0] - off[0], p[1] - off[1]), (q[0] - off[0], q[1] - off[1]),
                 tuple(int(v) for v in hi), max(1, wd // 3), cv2.LINE_AA)
    if kind == "oil_morph":                                  # a few white lifts in the darks
        for _ in range(int(len(xs) * 0.06)):
            x, y = int(rng.uniform(0, w)), int(rng.uniform(0, h))
            if 120 < grey[y, x] < 215:                       # lifts where the picture is light
                a = angles[y, x]
                ln = rng.uniform(6, 16)
                cv2.line(canvas, (int(x - np.cos(a) * ln), int(y - np.sin(a) * ln)),
                         (int(x + np.cos(a) * ln), int(y + np.sin(a) * ln)), (232, 232, 232),
                         int(rng.uniform(3, 7)), cv2.LINE_AA)
    return canvas


# --- motion -------------------------------------------------------------------------

class Drift:
    """A still in slow motion: depth parallax, and swirl where the picture is far
    (sky, water) carried by smooth flow fields that change over time."""

    def __init__(self, img: np.ndarray, depth: np.ndarray, kind: str, seed: int):
        k = KINDS[kind]
        self.img, self.depth, self.k = img, depth, k
        h, w = depth.shape
        rng = np.random.default_rng(seed)
        self.fields = [(_field((h, w), 5, rng), _field((h, w), 5, rng)) for _ in range(4)]
        self.yy, self.xx = np.mgrid[0:h, 0:w].astype(np.float32)
        self.far = np.clip(1.0 - depth * 1.6, 0, 1).astype(np.float32)   # swirl lives in the distance

    def frame(self, t: float, seconds: float) -> tuple[np.ndarray, np.ndarray]:
        h, w = self.depth.shape
        f = t / seconds
        cam = self.k["drift"] * w * np.array([np.sin(2 * np.pi * f), 0.5 * np.sin(4 * np.pi * f)])
        par = (self.depth - 0.5).astype(np.float32)
        # the swirl: a slowly turning blend of the flow fields
        s = np.zeros((h, w), np.float32), np.zeros((h, w), np.float32)
        for i, (fx, fy) in enumerate(self.fields):
            wgt = np.cos(2 * np.pi * (f * 0.6 + i / len(self.fields))) * 0.5 + 0.5
            s = (s[0] + fx * wgt, s[1] + fy * wgt)
        sx = s[0] * self.k["swirl"] * self.far * np.sin(2 * np.pi * f * 1.5)
        sy = s[1] * self.k["swirl"] * self.far * np.cos(2 * np.pi * f * 1.5)
        mx = (self.xx - cam[0] * par + sx).astype(np.float32)
        my = (self.yy - cam[1] * par + sy).astype(np.float32)
        moved = cv2.remap(self.img, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        # strokes follow the moving picture's own forms: its painted swirls already flow,
        # and overriding them in the sky with the motion field made it messier
        return moved, form_angles(moved)


def fill(img: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """A picture of any shape filling `size`: scaled to cover it, cut centred."""
    W, H = size
    h, w = img.shape[:2]
    s = max(W / w, H / h)
    img = cv2.resize(img, (max(W, round(w * s)), max(H, round(h * s))), interpolation=cv2.INTER_AREA)
    y, x = (img.shape[0] - H) // 2, (img.shape[1] - W) // 2
    return img[y:y + H, x:x + W]


class Sway:
    """One figure and no other poses: it breathes and sways, slowly. The bottom stays
    put; the higher up, the more it moves, as a body does. Loops."""

    def __init__(self, img: np.ndarray, seed: int):
        self.img = img
        h, w = img.shape[:2]
        self.yy, self.xx = np.mgrid[0:h, 0:w].astype(np.float32)
        self.lift = ((h - self.yy) / h) ** 1.5             # 0 at the bottom, 1 at the top
        self.phase = (seed % 100) / 100 * 2 * np.pi

    def frame(self, t: float, seconds: float) -> tuple[np.ndarray, np.ndarray]:
        h, w = self.img.shape[:2]
        f = t / seconds
        breath = 0.012 * np.sin(2 * np.pi * f * 4)                       # four breaths a loop
        sway = 0.018 * w * np.sin(2 * np.pi * f * 2 + self.phase)       # two sways a loop
        mx = self.xx - sway * self.lift + (self.xx - w / 2) * breath * 0.5 * self.lift
        my = self.yy + (h - self.yy) * breath * self.lift
        img = cv2.remap(self.img, mx.astype(np.float32), my.astype(np.float32), cv2.INTER_LINEAR,
                        borderMode=cv2.BORDER_REPLICATE)
        return img, form_angles(img, 10)


class Morph:
    """One figure in several poses, each smeared into the next along optical flow."""

    def __init__(self, poses: list[np.ndarray]):
        self.poses = poses
        grey = [cv2.cvtColor(p, cv2.COLOR_RGB2GRAY) for p in poses]
        self.flows = []
        for a, b in zip(grey, grey[1:] + grey[:1]):
            fw = cv2.calcOpticalFlowFarneback(a, b, None, 0.5, 5, 41, 5, 7, 1.5, 0)
            bw = cv2.calcOpticalFlowFarneback(b, a, None, 0.5, 5, 41, 5, 7, 1.5, 0)
            self.flows.append((fw, bw))
        h, w = grey[0].shape
        self.yy, self.xx = np.mgrid[0:h, 0:w].astype(np.float32)

    def frame(self, t: float, seconds: float) -> tuple[np.ndarray, np.ndarray]:
        n = len(self.poses)
        pos = (t / seconds) * n
        i = int(pos) % n
        f = pos - int(pos)
        f = 0.0 if f < 0.35 else min(1.0, (f - 0.35) / 0.65)   # hold each pose, then smear
        f = f * f * (3 - 2 * f)
        a, b = self.poses[i], self.poses[(i + 1) % n]
        fw, bw = self.flows[i]
        wa = cv2.remap(a, self.xx - fw[..., 0] * f, self.yy - fw[..., 1] * f, cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_REPLICATE)
        wb = cv2.remap(b, self.xx - bw[..., 0] * (1 - f), self.yy - bw[..., 1] * (1 - f),
                       cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        img = cv2.addWeighted(wa, 1 - f, wb, f, 0)
        return img, form_angles(img, 10)


class Clip:
    """Real motion from a video model (wanvideo.py): its frames in order, played
    forward then back so a short clip fills the video without a jump."""

    def __init__(self, frames: list[np.ndarray], fps: int = 24, once: bool = False):
        self.frames, self.fps, self.once = frames, fps, once

    def frame(self, t: float, seconds: float) -> tuple[np.ndarray, np.ndarray]:
        n = len(self.frames)
        if self.once:                                   # a performance: play it, then hold the end
            img = self.frames[min(n - 1, int(t * self.fps))]
            return img, form_angles(img, 6 if img.mean() > 0 else 10)
        i = int(t * self.fps) % (2 * n - 2) if n > 1 else 0
        img = self.frames[i if i < n else 2 * n - 2 - i]
        return img, form_angles(img, 6 if img.mean() > 0 else 10)


def chain(start: Path, beats: list[str], folder: Path, config: dict[str, Any], seed: int) -> list[Path]:
    """One Wan clip per beat, each starting from the last frame of the one before, so the
    motion carries on; the frames joined (each clip's first frame, a repeat, dropped).
    Clips already made are kept, so a stopped run picks up where it left off."""
    import progress
    import wanvideo
    frames: list[Path] = []
    first = start
    progress.stage("plan", f"Generating the motion: {len(beats)} clips (Wan 2.2, ~15 min each)",
                   total=len(beats))
    for i, beat in enumerate(beats):
        out = folder / f"{i:02d}"
        made = sorted(out.glob("*.png"))
        if not made:
            progress.step(i, detail=f"Clip {i + 1} of {len(beats)}: {beat[:90]}")
            made = wanvideo.generate(first, beat, out, config, seed + i)
        frames += made if i == 0 else made[1:]
        first = made[-1]
    progress.step(len(beats))
    return frames


# --- the video ----------------------------------------------------------------------

def render(motion: Drift | Morph, kind: str, out: Path, cfg: dict[str, Any], music: Path | None,
           seed: int) -> Path:
    import handplay
    import progress
    W, H = cfg["size"]
    fps, hold, secs = int(cfg["fps"]), int(cfg["hold"]), float(cfg["seconds"])
    count = int(secs * fps)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(fps), "-i", "-"]
    if music:
        lead = handplay.music_start(music)
        cmd += ["-stream_loop", "-1", "-ss", f"{lead:.2f}", "-i", str(music), "-filter_complex",
                f"[1:a]atrim=0:{secs:.2f},asetpts=PTS-STARTPTS,afade=t=in:d=1.0,"
                f"afade=t=out:st={max(0, secs - 2.5):.2f}:d=2.5,loudnorm=I=-16:TP=-1.5:LRA=11[a]",
                "-map", "0:v", "-map", "[a]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-t", f"{secs:.3f}", "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    rng = np.random.default_rng(seed)
    held = None
    try:
        for k in range(count):
            if k % hold == 0:                               # a new painting, "on twos"
                src, ang = motion.frame(k / fps, secs)
                painted = paint(src, ang, kind, rng)
                held = cv2.resize(painted, (W, H), interpolation=cv2.INTER_CUBIC)
            proc.stdin.write(held.tobytes())
            if k % fps == 0:
                progress.step(k, count, f"Painting second {k // fps + 1} of {int(secs)}")
    finally:
        proc.stdin.close()
        proc.wait()
    if proc.returncode:
        raise RuntimeError("ffmpeg failed")
    return out


def footage(video: Path, folder: Path, size: tuple[int, int], fps: int = 12) -> tuple[list[np.ndarray], float]:
    """A real clip as the motion, the way "Loving Vincent" was made: its frames at
    `fps`, cropped to portrait (centred) and sized for the painter."""
    folder.mkdir(parents=True, exist_ok=True)
    W, H = size
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf",
                    f"fps={fps},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}",
                    str(folder / "%05d.png")], check=True)
    frames = [cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB) for p in sorted(folder.glob("*.png"))]
    if not frames:
        raise RuntimeError("no frames could be read from that video")
    return frames, len(frames) / fps


def make(idea: str, kind: str, config: dict[str, Any] | None = None, account: str = "",
         workdir: Path | None = None, reference_image: Path | None = None,
         source_video: Path | None = None, reference_images: list[Path] | None = None) -> dict[str, Any]:
    """One idea (or a picture, or a clip) -> one painted animation, saved in `workdir`.
    oil_morph with several pictures (`reference_images`): they are the poses, in order,
    each smeared into the next and the last back into the first, so it loops."""
    import datetime as dt
    import hashlib
    import carving
    import handplay
    import localimage
    import progress
    config = config or {}
    cfg = {**DEFAULTS, **(config.get("paintanim") or {})}
    workdir = (workdir or Path("out") / f"paintanim-{dt.datetime.now():%Y%m%d-%H%M%S}").resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    seed = int(hashlib.sha1(f"{idea}|{kind}".encode()).hexdigest()[:8], 16)
    owns = not progress.active()
    if owns:
        progress.start(account, f"Animating: {idea}", kind="painting")
    progress.set_workdir(workdir)
    try:
        Wk, Hk = cfg["work"]
        if source_video:                              # real footage: no picture to draw
            progress.stage("plan", "Reading the footage")
            frames, secs = footage(source_video, workdir / "footage", (Wk, Hk), int(cfg["fps"]) // int(cfg["hold"]))
            cfg["seconds"] = secs + float(cfg.get("end_hold", 1.5))
            motion = Clip(frames, int(cfg["fps"]) // int(cfg["hold"]), once=True)
            return _finish(idea, kind, config, account, workdir, cfg, motion, seed, owns)
        progress.stage("reference", f"{kind.replace('_', ' ')}: {idea[:120]}")
        look = {"van_gogh": ("oil painting in the style of Vincent van Gogh, thick swirling impasto "
                             "brushstrokes, vivid blues and yellows, expressive sky, one clear scene"),
                "oil_morph": ("expressive black oil paint on white paper, one figure, head and "
                              "shoulders, bold dark silhouette, plain white background")}[kind]
        local = {**((config.get("longform") or {}).get("local_image") or {}), "style": look,
                 "negative": "text, watermark, signature, frame, photo", "width": 576, "height": 1024}
        lcfg = {"longform": {"local_image": local}}
        given = [Path(p) for p in (reference_images or ([reference_image] if reference_image else []))]
        poses = 3 if kind == "oil_morph" else 1
        if given and (kind != "oil_morph" or len(given) > 1 or not localimage.installed(
                localimage.settings(lcfg))):
            # your pictures, and no image model: several are the poses; one alone sways
            poses = len(given) if kind == "oil_morph" else 1
        pics = []
        try:
            for i in range(poses):
                p = workdir / f"reference-{i}.png"
                if i < len(given):
                    import shutil
                    shutil.copy(given[i], p)
                elif not p.exists():
                    prompt = idea if i == 0 else f"{idea}, {['turning to the side', 'looking up'][i - 1]}"
                    localimage.generate(prompt, p, lcfg, seed=seed + i)
                pics.append(p)
        finally:
            localimage.release(localimage.settings(lcfg))
        imgs = [fill(cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB), (Wk, Hk)) for p in pics]
        progress.stage("plan", "Working out the motion")
        import wanvideo
        # Procedural motion by default: local Wan broke too often on this Mac (a sunflower
        # melted even under "gentle breeze"); "motion": "wan" turns it back on.
        if cfg.get("motion", "drift") == "wan" and wanvideo.installed(config):
            # real motion from the local video model, then every frame repainted
            beats = cfg.get("beats") or ([f"{idea}, {b}" for b in BEATS[kind]]
                                         if cfg.get("chain", True) else None)
            if beats:
                frames = chain(pics[0], beats, workdir / "clips", config, seed)
                # one Wan frame per painting, held two frames: on twos, at the clip's own pace;
                # then the last pose held
                fps_src = int(cfg["fps"]) // int(cfg["hold"])
                cfg["seconds"] = len(frames) / fps_src + float(cfg.get("end_hold", 3.0))
                motion = Clip([cv2.resize(cv2.cvtColor(cv2.imread(str(f)), cv2.COLOR_BGR2RGB), (Wk, Hk),
                                          interpolation=cv2.INTER_AREA) for f in frames], fps_src, once=True)
            else:
                clip_dir = workdir / "clip"
                frames = sorted(clip_dir.glob("*.png"))
                if not frames:
                    progress.stage("plan", "Generating the motion (Wan 2.2, local; can take most of an hour)")
                    frames = wanvideo.generate(pics[0], cfg.get("motion_prompt") or f"{idea}, {MOTION[kind]}",
                                               clip_dir, config, seed)
                motion = Clip([cv2.resize(cv2.cvtColor(cv2.imread(str(f)), cv2.COLOR_BGR2RGB), (Wk, Hk),
                                          interpolation=cv2.INTER_AREA) for f in frames])
        elif kind == "oil_morph" and len(imgs) > 1:
            motion: Drift | Morph | Sway = Morph(imgs)
        elif kind == "oil_morph":                     # one picture, no poses to smear between
            motion = Sway(imgs[0], seed)
        else:
            d = carving.depth(pics[0])
            motion = Drift(imgs[0], cv2.resize(d.astype(np.float32), (Wk, Hk)), kind, seed)
        return _finish(idea, kind, config, account, workdir, cfg, motion, seed, owns)
    except Exception as e:
        if owns:
            progress.fail(str(e))
        raise


def _finish(idea: str, kind: str, config: dict[str, Any], account: str, workdir: Path,
            cfg: dict[str, Any], motion: Any, seed: int, owns: bool) -> dict[str, Any]:
    import handplay
    import progress
    try:
        progress.stage("paint", "Painting every frame")
        music = handplay.choose_music(float(cfg["seconds"]), seed, config)
        video = workdir / "animation.mp4"
        render(motion, kind, video, cfg, music, seed)
        progress.stage("assemble", "Saving")
        title = {"van_gogh": "A Van Gogh painting comes alive 🌌", "oil_morph": "Paint that moves 🖤"}[kind]
        desc = (f"Every frame painted afresh, stroke by stroke, by a computer simulation, not a human "
                f"artist ({kind.replace('_', ' ')} style).")
        if music:
            manifest = json.loads((handplay.MUSIC / "manifest.json").read_text(encoding="utf-8"))
            m = next((m for m in manifest if m["file"] == music.name), None)
            if m:
                desc += (f"\n\nMusic: {m['title'].removeprefix('File:').rsplit('.', 1)[0]} "
                         f"({m['licence']}, via Wikimedia Commons / Musopen).")
        (workdir / "credits.txt").write_text(f"{title}\n\n{desc}\n", encoding="utf-8")
        if owns:
            progress.finish(title)
        return {"video": video, "title": title, "description": desc, "workdir": str(workdir)}
    except Exception as e:
        if owns:
            progress.fail(str(e))
        raise
