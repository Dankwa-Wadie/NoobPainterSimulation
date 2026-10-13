# NoobPainterSimulation

Pictures painted, drawn, carved and animated **stroke by stroke**, by a simulation, and
turned into short vertical videos you can watch being made.

It is not a filter. A real paint simulator mixes paint from named tubes, pushes it
around with bristles, and lets it dry; this project decides, stroke by stroke, where a
painter would put each one, and records the whole thing.

| Oil (Van Gogh style) | Pencil and ruler | Pen maze | Compass spiral |
|:---:|:---:|:---:|:---:|
| ![](docs/media/lighthouse-van-gogh.gif) | ![](docs/media/pencil-ruler-portrait.gif) | ![](docs/media/maze-owl.gif) | ![](docs/media/compass-spiral-owl.gif) |

| Wood carving | Baroque oil | Moving painting |
|:---:|:---:|:---:|
| ![](docs/media/wood-carving-owl.gif) | ![](docs/media/baroque-scholar.gif) | ![](docs/media/van-gogh-animation.gif) |

Full sample videos (with music) are on the [Releases page](../../releases). These
samples were made with the optional hand overlay on (`--hand`).

---

## How it works, in plain words

**1. It starts with a picture.** You give it a photo or picture, or describe an idea and a
local image model draws one first. That picture is the guide, like the photo an artist
pins next to the easel.

**2. The paint is simulated, not faked.** The paint engine
([claude-paint](https://github.com/aliceisjustplaying/claude-paint)) models real oil
paint: a brush is a set of bristles, paint is mixed on a palette from named tubes (lead
white, ochre, cobalt blue…), thick paint holds ridges, thin paint glazes, colours mix the
way pigments do, and paint dries over time.

**3. It paints the way a painter does: big to small.** It compares the canvas with the
picture, then blocks in the big shapes with a big brush, refines with smaller brushes, and
finishes with a fine one. Each time it looks at what it has painted so far and only adds
strokes where the canvas doesn't match yet. Every stroke follows the shape of the thing
it paints. A painting takes a few thousand strokes.

**4. Every stroke is recorded.** Where it went and when. That is how the video can follow
the brush (and, with `--hand`, show a hand moving exactly along each stroke).

**5. It's cut into a short video.** It opens on the finished picture blurred — *"Guess
what I'm painting"* — then a close-up camera follows the brush, so you see strokes but not
the whole picture; the subject is painted last; the camera pulls back for the reveal.
Calm public-domain classical music plays underneath.

### Other materials, same idea
- **Pencil** — simulated pencils of different softness; shading ruled line by line against a ruler, crossing like an engraving.
- **Pen** — every mark one pen line: a maze that forms the picture, op-art waves, one continuous line.
- **Compass spiral** — the picture drawn as one spiral of compass arcs; the line weight carries the picture.
- **Carving and clay** — a 3D block carved away (or clay built up) bit by bit, rendered in Blender under raking light, in wood, marble or terracotta.
- **Moving paintings** — a picture or a video clip repainted frame by frame, about 12 paintings a second, so the brushwork shimmers (the technique of the film *Loving Vincent*).

**The honest part:** there is no human painter and no real canvas. It is a computer
simulation of painting. What makes it more than a filter is that the paint, the brushes
and the order of the work are modelled on how real artists paint.

---

## The maths

**In the paint engine (claude-paint)**
- **Colour mixing — Kubelka–Munk theory.** Pigments absorb some light and scatter some back; the theory predicts what comes out when they are mixed or layered (why blue + yellow paint makes green, and why a thin glaze shows the colour beneath).
- **Bristles, wet paint and drying** — each bristle modelled, paint moving between brush and canvas, layers drying at rates set by pigment and thickness.

**In this project: deciding the strokes**
- **Colour difference the way eyes see it (CIELAB)** — so "how wrong is the canvas here?" matches what a person would notice.
- **Palette by clustering (k-means)** — thousands of stroke colours pooled into a few paint mixes, as a painter mixes a few piles.
- **Paint matching** — a few hundred test swatches painted by the engine and measured; each colour is matched to the nearest real mix.
- **Stroke direction — gradients and the structure tensor** — at each point, the direction brightness changes fastest is across an edge; strokes run at right angles to it, along the form.
- **Stroke placement — error-driven, coarse to fine** — strokes go where the canvas differs most from the picture; each layer uses a smaller brush.
- **Style fitting — SSIM** — a standard image-similarity measure, so tuning a style can't trade away the subject.

**The drawings**
- **Compass spiral** — half-circles alternating between two centres, each a little wider (the draftsman's method); pencil pressure along each arc follows the picture's darkness.
- **Maze — graph theory** — the picture cut into cells (small where dark, large where light); a random spanning tree (Prim's algorithm) joins every cell by exactly one path, which is what makes it a real, solvable maze.
- **Waves and continuous line** — sine waves that wobble faster where the picture is darker.
- **Ruled shading** — parallel lines at one angle per layer, cut where the picture lightens; layers cross as cross-hatching.
- **Risograph** — each colour split into ink layers by least squares, then halftone dots from cosine patterns.

**Carving and motion**
- **Depth** — a model estimates each point's distance from the camera; that becomes the carving's height.
- **Carving** — each cut lowers the surface under a rounded tool profile but never below the target; smoothing sets how much detail each tool can reach.
- **Motion** — parallax (near moves more than far), smooth random fields for swirling skies, optical flow between frames, and exponential easing so the camera glides.

---

## Setup

Needs Python 3.10+, [Rust](https://rustup.rs) (to build the paint engine), ffmpeg, and —
for carving — [Blender](https://www.blender.org).

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
./setup.sh                          # fetches claude-paint, applies patches/, builds it
python tools/fetch_classical.py     # optional: the music (public domain / CC0, licence-checked)
```

Optional:
- **Drawing pictures from an idea** (`--idea`) uses a local [ComfyUI](https://github.com/comfyanonymous/ComfyUI) with Qwen-Image (`noobpainter/localimage.py`); set `NOOBPAINTER_COMFY_HOME` if it isn't in `~/ComfyUI-Installs/ComfyUI`. Without it, pass your own picture.
- **Blender** is found at the macOS default path; set `NOOBPAINTER_BLENDER` otherwise.

## Use

```sh
python -m noobpainter styles                                   # list the styles
python -m noobpainter paint photo.jpg --style van_gogh --out out/my-painting
python -m noobpainter paint photo.jpg --style pencil --hand    # with the hand overlay
python -m noobpainter paint photo.jpg --style relief_wood      # carved (Blender)
python -m noobpainter paint --video clip.mp4 --style anim_van_gogh
```

Each run writes `painting.mp4`, the finished `painting.png` and `credits.txt` (title,
description and the music's licence) into the output folder.

**Styles:** `contemporary`, `impressionist`, `van_gogh`, `old_master`, `baroque`, `pop`,
`caricature`, `bw_exaggerated`, `pencil`, `maze`, `waves`, `continuous`, `compass_spiral`,
`relief_wood`, `relief_marble`, `relief_clay`, `anim_van_gogh`, `anim_oil_morph`.

## Layout

| | |
|---|---|
| `noobpainter/brushwork.py` | plans the strokes: layers, colours, directions, style presets |
| `noobpainter/oilpaint.py` | paint mixing and swatch calibration |
| `noobpainter/handplay.py` | the video: camera, teaser, reveal, compass/ruler, optional hand, music |
| `noobpainter/lineart.py` | maze, waves, continuous line, compass spiral |
| `noobpainter/carving.py`, `relief_scene.py` | carving and clay moulding, rendered in Blender |
| `noobpainter/paintanim.py` | moving paintings, and repainting a clip frame by frame |
| `noobpainter/stylefit.py` | fits a style to example pictures |
| `noobpainter/riso.py` | risograph print effect |
| `noobpainter/pipeline.py` | picture/idea in, video out |
| `noobpainter/wanvideo.py` | **experimental:** local video model (Wan 2.2) for motion — unreliable on a 24 GB Mac; only subtle motion |
| `tools/fetch_classical.py` | downloads calm classical recordings, checks licences and voices |

## Credits and licences

This project is MIT-licensed (see `LICENSE`).

- **Paint engine:** [claude-paint](https://github.com/aliceisjustplaying/claude-paint) by aliceisjustplaying (MIT), fetched by `setup.sh`; this project adds one small read-only clock (`patches/`).
- **Music:** recordings from [Musopen](https://musopen.org) via Wikimedia Commons, public domain or CC0; each video's `credits.txt` names its track.
- **Font:** Montserrat (SIL Open Font License, `assets/fonts/OFL-montserrat.txt`).
- **Hand images** (`assets/hand`) were generated locally with Qwen-Image.
- Every video says it is made by a simulation, not a human artist.
