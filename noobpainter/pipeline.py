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
    "playback": True,           # False: just the finished picture, no video (painted styles only)
    "music": "library",         # handplay.choose_music: "library", "none" or a file of one's own
    # The YouTube Short's games: the "Guess what I'm painting" teaser, "Did you guess it?"
    # and the odd painting done upside down. The app turns them off: there it's your picture.
    "shorts": True,
}

# Styles that are a video by nature: the moving paintings, and the ink pages (the comic
# panel ends on a blur), so they have no "just the picture" version.
VIDEO_ONLY = ("anim_van_gogh", "anim_oil_morph", "comic_halftone", "bizarre_manga")

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
    Returns {"video", "painting", "title", "description"}; with painting.playback off,
    {"image", "title"}: the finished picture only, no video."""
    import brushwork
    import handplay
    import progress
    config = config or {}
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    playback = bool(settings(config)["playback"])
    if not playback and (style in VIDEO_ONLY or video):
        raise ValueError(f"{style.replace('_', ' ')} is only made as a video")
    if style in ("comic_halftone", "bizarre_manga"):
        return _ink_page(idea, style, out, config, picture)
    if style.startswith("anim_") or video:
        import paintanim
        kind = style.removeprefix("anim_") if style.startswith("anim_") else "van_gogh"
        res = paintanim.make(idea, kind, config, "", out, Path(picture) if picture else None,
                             Path(video) if video else None)
        return {**res, "image": last_frame(Path(res["video"]), out / "painting.png")}
    cfg = settings(config)
    seed = int(hashlib.sha1(f"{idea}|{style}".encode()).hexdigest()[:8], 16)
    owns = not progress.active()
    if owns:
        progress.start("", f"Painting: {idea or picture}")
    try:
        ref = out / "reference.png"
        if picture and not ref.exists():
            from PIL import Image, ImageOps
            img = Image.open(picture).convert("RGB")
            size = (int(cfg["image_width"]) * 2, int(cfg["image_height"]) * 2)
            if playback:                                # any shape in; the Short is 9:16
                ImageOps.fit(img, size, Image.LANCZOS).save(ref)
            else:                                       # a picture keeps its own shape
                img.thumbnail((max(size),) * 2, Image.LANCZOS)
                img.save(ref)
        progress.stage("reference", f"{style}: {idea[:100]}")
        reference(idea, style, config, ref, seed)
        painted = out / "painted"
        if not ((painted / "strokes.json").exists() and any((painted / "frames").glob("*.png"))):
            bcfg = {**(config.get("brushwork") or {}), "style": style}
            if not playback:   # a few small frames are enough for the progress bar and preview
                bcfg.update(max_frames=40, frame_width=720)
            brushwork.paint(ref, painted, {**config, "brushwork": bcfg})
        if not playback:
            return _picture_only(painted, out, style, owns)
        progress.stage("assemble", ("The teaser, the hand" if cfg["shorts"] else "The hand")
                       + " on every stroke, and the music")
        music = handplay.choose_music(float(cfg["seconds"]), seed, config)
        every = int(cfg["upside_down_every"]) if cfg["shorts"] else 0
        flip = bool(every) and seed % every == 0
        games = {} if cfg["shorts"] else {"teaser": 0, "ending_text": ""}
        play = {**config, "handplay": {**games, **(config.get("handplay") or {}), "upside_down": flip}}
        video_out = out / "painting.mp4"
        handplay.render(painted / "frames", painted / "strokes.json", video_out, float(cfg["seconds"]),
                        (int(cfg["width"]), int(cfg["height"])), music, config=play)
        title = title_for(style, seed)
        desc = describe(idea, style, music)
        (out / "credits.txt").write_text(f"{title}\n\n{desc}\n")
        image = _still(painted, out)
        if not cfg["keep_frames"]:
            shutil.rmtree(painted / "frames", ignore_errors=True)
        if owns:
            progress.finish(title)
        return {"video": video_out, "image": image, "painting": painted / "painting.png", "title": title,
                "description": desc, "upside_down": flip}
    except Exception as e:
        if owns:
            progress.fail(str(e))
        raise


def _still(painted: Path, out: Path) -> Path:
    """The finished painting as a picture, graded as the video's last frame is."""
    import brushwork
    from PIL import Image
    grade = json.loads((painted / "strokes.json").read_text()).get("grade")
    image = out / "painting.png"
    brushwork.apply_grade(Image.open(painted / "painting.png").convert("RGB"), grade).save(image)
    return image


def last_frame(video: Path, image: Path) -> Path | None:
    """A video's last frame as a picture (the moving and manga styles end on the finished piece)."""
    import subprocess
    res = subprocess.run(["ffmpeg", "-v", "error", "-y", "-sseof", "-0.5", "-i", str(video),
                          "-update", "1", "-q:v", "1", str(image)], capture_output=True)
    return image if res.returncode == 0 and image.exists() else None


def _picture_only(painted: Path, out: Path, style: str, owns: bool) -> dict[str, Any]:
    """Just the finished painting, no video."""
    import progress
    image = _still(painted, out)
    shutil.rmtree(painted / "frames", ignore_errors=True)
    title = f"{style.replace('_', ' ').title()} style"
    if owns:
        progress.finish(title)
    return {"image": image, "painting": painted / "painting.png", "title": title}


def _ink_page(idea: str, style: str, out: Path, config: dict[str, Any],
              picture: Path | str | None) -> dict[str, Any]:
    """comic_halftone (comicpanel.py) and bizarre_manga (manga.py): drawn with OpenCV,
    not the oil simulator, because they are crisp ink, dots and flat colour."""
    import comicpanel
    import handplay
    import localimage
    import manga
    import progress
    is_manga = style == "bizarre_manga"
    seed = int(hashlib.sha1(f"{idea}|{style}".encode()).hexdigest()[:8], 16)
    owns = not progress.active()
    if owns:
        progress.start("", f"{style}: {idea or picture}")
    try:
        ref = out / "reference.png"
        if picture and not ref.exists():
            shutil.copy(picture, ref)
        if not ref.exists():
            progress.stage("reference", idea[:100])
            lcfg = {"longform": {"local_image": {**(config.get("local_image") or {}),
                    "style": manga.PROMPT if is_manga else comicpanel.PROMPT,
                    "negative": "text, speech bubble, watermark, photo, logo",
                    "width": 576, "height": 1024}}}
            try:
                localimage.generate(idea, ref, lcfg, seed=seed)
            finally:
                localimage.release(localimage.settings(lcfg))
        progress.stage("assemble", "Inking" if is_manga else "Ink, screentone and the push in")
        if is_manga:
            secs = sum(float(v) for v in {**manga.DEFAULTS, **(config.get("manga") or {})}["timing"])
            music = handplay.choose_music(secs, seed, config)
            hand = bool((config.get("handplay") or {}).get("hand_overlay"))   # --hand / the app's tick box
            mcfg = {**config, "manga": {**(config.get("manga") or {}), "hand_overlay": hand}}
            video = manga.render(ref, out / "manga.mp4", mcfg, music, seed)
            title = "Watch this manga page come to life 🖋️"
            made = ("A blue pencil rough, brush-pen ink, hatching and solid blacks, then flat colour "
                    "under the ink")
        else:
            secs = float({**comicpanel.DEFAULTS, **(config.get("comicpanel") or {})}["seconds"])
            music = handplay.choose_music(secs, seed, config)
            video = comicpanel.render(ref, out / "comic.mp4", config, music)
            title = "Comic panel 🖤"
            made = "A comic panel in ink and screentone"
        desc = f"{made}, made by a computer simulation, not a human artist ({style.replace('_', ' ')} style)."
        if music:
            m = next((m for m in json.loads((handplay.MUSIC / "manifest.json").read_text())
                      if m["file"] == music.name), None)
            if m:
                desc += (f"\n\nMusic: {m['title'].removeprefix('File:').rsplit('.', 1)[0]} "
                         f"({m['licence']}, via Wikimedia Commons / Musopen).")
        (out / "credits.txt").write_text(f"{title}\n\n{desc}\n")
        if owns:
            progress.finish(title)
        # the manga page ends finished; the comic panel ends on a blur, so it has no picture
        image = last_frame(Path(video), out / "painting.png") if is_manga else None
        return {"video": video, "image": image, "title": title, "description": desc}
    except Exception as e:
        if owns:
            progress.fail(str(e))
        raise
