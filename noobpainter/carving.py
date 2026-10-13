"""Carving: a relief carved from a flat block, stroke by stroke, rendered in Blender.

  1. The target: the reference picture's depth (Depth Anything V2 Small, local)
     as the relief's height, with a little of the picture's fine detail on top,
     so feathers and wrinkles are cut too.
  2. The carving (here, in numpy): the block starts flat at full height and is
     cut down with a wide gouge, a medium gouge, a V-tool and a detail tool in
     turn. Each tool lays strokes where the block still stands above the target,
     along the forms, and never cuts below what its stage allows: rough tools
     leave material for the fine ones, as a carver does.
  3. Snapshots of the surface every few strokes are rendered by Blender
     (relief_scene.py) in wood, marble or clay under a raking light, with a
     straight-on camera, so a stroke's position on the block is its position in
     the frame.

The output is what brushwork.paint gives a painting (frames with their hand
times, strokes.json, the finished picture), so handplay plays it the same way:
teaser, the hand on the tool, the close-up, music and the reveal.
"""
from __future__ import annotations

import json
import os
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageFilter

LOG = logging.getLogger("mken.carving")
ROOT = Path(__file__).resolve().parent
SCENE = ROOT / "relief_scene.py"
BLENDER = Path(os.environ.get("NOOBPAINTER_BLENDER", "/Applications/Blender.app/Contents/MacOS/Blender"))
DEPTH_MODEL = "depth-anything/Depth-Anything-V2-Small-hf"

# (name, radius in canvas units, margin left above the target, most strokes, s of hand time a unit)
TOOLS = [("wide gouge", 22.0, 0.10, 700, 0.010), ("gouge", 11.0, 0.045, 1300, 0.014),
         ("v-tool", 5.0, 0.012, 2200, 0.020), ("detail", 2.4, 0.0, 2400, 0.030)]
# Clay is built up, not cut: lumps pressed on, then pellets, then a modelling tool
MOULD_TOOLS = [("lumps", 22.0, 0.10, 700, 0.010), ("pellets", 11.0, 0.045, 1300, 0.014),
               ("modelling tool", 5.0, 0.012, 2200, 0.020), ("fine tool", 2.4, 0.0, 2400, 0.030)]


def depth(picture: Path) -> np.ndarray:
    """Relative depth 0 (far) .. 1 (near), at the picture's size."""
    import os
    import torch
    cache = Path.home() / ".cache" / "huggingface" / "hub" / ("models--" + DEPTH_MODEL.replace("/", "--"))
    if cache.exists():                               # downloaded once: no network after
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from transformers import pipeline
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    pipe = pipeline("depth-estimation", model=DEPTH_MODEL, device=device)
    img = Image.open(picture).convert("RGB")
    d = np.asarray(pipe(img)["predicted_depth"].squeeze().cpu(), dtype=np.float64)
    d = (d - d.min()) / max(1e-9, d.max() - d.min())
    return np.asarray(Image.fromarray((d * 65535).astype(np.uint16)).resize(img.size, Image.BILINEAR),
                      dtype=np.float64) / 65535


def target(picture: Path, units: tuple[int, int], detail: float = 0.03) -> np.ndarray:
    """The finished relief's height (0..1) on a grid of `units` (w, h) canvas units / 2.

    Depth gives the big forms; the picture's own light and dark give the surface
    detail, in two bands: fine (texture, feathers) and medium (shell plates, folds,
    which depth alone smooths away). Their strength is set so the detail always
    stands `detail` high on average (its std): a fixed amount suited the busy owl
    and left a plain turtle bare."""
    w, h = units
    d = depth(picture)
    grey = Image.open(picture).convert("L")
    lum = np.asarray(grey, dtype=np.float64) / 255
    blur4 = np.asarray(grey.filter(ImageFilter.GaussianBlur(4)), dtype=np.float64) / 255
    blur14 = np.asarray(grey.filter(ImageFilter.GaussianBlur(14)), dtype=np.float64) / 255
    surface = (lum - blur4) + 0.6 * (blur4 - blur14)
    surface *= detail / max(1e-6, surface.std())
    hgt = np.clip(0.12 + 0.8 * d + surface, 0, 1)
    return np.asarray(Image.fromarray((hgt * 65535).astype(np.uint16)).resize((w, h), Image.BILINEAR),
                      dtype=np.float64) / 65535


def _disk(r: float) -> np.ndarray:
    k = int(np.ceil(r))
    yy, xx = np.mgrid[-k:k + 1, -k:k + 1]
    return np.clip(1 - (xx * xx + yy * yy) / (r * r), 0, 1)       # a rounded gouge's profile


def cut(C: np.ndarray, T: np.ndarray, pts: list[tuple[float, float]], r: float, margin: float,
        bite: float, scale: float) -> None:
    """Carve along `pts` (canvas units): lower C by up to `bite` under the tool,
    never below the target plus `margin`. `scale` is grid cells per canvas unit."""
    rr = r * scale
    disk = _disk(rr)
    k = disk.shape[0] // 2
    h, w = C.shape
    step = max(1.0, rr * 0.5)
    path = []
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        n = max(1, int(np.hypot(x1 - x0, y1 - y0) * scale / step))
        path += [(x0 + (x1 - x0) * t / n, y0 + (y1 - y0) * t / n) for t in range(n)]
    path.append(pts[-1])
    for x, y in path:
        cx, cy = int(x * scale), int(y * scale)
        y0, y1, x0, x1 = max(0, cy - k), min(h, cy + k + 1), max(0, cx - k), min(w, cx + k + 1)
        if y0 >= y1 or x0 >= x1:
            continue
        prof = disk[y0 - cy + k:y1 - cy + k, x0 - cx + k:x1 - cx + k]
        floor = T[y0:y1, x0:x1] + margin
        C[y0:y1, x0:x1] = np.maximum(np.minimum(C[y0:y1, x0:x1], C[y0:y1, x0:x1] - bite * prof),
                                     np.minimum(floor, C[y0:y1, x0:x1]))


def shaped(T: np.ndarray, r: float, scale: float) -> np.ndarray:
    """The target as a tool of radius `r` can cut it: no detail finer than the tool.
    (Cutting to the full target plus a margin, the wide gouge left a raised copy
    of every feather, and the owl was complete in the first minute.)"""
    if r * scale < 1.2:
        return T
    # Swell the target by the tool's size, then smooth: always above the true
    # surface, with nothing finer than the tool. (max(blur(T), T) kept every
    # raised feather and eye ring: they stand above their own blur.)
    from scipy.ndimage import gaussian_filter, grey_dilation
    k = max(3, int(r * scale * 1.6) | 1)
    return np.maximum(gaussian_filter(grey_dilation(T, size=(k, k)), r * scale * 0.5), T - 1e-3)


def plan_tool(C: np.ndarray, T: np.ndarray, r: float, margin: float, budget: int, scale: float,
              rng: np.random.Generator) -> list[dict[str, Any]]:
    """Strokes for one tool: from where the most is left to cut, along the forms."""
    err = C - (T + margin)
    gy, gx = np.gradient(np.asarray(Image.fromarray((T * 255).astype(np.uint8))
                                    .filter(ImageFilter.GaussianBlur(r * scale)), dtype=np.float64))
    h, w = C.shape
    cell = max(2, int(r * scale * 1.3))
    out = []
    for cy in range(0, h, cell):
        for cx in range(0, w, cell):
            e = err[cy:cy + cell, cx:cx + cell]
            if e.max() < 0.015:
                continue
            iy, ix = np.unravel_index(int(np.argmax(e)), e.shape)
            x, y = (cx + ix) / scale, (cy + iy) / scale
            pts = [(x, y)]
            heading = None
            for _ in range(int(rng.integers(4, 10))):
                px, py = int(x * scale), int(y * scale)
                if not (0 <= px < w and 0 <= py < h):
                    break
                dx, dy = -gy[py, px], gx[py, px]               # along the form
                n = np.hypot(dx, dy)
                if n < 1e-6:
                    a = rng.uniform(0, np.pi) if heading is None else np.arctan2(heading[1], heading[0])
                    dx, dy = np.cos(a), np.sin(a)
                else:
                    dx, dy = dx / n, dy / n
                if heading is not None and dx * heading[0] + dy * heading[1] < 0:
                    dx, dy = -dx, -dy
                heading = (dx, dy)
                x, y = x + dx * r * 0.9, y + dy * r * 0.9
                pts.append((x, y))
            out.append({"pts": [(round(a, 1), round(b, 1)) for a, b in pts], "err": float(e.max())})
    if len(out) > budget:
        wgt = np.array([o["err"] for o in out]) + 1e-3
        out = [out[i] for i in rng.choice(len(out), budget, replace=False, p=wgt / wgt.sum())]
    # a carver works across the block in bands, not at random
    band = r * 4
    out.sort(key=lambda o: (int(o["pts"][0][1] // band),
                            o["pts"][0][0] if int(o["pts"][0][1] // band) % 2 == 0 else -o["pts"][0][0]))
    return out


def plan_finish(C: np.ndarray, T: np.ndarray, r: float, scale: float, tolerance: float = 0.006,
                longest: float = 40.0) -> list[dict[str, Any]]:
    """The finishing pass: a flat chisel row by row over everything not yet down to
    the surface. (Stopping each tool at "fewer than 6% of spots 0.02 too high" left
    a pitted surface that a raking light shows plainly.)"""
    h, w = C.shape
    out = []
    step = max(1, int(r * scale))
    for i, row in enumerate(range(0, h, step)):
        high = (C[row:row + step] - T[row:row + step] > tolerance).any(axis=0)
        xs = np.nonzero(high)[0]
        if not len(xs):
            continue
        runs = np.split(xs, np.nonzero(np.diff(xs) > step * 2)[0] + 1)
        if i % 2:
            runs = runs[::-1]
        y = (row + step / 2) / scale
        for run in runs:
            a, b = run[0] / scale, (run[-1] + 1) / scale
            n = max(1, int(np.ceil((b - a) / longest)))
            for k in range(n):
                x0, x1 = a + (b - a) * k / n, a + (b - a) * (k + 1) / n
                if i % 2:
                    x0, x1 = a + b - x1, a + b - x0
                    x0, x1 = x1, x0
                out.append({"pts": [(round(x0, 1), round(y, 1)), (round(x1, 1), round(y, 1))],
                            "err": 0.0})
    return out


def carve(picture: Path | str, out_dir: Path | str, cfg: dict[str, Any]) -> dict[str, Any]:
    """The relief carved and rendered: {painting, frames, strokes, ...} like brushwork.paint."""
    import progress
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    img = Image.open(picture)
    aspect = img.width / img.height
    scale = 0.5                                      # grid cells per canvas unit (1000 -> 500)
    gw, gh = 500, int(round(500 / aspect))
    progress.stage("plan", "Reading the depth of the picture")
    T = target(Path(picture), (gw, gh), float(cfg.get("detail", 0.03)))
    # Moulding (clay) is carving upside down: clay is added up to the surface, so
    # flipping the heights turns "build up to T" into "cut down to 1 - T". The rough
    # stages then stay below the true surface, as they should for build-up, and the
    # snapshots are flipped back.
    mould = cfg.get("process") == "mould"
    if mould:
        T = 1.0 - T
    C = np.full_like(T, T.max() if mould else 1.0)   # the uncut block, or the flat clay board
    rng = np.random.default_rng(int(cfg.get("seed", 11)))
    heights = out / "heights"
    shutil.rmtree(heights, ignore_errors=True)
    heights.mkdir(parents=True)
    plan: list[dict[str, Any]] = []
    shots: list[tuple[str, float]] = []
    clock = 0.0

    def surface() -> np.ndarray:
        return np.clip(1.0 - C if mould else C, 0, 1)

    def snapshot() -> None:
        p = heights / f"{len(shots):05d}.png"
        Image.fromarray((surface() * 65535).astype(np.uint16)).save(p)
        shots.append((str(p), clock))
    snapshot()
    # ~150 views of ~35,000 cuts: Blender's renders are most of the time, and the
    # playback reveals each cut between views anyway
    every = int(cfg.get("snapshot_every", 240))
    tools = cfg.get("tools") or (MOULD_TOOLS if mould else TOOLS)
    progress.stage("plan", "Carving", total=len(tools))
    full = T
    for n, (name, r, margin, budget, per_unit) in enumerate(tools):
        T = shaped(full, float(r) * (1.0 if n < len(tools) - 1 else 0.0), scale)
        clock += 8.0                                 # picking up the next tool
        # the roughing gouge takes deep, rough bites; every later tool cuts clean
        # to its own (smoothed) surface, so each stage leaves a smooth result and
        # the detail tool only works where detail remains. (Partial bites left
        # scallops over the whole block, background and all.)
        bite = 0.6 if n == 0 else 1.0
        # Rounds until this tool's stage is done: one pass left the block standing
        # ~0.05 above the target on average, the background never cleared.
        strokes = []
        # Rounds until the tool's stage is really done: < 6% of the block left
        # standing 0.02 above it. (At three rounds the small tools left 25-35%,
        # and the finished relief was pitted all over.)
        for _ in range(int(cfg.get("rounds", 8))):
            if np.mean(C - (T + float(margin)) > 0.02) < 0.06:
                break
            batch = plan_tool(C, T, float(r), float(margin), int(budget), scale, rng)
            for st in batch:
                cut(C, T, st["pts"], float(r), float(margin), bite, scale)
            strokes += batch
        C[:] = 1.0                                   # replay the cuts in order for the snapshots
        targets: dict[float, np.ndarray] = {}
        for prev in plan:
            if prev["shape_r"] not in targets:
                targets[prev["shape_r"]] = shaped(full, prev["shape_r"], scale)
            cut(C, targets[prev["shape_r"]], prev["pts"], prev["r"], prev["margin"], prev["bite"], scale)
        for k, st in enumerate(strokes):
            length = sum(np.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(st["pts"], st["pts"][1:]))
            t0 = clock
            cut(C, T, st["pts"], float(r), float(margin), bite, scale)
            clock += 0.4 + length * per_unit * 10
            x0, y0 = st["pts"][0]
            plan.append({"pts": st["pts"], "t0": t0, "t1": clock, "width": float(r) * 1.4,
                         "r": float(r), "margin": float(margin), "bite": bite,
                         "shape_r": float(r) * (1.0 if n < len(tools) - 1 else 0.0),
                         "layer": n, "brush": name, "rgb": [0.55, 0.38, 0.22],
                         "subject": bool(abs(float(mould) - full[min(gh - 1, int(y0 * scale)),
                                                      min(gw - 1, int(x0 * scale))]) > 0.35)})
            if (k + 1) % every == 0:
                snapshot()
        snapshot()
        LOG.info("%s: %d strokes", name, len(strokes))
        progress.step(n + 1, detail=f"{name}: {len(strokes):,} cuts")
    # the finishing chisel: down to the true surface everywhere it isn't yet
    r_fin = float(cfg.get("finish_radius", 3.0))
    clock += 8.0
    finishing = plan_finish(C, full, r_fin, scale)
    for k, st in enumerate(finishing):
        t0 = clock
        cut(C, full, st["pts"], r_fin, 0.0, 1.0, scale)
        length = abs(st["pts"][1][0] - st["pts"][0][0])
        clock += 0.3 + length * 0.15
        x0, y0 = st["pts"][0]
        plan.append({"pts": st["pts"], "t0": t0, "t1": clock, "width": r_fin * 1.4,
                     "r": r_fin, "margin": 0.0, "bite": 1.0, "shape_r": 0.0,
                     "layer": len(tools), "brush": "smoothing" if mould else "finishing chisel",
                     "rgb": [0.55, 0.38, 0.22],
                     "subject": bool(abs(float(mould) - full[min(gh - 1, int(y0 * scale)),
                                                      min(gw - 1, int(x0 * scale))]) > 0.35)})
        if (k + 1) % every == 0:
            snapshot()
    snapshot()
    LOG.info("finishing chisel: %d passes; left above the surface (>0.006): %.1f%%",
             len(finishing), 100 * np.mean(C - full > 0.006))
    final = heights / "final.png"
    Image.fromarray((surface() * 65535).astype(np.uint16)).save(final)

    progress.stage("paint", f"Rendering {len(shots)} views of the block in Blender")
    frames = out / "frames"
    shutil.rmtree(frames, ignore_errors=True)
    size = cfg.get("frame_size", [1440, 2560])
    job = {"heights": [s for s, _ in shots], "out_dir": str(frames), "size": size,
           "material": cfg.get("material", "wood"), "depth": float(cfg.get("relief", 0.18))}
    (out / "blender-job.json").write_text(json.dumps(job))
    blender = subprocess.Popen([str(BLENDER), "-b", "--factory-startup", "-P", str(SCENE), "--",
                                str(out / "blender-job.json")],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in blender.stdout:
        if line.startswith("RENDERED"):
            done, total = line.split()[1].split("/")
            progress.fraction(int(done) / int(total), f"View {done} of {total}")
    if blender.wait() != 0:
        raise RuntimeError("Blender failed to render the relief")
    rendered = sorted(frames.glob("*.png"))
    if len(rendered) != len(shots):
        raise RuntimeError(f"Blender rendered {len(rendered)} of {len(shots)} views")
    (frames / "frames.tsv").write_text("file\thand_secs\tticks\tchunks_done\tkind\n" + "".join(
        f"{p.name}\t{t:.1f}\t0\t0\ttick\n" for p, (_, t) in zip(rendered, shots)))
    painting = out / "painting.png"
    shutil.copy(rendered[-1], painting)
    shutil.rmtree(heights, ignore_errors=True)
    record = {"aspect": aspect, "height_units": 1000 / aspect, "strokes": plan, "dips": [],
              "style": cfg.get("style", ""), "medium": "moulding" if mould else "carving", "grade": None,
              "playback": cfg.get("playback") or {}}
    (out / "strokes.json").write_text(json.dumps(record))
    progress.set_preview(painting)
    return {"painting": painting, "frames": frames, "strokes": out / "strokes.json"}
