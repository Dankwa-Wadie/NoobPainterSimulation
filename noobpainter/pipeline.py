"""The pipeline: a picture (or an idea) in, a painted video out.

    picture or idea -> reference picture -> painted stroke by stroke (brushwork.py),
    or carved / moulded (carving.py) -> the video with the hand, teaser, reveal and
    music (handplay.py) -> saved with its title and credits.

Moving paintings (anim_* styles, or a source video) go through paintanim.py.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any

LOG = logging.getLogger("noobpainter")

DEFAULTS: dict[str, Any] = {
    "seconds": 45.0,
    "width": 1080, "height": 1920,
    "image_width": 576, "image_height": 1024,
    "keep_frames": False,       # the frames are a few GB a painting; the video keeps the story
    "upside_down_every": 4,     # one painting in this many is painted upside down (0: never)
}

TITLES = {  # by medium: a carving called "the painting" read wrong
    "oil": ["Guess what I'm painting 🎨", "Can you tell what this is yet? 🎨",
            "What am I painting? Guess before the end 🎨", "Guess the painting 🖌️"],
    "pencil": ["Guess what I'm drawing ✏️", "Can you tell what this is yet? ✏️",
               "What am I drawing? Guess before the end ✏️"],
    "compass": ["Guess what I'm drawing 🌀", "Can you tell what this spiral is yet? 🌀",
                "What am I drawing? Guess before the end 🌀"],
    "pen": ["Guess what I'm drawing 🖊️", "Can you tell what this is yet? 🖊️",
            "What am I drawing? Guess before the end 🖊️"],
    "carving": ["Guess what I'm carving 🪵", "Can you tell what this is yet? 🪵",
                "What am I carving? Guess before the end 🪵"],
    "moulding": ["Guess what I'm sculpting 🏺", "Can you tell what this is yet? 🏺",
                 "What am I sculpting? Guess before the end 🏺"],
}


def title_for(style: str, seed: int) -> str:
    import brushwork
    st = brushwork.settings({"brushwork": {"style": style}})
    medium = "moulding" if st.get("process") == "mould" else st.get("medium", "oil")
    options = TITLES.get(medium, TITLES["oil"])
    return options[seed % len(options)]


def settings(config: dict[str, Any]) -> dict[str, Any]:
    return {**DEFAULTS, **(config.get("painting") or {})}


def reference(idea: str, style: str, config: dict[str, Any], out: Path, seed: int) -> Path:
    """The picture to paint, drawn by a local image model (optional: localimage.py, ComfyUI)."""
    import brushwork
    import localimage
    if out.exists() and out.stat().st_size > 0:
        return out
    cfg = settings(config)
    look = brushwork.settings({"brushwork": {"style": style}})["prompt"]
    lcfg = {"longform": {"local_image": {**(config.get("local_image") or {}), "style": look,
            "negative": "text, watermark, signature, picture frame, blurry, photo",
            "width": int(cfg["image_width"]), "height": int(cfg["image_height"])}}}
    try:
        localimage.generate(idea, out, lcfg, seed=seed)
    finally:
        localimage.release(localimage.settings(lcfg))
    return out


def describe(idea: str, style: str, music: Path | None) -> str:
    """The description: what it is, how it was made, and the music's licence."""
    import handplay
    credit = ""
    if music:
        manifest = json.loads((handplay.MUSIC / "manifest.json").read_text())
        m = next((m for m in manifest if m["file"] == music.name), None)
        if m:
            credit = (f"\n\nMusic: {m['title'].removeprefix('File:').rsplit('.', 1)[0]} "
                      f"({m['licence']}, via Wikimedia Commons / Musopen).")
    import brushwork
    label = style.replace("_", " ").title()
    st = brushwork.settings({"brushwork": {"style": style}})
    medium = "moulding" if st.get("process") == "mould" else st.get("medium", "oil")
    made, verb, tags = {
        "moulding": ("Moulded by hand from simulated clay, rendered in 3D", "sculpted",
                     "#sculpting #clay"),
        "oil": ("Painted stroke by stroke in simulated oil paint: a computer simulation of "
                "brushes, wet paint and pigments", "painted", "#painting #oilpainting"),
        "pencil": ("Drawn line by line in simulated graphite: a computer simulation of pencils "
                   "on paper", "drawn", "#drawing #pencildrawing"),
        "pen": ("Drawn line by line in simulated ink", "drawn", "#drawing #lineart"),
        "compass": ("Drawn arc by arc with a pencil in a compass, as one spiral, in simulated "
                    "graphite", "drawn", "#drawing #spiralart"),
        "carving": ("Carved cut by cut from a simulated block, rendered in 3D", "carved",
                    "#carving #woodcarving"),
    }.get(medium, ("Made by a computer simulation", "made", "#art"))
    return (f"Guess what's being {verb} before the end! Drop your guess in the comments.\n\n"
            f"{made} ({label} style), not a human artist."
            f"{credit}\n\n{tags} #guesstheart #shorts")


def make(idea: str, style: str, out_dir: Path | str, config: dict[str, Any] | None = None,
         picture: Path | str | None = None, video: Path | str | None = None) -> dict[str, Any]:
    """Paint one picture (`picture`, or drawn from `idea`) in `style` into `out_dir`.
    Returns {"video", "painting", "title", "description"}."""
    import brushwork
    import handplay
    import progress
    config = config or {}
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if style.startswith("anim_") or video:
        import paintanim
        kind = style.removeprefix("anim_") if style.startswith("anim_") else "van_gogh"
        return paintanim.make(idea, kind, config, "", out, Path(picture) if picture else None,
                              Path(video) if video else None)
    cfg = settings(config)
    seed = int(hashlib.sha1(f"{idea}|{style}".encode()).hexdigest()[:8], 16)
    owns = not progress.active()
    if owns:
        progress.start("", f"Painting: {idea or picture}")
    try:
        ref = out / "reference.png"
        if picture and not ref.exists():
            from PIL import Image, ImageOps             # any shape in; the Short is 9:16
            ImageOps.fit(Image.open(picture).convert("RGB"),
                         (int(cfg["image_width"]) * 2, int(cfg["image_height"]) * 2), Image.LANCZOS).save(ref)
        progress.stage("reference", f"{style}: {idea[:100]}")
        reference(idea, style, config, ref, seed)
        painted = out / "painted"
        if not ((painted / "strokes.json").exists() and any((painted / "frames").glob("*.png"))):
            brushwork.paint(ref, painted, {**config, "brushwork": {**(config.get("brushwork") or {}),
                                                                   "style": style}})
        progress.stage("assemble", "The teaser, the hand on every stroke, and the music")
        music = handplay.pick_music(float(cfg["seconds"]), seed)
        every = int(cfg["upside_down_every"])
        flip = bool(every) and seed % every == 0
        play = {**config, "handplay": {**(config.get("handplay") or {}), "upside_down": flip}}
        video_out = out / "painting.mp4"
        handplay.render(painted / "frames", painted / "strokes.json", video_out, float(cfg["seconds"]),
                        (int(cfg["width"]), int(cfg["height"])), music, config=play)
        title = title_for(style, seed)
        desc = describe(idea, style, music)
        (out / "credits.txt").write_text(f"{title}\n\n{desc}\n")
        if not cfg["keep_frames"]:
            shutil.rmtree(painted / "frames", ignore_errors=True)
        if owns:
            progress.finish(title)
        return {"video": video_out, "painting": painted / "painting.png", "title": title,
                "description": desc, "upside_down": flip}
    except Exception as e:
        if owns:
            progress.fail(str(e))
        raise
