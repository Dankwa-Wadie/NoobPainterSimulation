"""Oil painting: a picture repainted in simulated oils, with a playback of it
being painted from the bare canvas.

The paint is aliceisjustplaying/claude-paint (MIT, vendor/claude-paint): a
physical oil-paint engine (bristles, wet paint, drying, Kubelka-Munk mixing)
driven by Lua. Nothing here needs a language model: a reference picture (a
local Qwen one, see sketchbook.py) is planned into Lua the way a painter
works, then the easel replays it headless and writes a frame every few
seconds of the painter's hand time.

  1. The picture is reduced to a few colour regions (k-means), coarse for the
     block-in and finer for the body of the painting.
  2. Each region's colour is mixed from the tube box: the nearest of a table
     of swatches the easel itself painted (`calibrate`, cached in
     assets/oilpaint/swatches.json), so the mix is what the paint really does.
  3. The region map rides in the Lua as a string; masks read it.
  4. Passes, darks to lights within each: a chalk tonal sketch, a thin broad
     block-in, the body in fuller paint, then small strokes along the edges.
     Each pass is cut into tiles so the playback advances stroke by stroke
     (the easel takes a frame per operation, and per 15 minutes inside one).

`paint(picture, out_dir)` returns the finished painting and the frames;
`playback(...)` cuts the frames into a clip.
"""
from __future__ import annotations

import json
import logging
import subprocess
import os
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

LOG = logging.getLogger("mken.oilpaint")

ROOT = Path(__file__).resolve().parent.parent          # the repo root (assets/, vendor/)
VENDOR = ROOT / "vendor" / "claude-paint"
EASEL = VENDOR / "target" / "release" / ("easel.exe" if os.name == "nt" else "easel")
SWATCHES = ROOT / "assets" / "oilpaint" / "swatches.json"

TUBES = ["lead white", "smalt", "pale smalt", "yellow ochre", "red earth", "vermilion",
         "raw umber", "bone black", "cobalt blue", "chrome yellow", "Prussian blue",
         "green earth", "Rinmann's green", "copper green"]
CHROMA = [t for t in TUBES if t != "lead white"]
# A modern palette from the easel's "every" box (built with --features
# all-boxes): cadmiums, alizarin, ultramarine, cerulean, viridian and friends,
# for colour the 1820s tube box can't reach.
MODERN = ["zinc white", "pale cadmium", "cadmium yellow", "deep cadmium", "cadmium red",
          "permanent alizarin", "magenta", "cobalt violet", "ultramarine blue", "cerulean blue",
          "cobalt blue", "Prussian blue", "viridian", "emerald green", "yellow ochre",
          "burnt sienna", "raw umber", "Mars red", "vine black"]
# Each palette: the easel box it needs, its white and its colours.
PALETTES: dict[str, dict[str, Any]] = {
    "classic": {"box": None, "white": "lead white", "tubes": CHROMA},
    "modern": {"box": "every", "white": "lead white", "tubes": MODERN},
    # Black and white: greys mixed from real blacks and whites, a little umber
    # for a warm shadow, so a monochrome painting is painted, not desaturated.
    "mono": {"box": "every", "white": "zinc white",
             "tubes": ["vine black", "bone black", "raw umber", "lead white"],
             "tints": [0.25, 0.5, 1, 1.5, 2, 3, 4, 6, 8, 12]},   # an even grey scale
}

DEFAULTS: dict[str, Any] = {
    "width": 1000,            # px the easel paints at (2400 is its full size; slower)
    "frame_width": 1080,      # px of the playback frames
    "frames_every": 6.0,      # s of hand time between playback frames
    "block_colours": 6,       # regions in the block-in
    "body_colours": 12,       # regions in the body of the painting
    "grid": 320,              # region-map cells across (the Lua lookup table)
    "block_tile": 250,        # canvas units per tile (the canvas is 1000 wide)
    "body_tile": 125,
    "seed": 7,
}


def settings(config: dict[str, Any] | None = None) -> dict[str, Any]:
    return {**DEFAULTS, **((config or {}).get("oilpaint") or {})}


def installed() -> bool:
    return EASEL.exists()


def build() -> None:
    """Build the easel (needs Rust; the repo's .cargo/config.toml fixes Lua's hash seed)."""
    cargo = Path.home() / ".cargo" / "bin" / "cargo"
    subprocess.run([str(cargo if cargo.exists() else "cargo"), "build", "--release", "-p", "easel"],
                   cwd=VENDOR, check=True)


# --- colour ---------------------------------------------------------------

def _lab(rgb: np.ndarray) -> np.ndarray:
    """sRGB in [0, 1] (..., 3) -> CIELAB (D65)."""
    c = np.where(rgb > 0.04045, ((rgb + 0.055) / 1.055) ** 2.4, rgb / 12.92)
    xyz = c @ np.array([[0.4124, 0.2126, 0.0193], [0.3576, 0.7152, 0.1192],
                        [0.1805, 0.0722, 0.9505]])
    xyz = xyz / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 216 / 24389, np.cbrt(xyz), (24389 / 27 * xyz + 16) / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]),
                     200 * (f[..., 1] - f[..., 2])], axis=-1)


def candidate_mixes(palette: str = "classic") -> list[list[tuple[str, float]]]:
    """Recipes the calibration paints: tubes, tints, pairs and tinted pairs."""
    white, tubes = PALETTES[palette]["white"], PALETTES[palette]["tubes"]
    out: list[list[tuple[str, float]]] = [[(white, 1)]]
    for t in tubes:
        out += [[(t, 1)]] + [[(t, 1), (white, w)] for w in PALETTES[palette].get("tints", (1, 4, 12))]
    for i, a in enumerate(tubes):
        for b in tubes[i + 1:]:
            out += [[(a, 1), (b, 1)], [(a, 1), (b, 1), (white, 3)]]
    return out


def head(palette: str = "classic") -> list[str]:
    """The script's head lines: the engine, and the box when not the default."""
    box = PALETTES[palette]["box"]
    return ["--@ engine 5"] + ([f"--@ box {box}"] if box else [])


def swatch_path(palette: str = "classic") -> Path:
    return SWATCHES if palette == "classic" else SWATCHES.with_name(f"swatches-{palette}.json")


def _recipe_lua(recipe: list[tuple[str, float]], extra: str = "") -> str:
    parts = ", ".join(f'{{"{t}", {p:g}}}' for t, p in recipe)
    return f"pile{{{parts}{extra}}}"


CANVAS = ('canvas{{size=400, aspect={aspect}, linen=18, seed={seed}, '
          'ground={{{{pile={{{{"lead white", 1}}}}, um=80, apply="knife", texture=0.25}}}}}}')


def calibrate(palette: str = "classic") -> list[dict[str, Any]]:
    """Paint every candidate mix as an opaque swatch and record its colour."""
    import tempfile
    path = swatch_path(palette)
    mixes = candidate_mixes(palette)
    cols = int(np.ceil(np.sqrt(mixes.__len__())))
    rows = int(np.ceil(len(mixes) / cols))
    cell = 1000 / cols
    lines = [*head(palette), "--@ chunk", CANVAS.format(aspect=f"{cols}/{rows}", seed=1)]
    for k, recipe in enumerate(mixes):
        x, y = (k % cols) * cell, (k // cols) * cell
        lines += ["--@ chunk",
                  f"work(rect({x + 2:.1f}, {y + 2:.1f}, {cell - 4:.1f}, {cell - 4:.1f}), "
                  f"{{hand=\"body\", pile={_recipe_lua(recipe)}, coverage=4, fill=true, clip=true}})"]
    with tempfile.TemporaryDirectory() as d:
        lua, png = Path(d) / "swatches.lua", Path(d) / "swatches.png"
        lua.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _run(lua, png, width=1000)
        img = np.asarray(Image.open(png).convert("RGB"), dtype=np.float64) / 255.0
    px = img.shape[1] / 1000
    out = []
    for k, recipe in enumerate(mixes):
        x, y = (k % cols) * cell, (k // cols) * cell
        a, b = int((x + cell * 0.3) * px), int((x + cell * 0.7) * px)
        c, d = int((y + cell * 0.3) * px), int((y + cell * 0.7) * px)
        rgb = np.median(img[c:d, a:b].reshape(-1, 3), axis=0)
        out.append({"recipe": recipe, "rgb": [round(float(v), 4) for v in rgb]})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=0), encoding="utf-8")
    LOG.info("calibrated %d swatches -> %s", len(out), path)
    return out


def swatches(palette: str = "classic") -> list[dict[str, Any]]:
    path = swatch_path(palette)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else calibrate(palette)


def match(rgb: np.ndarray, table: list[dict[str, Any]]) -> list[tuple[str, float]]:
    """The swatch recipe nearest a colour (CIELAB distance)."""
    labs = _lab(np.array([s["rgb"] for s in table]))
    k = int(np.argmin(np.sum((labs - _lab(np.asarray(rgb))) ** 2, axis=1)))
    return [tuple(p) for p in table[k]["recipe"]]


# --- planning -------------------------------------------------------------

def kmeans(pixels: np.ndarray, k: int, seed: int = 0, iters: int = 20
           ) -> tuple[np.ndarray, np.ndarray]:
    """Labels and centres of `pixels` (N, 3), clustered in CIELAB."""
    lab = _lab(pixels)
    rng = np.random.default_rng(seed)
    centres = lab[rng.choice(len(lab), k, replace=False)]
    for _ in range(iters):
        labels = np.argmin(((lab[:, None] - centres[None]) ** 2).sum(-1), axis=1)
        for j in range(k):
            if np.any(labels == j):
                centres[j] = lab[labels == j].mean(0)
    rgb = np.array([pixels[labels == j].mean(0) if np.any(labels == j) else np.zeros(3)
                    for j in range(k)])
    return labels, rgb


def regions(img: Image.Image, k: int, grid: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """A (gh, grid) label map, regions numbered dark to light, and their colours."""
    gh = int(round(grid * img.height / img.width))
    small = np.asarray(img.resize((grid, gh), Image.LANCZOS), dtype=np.float64) / 255.0
    labels, rgb = kmeans(small.reshape(-1, 3), k, seed)
    order = np.argsort(_lab(rgb)[:, 0])                     # darks first, as painters lay in
    rank = np.empty(k, dtype=int)
    rank[order] = np.arange(k)
    return rank[labels].reshape(gh, grid), rgb[order]


def _labels_lua(name: str, labels: np.ndarray) -> str:
    """The map as a Lua string, one letter per cell (A = darkest)."""
    text = "".join(chr(65 + int(v)) for v in labels.ravel())
    return f'{name} = "{text}"'


def flow(img: Image.Image, cells: int = 48) -> np.ndarray:
    """Stroke direction per cell (radians, canvas convention): along the edges.

    The structure tensor's minor axis, smoothed per cell, is the direction the
    picture's forms run in; a painter strokes along them.
    """
    gh = int(round(cells * img.height / img.width))
    g = np.asarray(img.convert("L").resize((cells * 8, gh * 8), Image.LANCZOS), dtype=np.float64)
    gy, gx = np.gradient(g)
    def pool(a: np.ndarray) -> np.ndarray:
        return a.reshape(gh, 8, cells, 8).mean(axis=(1, 3))
    jxx, jyy, jxy = pool(gx * gx), pool(gy * gy), pool(gx * gy)
    # orientation of the strongest gradient, turned a quarter to run along the edge
    return 0.5 * np.arctan2(2 * jxy, jxx - jyy) + np.pi / 2


def _flow_lua(angles: np.ndarray) -> str:
    gh, gw = angles.shape
    vals = ",".join(f"{a:.2f}" for a in angles.ravel())
    return (f"FLOW = {{{vals}}}\n"
            f"function flow(x, y)\n"
            f"  local cx = math.min({gw - 1}, math.floor(x / W * {gw}))\n"
            f"  local cy = math.min({gh - 1}, math.floor(y / H * {gh}))\n"
            f"  return FLOW[cy * {gw} + cx + 1]\n"
            f"end")


MASK_FN = """
function region(map, gw, gh, letter)
  local code = string.byte(letter)
  return mask(function(x, y)
    local cx = math.min(gw - 1, math.floor(x / W * gw))
    local cy = math.min(gh - 1, math.floor(y / H * gh))
    return string.byte(map, cy * gw + cx + 1) == code and 1 or 0
  end):soften(6)
end
"""


def _tiles(labels: np.ndarray, letter_index: int, tile: float, aspect: float
           ) -> list[tuple[float, float]]:
    """Corners (canvas units) of the tiles the region reaches, top to bottom."""
    gh, gw = labels.shape
    height = 1000 / aspect
    out = []
    for ty in np.arange(0, height, tile):
        for tx in np.arange(0, 1000, tile):
            a, b = int(tx / 1000 * gw), int(min(gw, np.ceil((tx + tile) / 1000 * gw)))
            c, d = int(ty / height * gh), int(min(gh, np.ceil((ty + tile) / height * gh)))
            if np.mean(labels[c:d, a:b] == letter_index) > 0.02:
                out.append((float(tx), float(ty)))
    return out


def plan(picture: Path, cfg: dict[str, Any], table: list[dict[str, Any]]) -> str:
    """The Lua that paints `picture`, chunk by chunk."""
    img = Image.open(picture).convert("RGB")
    aspect = img.width / img.height
    seed = int(cfg["seed"])
    grid = int(cfg["grid"])
    block, block_rgb = regions(img, int(cfg["block_colours"]), grid, seed)
    body, body_rgb = regions(img, int(cfg["body_colours"]), grid, seed + 1)
    gh = block.shape[0]
    out = ["--@ engine 5", "--@ chunk", CANVAS.format(aspect=f"{img.width}/{img.height}", seed=seed),
           "--@ chunk", MASK_FN.strip(), _labels_lua("BLOCK", block), _labels_lua("BODY", body),
           _flow_lua(flow(img))]

    def chunk(*lines: str) -> None:
        out.append("--@ chunk")
        out.extend(lines)

    # 1. A tonal sketch in chalk: hatching over the two darkest block regions.
    chunk('c = chalk()')
    for j in range(min(2, len(block_rgb))):
        for tx, ty in _tiles(block, j, float(cfg["block_tile"]), aspect):
            chunk(f'c:hatch(region(BLOCK, {grid}, {gh}, "{chr(65 + j)}") * '
                  f'rect({tx}, {ty}, {cfg["block_tile"]}, {cfg["block_tile"]}), '
                  f'{{angle=-1.0, pressure={0.35 - 0.1 * j:.2f}}})')

    # 2. Block-in: thin, broad, darks to lights.
    for j, rgb in enumerate(block_rgb):
        letter, tile = chr(65 + j), float(cfg["block_tile"])
        chunk(f'm = region(BLOCK, {grid}, {gh}, "{letter}")',
              f'p = {_recipe_lua(match(rgb, table), ", turps=0.5")}')
        for tx, ty in _tiles(block, j, tile, aspect):
            chunk(f'work(m * rect({tx}, {ty}, {tile}, {tile}), '
                  f'{{hand="broad", pile=p, coverage=1.1, fill=true, edge="soft"}})')

    # 3. Body: fuller paint in the finer regions.
    for j, rgb in enumerate(body_rgb):
        letter, tile = chr(65 + j), float(cfg["body_tile"])
        chunk(f'm = region(BODY, {grid}, {gh}, "{letter}")', f'p = {_recipe_lua(match(rgb, table))}')
        for tx, ty in _tiles(body, j, tile, aspect):
            chunk(f'work(m * rect({tx}, {ty}, {tile}, {tile}), '
                  f'{{hand="body", pile=p, coverage=1.6, fill=true, edge="firm", angle=flow}})')

    # 4. Detail: small strokes along every region's edge, restating the drawing.
    for j, rgb in enumerate(body_rgb):
        letter, tile = chr(65 + j), float(cfg["block_tile"])
        chunk(f'm = region(BODY, {grid}, {gh}, "{letter}")',
              f'e = m * m:rim(6, 2)', f'p = {_recipe_lua(match(rgb, table))}')
        for tx, ty in _tiles(body, j, tile, aspect):
            chunk(f'work(e * rect({tx}, {ty}, {tile}, {tile}), '
                  f'{{hand="detail", pile=p, coverage=1.2, angle=flow}})')
    return "\n".join(out) + "\n"


# --- running --------------------------------------------------------------

def chunks_total(lua: Path) -> int:
    """How many chunks of strokes the plan holds: the painting is done after the last."""
    try:
        return sum(1 for ln in lua.read_text(encoding="utf-8").splitlines() if "chunk" in ln)
    except OSError:
        return 0


def chunks_done(frames: Path) -> int:
    """How many chunks the easel has finished, from the log it writes beside its frames."""
    try:
        rows = (frames / "frames.tsv").read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    for row in reversed(rows[1:]):
        cols = row.split("\t")
        if len(cols) >= 4 and cols[3].isdigit():
            return int(cols[3])
    return 0


def _run(lua: Path, png: Path, width: int, frames: Path | None = None,
         every: float = 6.0, frame_width: int = 1080, timeout: int = 3600,
         on_progress: Any = None) -> None:
    """
    Run the easel. With `on_progress`, it is called every few seconds with
    (chunks done, chunks in the plan) while the painting goes on.
    """
    import time
    cmd = [str(EASEL), "run", str(lua), "--out", str(png), "--width", str(width)]
    if frames:
        cmd += ["--frames-every", f"{every:g}", "--frames-dir", str(frames),
                "--frame-width", str(frame_width)]
    if not (on_progress and frames):
        res = subprocess.run(cmd, cwd=VENDOR, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        out, err, code = res.stdout, res.stderr, res.returncode
    else:
        total, end = chunks_total(lua), time.time() + timeout
        proc = subprocess.Popen(cmd, cwd=VENDOR, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace")
        import threading
        got: dict[str, str] = {}
        reader = threading.Thread(target=lambda: got.update(zip(("out", "err"), proc.communicate())))
        reader.start()
        while reader.is_alive():
            if time.time() > end:
                proc.kill()
                reader.join()
                raise RuntimeError(f"easel took longer than {timeout} s")
            try:
                on_progress(chunks_done(frames), total)
            except Exception:                           # noqa: BLE001 (progress is best-effort)
                pass
            reader.join(3)
        out, err, code = got.get("out", ""), got.get("err", ""), proc.returncode
    if code != 0 or "failed" in out + err:
        raise RuntimeError(f"easel failed:\n{(out + err)[-2000:]}")


def paint(picture: Path | str, out_dir: Path | str, config: dict[str, Any] | None = None,
          on_progress: Any = None) -> dict[str, Any]:
    """Repaint `picture` in oils; returns {"painting", "frames", "lua"}."""
    cfg = settings(config)
    if not installed():
        raise RuntimeError(f"the easel isn't built: run oilpaint.build() ({EASEL})")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    lua = out_dir / "painting.lua"
    lua.write_text(plan(Path(picture), cfg, swatches()), encoding="utf-8")
    frames = out_dir / "frames"
    if frames.exists():
        for f in frames.iterdir():
            f.unlink()
    png = out_dir / "painting.png"
    _run(lua, png, int(cfg["width"]), frames, float(cfg["frames_every"]), int(cfg["frame_width"]),
         on_progress=on_progress)
    return {"painting": png, "frames": frames, "lua": lua}


def playback(frames: Path, out: Path, seconds: float = 20.0, hold: float = 2.0,
             fps: int = 30, size: tuple[int, int] | None = None) -> Path:
    """The frames as a clip of `seconds`, ending on the finished painting held for `hold`."""
    shots = sorted(frames.glob("*.png"))
    if not shots:
        raise RuntimeError(f"no frames in {frames}")
    step = max(1e-3, seconds / len(shots))
    listing = frames / "playback.ffconcat"
    body = ["ffconcat version 1.0"]
    for s in shots:
        body += [f"file '{s.name}'", f"duration {step:.4f}"]
    body += [f"file '{shots[-1].name}'", f"duration {hold:.3f}", f"file '{shots[-1].name}'"]
    listing.write_text("\n".join(body) + "\n", encoding="utf-8")
    vf = f"fps={fps},format=yuv420p"
    if size:
        w, h = size
        vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}," + vf
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(listing), "-vf", vf, "-c:v", "libx264", "-crf", "18", str(out)],
                   check=True)
    return out


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Repaint a picture in oils, with a playback.")
    ap.add_argument("picture", nargs="?")
    ap.add_argument("out_dir", nargs="?")
    ap.add_argument("--calibrate", metavar="PALETTE", nargs="?", const="classic",
                    help="repaint a swatch table (classic or modern)")
    ap.add_argument("--seconds", type=float, default=20.0, help="playback length")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if a.calibrate:
        calibrate(a.calibrate)
    if a.picture:
        res = paint(a.picture, a.out_dir or "oilpaint-out")
        clip = playback(res["frames"], Path(res["painting"]).with_name("playback.mp4"), a.seconds)
        print(res["painting"], clip)


if __name__ == "__main__":
    main()
