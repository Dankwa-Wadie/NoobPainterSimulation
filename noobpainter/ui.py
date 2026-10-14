"""A small web app for the painter: pick a picture and a style, press Paint, watch it.

    python -m noobpainter ui            # opens http://127.0.0.1:8770

Local only (it listens on 127.0.0.1), no extra dependencies: Python's own web server
and one page. One painting at a time; each run lands in out/ui/<time>/.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import mimetypes
import threading
import traceback
import urllib.parse
import os
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import brushwork
import progress

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "assets" / "brand"
OUT = ROOT / "out" / "ui"
UPLOADS = OUT / "uploads"
PREVIEWS = ROOT / "docs" / "media" / "styles"
MAX_UPLOAD = 500 * 1024 ** 2

MEDIUM_LABEL = {"oil": "Oil paint", "pencil": "Pencil", "pen": "Pen", "compass": "Compass",
                "carving": "Carving", "animation": "Moving painting"}
NOTES = {"relief_wood": "carved in Blender", "relief_marble": "carved in Blender",
         "relief_clay": "moulded in Blender", "anim_van_gogh": "takes a picture or a clip",
         "anim_oil_morph": "takes a picture or a clip"}

PICTURES = {".png", ".jpg", ".jpeg", ".webp"}
VIDEOS = {".mp4", ".mov", ".m4v", ".webm"}
CLIP_STYLES = ("anim_van_gogh", "anim_oil_morph")      # the styles that can repaint a video clip
MULTI_STYLES = ("anim_oil_morph",)                     # the one style that takes several pictures
BLENDER_STYLES = ("relief_wood", "relief_marble", "relief_clay")
MAX_PICTURES = 8                 # Oil paint (moving): a pose each
AUDIO = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".flac", ".opus"}

_job: dict = {}
_model: dict = {"status": "idle", "log": []}
_music: dict = {"status": "idle", "log": []}


def music_state() -> dict:
    """Whether the optional classical library has been fetched, and any fetch's progress."""
    import handplay
    have = any(handplay.MUSIC.glob("*.m4a")) and (handplay.MUSIC / "manifest.json").exists()
    return {"installed": have, "install": _music.get("status"), "log": _music.get("log", [])[-8:]}


def _fetch_music() -> None:
    """tools/fetch_classical.py, as the installer runs it (about 500 MB with its voice check)."""
    import subprocess
    import sys
    _music.update(status="running", log=[])
    proc = subprocess.Popen([sys.executable, str(ROOT / "tools" / "fetch_classical.py")], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
    for line in proc.stdout:
        line = line.rstrip().split("\r")[-1]
        if line:
            _music["log"] = (_music["log"] + [line])[-40:]
    _music["status"] = "done" if proc.wait() == 0 else "failed"


def model_state() -> dict:
    """Whether the optional local image model is installed, and any install's progress."""
    import localimage
    cfg = localimage.settings(None)
    return {"installed": localimage.installed(cfg), "missing": localimage.missing_files(cfg),
            "install": _model.get("status"), "log": _model.get("log", [])[-12:]}


def _install_model() -> None:
    import subprocess
    _model.update(status="running", log=[])
    if os.name == "nt":
        _model.update(status="failed", log=["The one-click image-model install is for macOS and Linux for now.",
                                            "On Windows, see the README: install ComfyUI and the four model files by hand."])
        return
    proc = subprocess.Popen(["bash", str(ROOT / "tools" / "install_image_model.sh")], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
    for line in proc.stdout:
        line = line.rstrip().split("\r")[-1]
        if line:
            _model["log"] = (_model["log"] + [line])[-40:]
    _model["status"] = "done" if proc.wait() == 0 else "failed"


_blender: dict = {"checked": None}
_blinstall: dict = {"status": "idle", "log": []}


def _install_blender() -> None:
    """The Install Blender button: blendersetup.install, its lines shown on the page."""
    import blendersetup
    _blinstall.update(status="running", log=[])

    def say(line: str) -> None:
        log = _blinstall["log"]
        if log and line.startswith("Downloading") and log[-1].startswith("Downloading"):
            log[-1] = line                       # one progress line that updates, not hundreds
        else:
            log.append(line)
        _blinstall["log"] = log[-12:]
    try:
        blendersetup.install(say)
        _blinstall["status"] = "done"
    except Exception as e:                        # noqa: BLE001 - shown on the page
        say(f"Stopped: {e}")
        _blinstall["status"] = "failed"
        traceback.print_exc()


def blender_state(path: str | None = None) -> dict:
    """Whether Blender (for the carving styles) is connected: found in the usual places
    or where the person pointed, and runs. Looked for afresh, so a later install shows."""
    import carving
    found = carving.find_blender()
    version = carving.blender_version(found) if found else None
    import blendersetup
    try:
        offer = blendersetup.asset()
    except RuntimeError:
        offer = ""                                # no official build for this computer
    return {"found": bool(version), "path": str(found) if found else "", "version": version or "",
            "saved": bool(carving._saved().get("blender")), "install": _blinstall.get("status"),
            "log": _blinstall.get("log", [])[-8:], "offer": f"Blender {blendersetup.VERSION}" if offer else ""}


def connect_blender(path: str) -> dict:
    """Point the app at a Blender the usual places miss (a program, or on macOS its .app)."""
    import carving
    p = Path(path.strip().strip('"').strip("'")).expanduser()
    if p.suffix == ".app":
        p = p / "Contents" / "MacOS" / "Blender"
    if p.is_dir():                                # its folder: the program inside
        p = next((q for q in (p / "blender.exe", p / "blender", p / "Blender") if q.is_file()), p)
    version = carving.blender_version(p)
    if not version:
        return {**blender_state(), "error": f"No Blender that runs at {p}"}
    carving.save_blender(str(p))
    return blender_state()


# What the hand holds, by the style's playback hand (handplay: assets/hand/<name>.json)
HAND_TOOL = {"": "the brush", "pencil": "the pencil", "pen": "the pen",
             "carving": "the carving tool", "mould": "the modelling tool"}


def hand_tool(st: dict) -> str | None:
    """What a hand would hold in this style's video, or None when it has no hand (the
    compass draws on its own)."""
    import handplay
    which = str((st.get("playback") or {}).get("hand") or "")
    if which == "none" or which not in HAND_TOOL:
        return None
    return HAND_TOOL[which] if handplay.HAND.with_name(f"{which or 'hand'}.json").exists() else None


def styles() -> list[dict]:
    rows = []
    for name, st in brushwork.STYLES.items():
        medium = st.get("medium", "oil")
        if st.get("process") == "mould":
            medium = "carving"
        rows.append({"name": name, "medium": MEDIUM_LABEL.get(medium, medium), "video_only": False,
                     "clip": False, "multi": False, "blender": name in BLENDER_STYLES,
                     "hand": hand_tool(st),
                     "note": NOTES.get(name, ""), "preview": (PREVIEWS / f"{name}.gif").exists()})
    for name in ("anim_van_gogh", "anim_oil_morph"):
        rows.append({"name": name, "medium": MEDIUM_LABEL["animation"], "note": NOTES[name], "video_only": True,
                     "clip": name in CLIP_STYLES, "multi": name in MULTI_STYLES, "blender": False,
                     "hand": None,
                     "preview": (PREVIEWS / f"{name}.gif").exists()})
    for name, note in (("bizarre_manga", "inked, then coloured"), ("comic_halftone", "dots, then a slow blur")):
        rows.append({"name": name, "medium": "Ink", "note": note, "video_only": True,
                     "clip": False, "multi": False, "blender": False,
                     "hand": "the pencil, then the pen" if name == "bizarre_manga" else None,
                     "preview": (PREVIEWS / f"{name}.gif").exists()})
    rows.sort(key=lambda r: r["name"] != "contemporary")     # the default first
    return rows


def _run(job: dict) -> None:
    import pipeline
    try:
        res = pipeline.make(job["idea"], job["style"], job["out"],
                            {"painting": {"seconds": job["seconds"], "playback": job["playback"],
                                          "music": job["music"], "shorts": False},
                             "handplay": {"hand_overlay": job["hand"]}},
                            picture=job.get("picture"), video=job.get("video"), pictures=job.get("pictures"))
        job.update(status="done", video=str(res["video"]) if res.get("video") else None,
                   image=str(res["image"]) if res.get("image") else None, title=res.get("title", ""),
                   description=res.get("description", ""))
    except Exception as e:                                    # noqa: BLE001 - shown on the page
        job.update(status="failed", error=f"{e}")
        traceback.print_exc()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):                                # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data: dict) -> None:
        self._send(code, json.dumps(data).encode())

    def _file(self, path: Path, base: Path) -> None:
        path = path.resolve()
        if not path.is_relative_to(base.resolve()) or not path.is_file():
            return self._json(404, {"error": "not found"})
        self._send(200, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/":
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if url.path == "/api/styles":
            return self._json(200, {"styles": styles()})
        if url.path == "/api/model":
            return self._json(200, model_state())
        if url.path == "/api/music":
            return self._json(200, music_state())
        if url.path == "/api/blender":
            return self._json(200, blender_state())
        if url.path == "/api/job":
            state = progress.snapshot()
            data = {k: _job.get(k) for k in ("status", "style", "error", "title", "description")}
            data.update({k: state.get(k) for k in ("stage", "detail", "percent", "log")})
            if state.get("preview"):
                data["preview"] = "/out/" + str(Path(state["preview"]).resolve().relative_to(OUT.resolve())) \
                    if Path(state["preview"]).resolve().is_relative_to(OUT.resolve()) else None
                data["preview_at"] = state.get("preview_at")
            for key in ("video", "image"):
                if _job.get(key):
                    data[key] = "/out/" + str(Path(_job[key]).resolve().relative_to(OUT.resolve()))
            return self._json(200, data)
        if url.path.startswith("/previews/"):
            return self._file(PREVIEWS / urllib.parse.unquote(url.path[len("/previews/"):]), PREVIEWS)
        if url.path.startswith("/out/"):
            return self._file(OUT / urllib.parse.unquote(url.path[len("/out/"):]), OUT)
        if url.path in ("/favicon.ico", "/brand/app.ico"):
            return self._file(BRAND / "app.ico", BRAND)
        if url.path.startswith("/brand/"):
            return self._file(BRAND / urllib.parse.unquote(url.path[len("/brand/"):]), BRAND)
        self._json(404, {"error": "not found"})

    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        if url.path == "/api/upload":
            if not 0 < length <= MAX_UPLOAD:
                return self._json(400, {"error": "empty or too large (500 MB at most)"})
            name = urllib.parse.parse_qs(url.query).get("name", ["file"])[0]
            ext = Path(name).suffix.lower()
            if ext not in {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".m4v", ".webm", *AUDIO}:
                return self._json(400, {"error": "a picture (PNG, JPEG, WEBP), a video (MP4, MOV, WEBM) "
                                                 "or music (MP3, M4A, WAV, OGG, FLAC)"})
            data = self.rfile.read(length)
            UPLOADS.mkdir(parents=True, exist_ok=True)
            dst = UPLOADS / f"{hashlib.sha1(data).hexdigest()[:16]}{ext}"
            dst.write_bytes(data)
            kind = ("video" if ext in {".mp4", ".mov", ".m4v", ".webm"} else
                    "audio" if ext in AUDIO else "picture")
            return self._json(200, {"file": dst.name, "kind": kind})
        if url.path == "/api/blender":
            try:
                b = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "bad request"})
            if b.get("install"):
                if _blinstall.get("status") != "running":
                    threading.Thread(target=_install_blender, daemon=True).start()
                return self._json(200, {"ok": True})
            if b.get("forget"):
                import carving
                carving.save_blender(None)
                return self._json(200, blender_state())
            if b.get("path"):
                return self._json(200, connect_blender(str(b["path"])))
            return self._json(200, blender_state())          # look again
        if url.path == "/api/music/install":
            if _music.get("status") == "running":
                return self._json(409, {"error": "already downloading"})
            threading.Thread(target=_fetch_music, daemon=True).start()
            return self._json(200, {"ok": True})
        if url.path == "/api/model/install":
            if _model.get("status") == "running":
                return self._json(409, {"error": "already installing"})
            threading.Thread(target=_install_model, daemon=True).start()
            return self._json(200, {"ok": True})
        if url.path == "/api/paint":
            if _job.get("status") == "running":
                return self._json(409, {"error": "a painting is already being made"})
            try:
                b = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "bad request"})
            style = str(b.get("style", ""))
            if style not in brushwork.STYLES and style not in ("anim_van_gogh", "anim_oil_morph",
                                                                "comic_halftone", "bizarre_manga"):
                return self._json(400, {"error": f"unknown style {style!r}"})
            # one file, or several pictures (Oil paint (moving) uses them all as its poses)
            names = [str(n) for n in (b.get("files") or ([b["file"]] if b.get("file") else []))][:MAX_PICTURES]
            files = [UPLOADS / Path(n).name for n in names]
            if any(not f.exists() for f in files):
                return self._json(400, {"error": "upload the file again"})
            if len(files) > 1 and style not in MULTI_STYLES:
                return self._json(400, {"error": "this style paints one picture at a time: only Oil paint "
                                                 "(moving) takes several"})
            if len(files) > 1 and any(f.suffix.lower() not in PICTURES for f in files):
                return self._json(400, {"error": "several files must all be pictures: a video goes on its own"})
            upload = files[0] if files else None
            idea = str(b.get("idea", "")).strip()[:300]
            if not upload and not idea:
                return self._json(400, {"error": "choose a picture or describe an idea"})
            if upload and upload.suffix.lower() in AUDIO:
                return self._json(400, {"error": "that's music: add it under Music, and choose a picture to paint"})
            is_video = bool(upload and upload.suffix.lower() in VIDEOS)
            if is_video and style not in CLIP_STYLES:
                return self._json(400, {"error": "this style takes a picture, not a video: Van Gogh (moving) "
                                                 "and Oil paint (moving) take a clip"})
            if style in BLENDER_STYLES and not blender_state()["found"]:
                return self._json(400, {"error": "the carving styles need Blender: install it free from "
                                                 "blender.org, then connect it under Style"})
            import pipeline
            playback = b.get("playback", True) is not False
            if not playback and (style in pipeline.VIDEO_ONLY or is_video):
                return self._json(400, {"error": "this style is only made as a video: choose "
                                                 "\"The painting video\", or another style"})
            music = str(b.get("music") or "none")
            if music == "own":
                own = UPLOADS / Path(str(b.get("music_file", ""))).name
                if not (b.get("music_file") and own.exists() and own.suffix.lower() in AUDIO):
                    return self._json(400, {"error": "choose your music file again"})
                music = str(own)
            elif music not in ("none", "library"):
                return self._json(400, {"error": "bad music choice"})
            out = OUT / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            _job.clear()
            _job.update(status="running", style=style, idea=idea or "a picture", out=out,
                        seconds=max(10.0, min(90.0, float(b.get("seconds", 30)))), hand=bool(b.get("hand")),
                        playback=playback, music=music,
                        picture=None if is_video else upload, video=upload if is_video else None,
                        pictures=None if is_video else files)
            threading.Thread(target=_run, args=(_job,), daemon=True).start()
            return self._json(200, {"ok": True})
        self._json(404, {"error": "not found"})


def already_running(url: str) -> bool:
    """Whether this app (not some other program) is the one answering at url."""
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return b"<title>NoobPainterSimulation</title>" in r.read(4096)
    except OSError:
        return False


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second server bind a port already in use, so a
    # second launch would start a second app instead of finding the first.
    allow_reuse_address = os.name != "nt"


def serve(port: int = 8770, open_browser: bool = True) -> None:
    url = f"http://127.0.0.1:{port}"
    # The desktop shortcut double-clicked while the app is already open: show that one.
    if already_running(url):
        print(f"NoobPainterSimulation is already running at {url}; opening it.")
        if open_browser:
            webbrowser.open(url)
        return
    server = Server(("127.0.0.1", port), Handler)
    print(f"NoobPainterSimulation is running at {url}")
    print("Keep this window open while you use it; close it (or press Ctrl+C) to quit.")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NoobPainterSimulation</title>
<link rel="icon" type="image/png" href="/brand/app.png">
<link rel="alternate icon" href="/favicon.ico">
<style>
:root{--bg:#f6f3ee;--panel:#fff;--ink:#1f1c18;--muted:#6f675d;--line:#e3ddd3;--accent:#b5562f;--accent-ink:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#16140f;--panel:#201d17;--ink:#efe9df;--muted:#a59b8c;--line:#353027;--accent:#e07a4f;--accent-ink:#16140f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,system-ui,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 60px}h1{margin:0 0 4px;font-size:26px}p.sub{margin:0 0 22px;color:var(--muted)}
.grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.15fr);gap:20px}@media(max-width:820px){.grid{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px}h2{font-size:15px;margin:0 0 12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.drop{border:2px dashed var(--line);border-radius:12px;padding:22px;text-align:center;cursor:pointer;color:var(--muted)}.drop.over{border-color:var(--accent);color:var(--ink)}
.drop img,.drop video{max-width:100%;max-height:220px;border-radius:8px;display:block;margin:0 auto 8px}
.thumbs{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-bottom:6px}.thumbs figure{margin:0;width:74px;position:relative}
.thumbs img{width:74px;height:110px;object-fit:cover;margin:0 0 3px}.thumbs figcaption{position:absolute;top:3px;left:3px;background:var(--accent);color:var(--accent-ink);border-radius:9px;font-size:11px;padding:0 6px}
.thumbs figure.unused{opacity:.35}.blendpath{display:flex;gap:6px;margin-top:6px}.blendpath input{flex:1}
.thumbs span{display:flex;justify-content:space-between}.thumbs button{border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:6px;width:22px;height:22px;padding:0;font:inherit;line-height:1;cursor:pointer}.thumbs button:disabled{opacity:.35;cursor:default}
label{display:block;margin:14px 0 6px;font-weight:600}input[type=text],input[type=number]{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:9px;background:var(--bg);color:var(--ink);font:inherit}
.row{display:flex;gap:14px;align-items:center;flex-wrap:wrap}.row label{margin:0;font-weight:500}
.styles{display:grid;grid-template-columns:repeat(auto-fill,minmax(118px,1fr));gap:10px;max-height:430px;overflow:auto;padding:2px}
.card{border:2px solid var(--line);border-radius:10px;padding:6px;cursor:pointer;background:var(--bg);text-align:left;color:var(--ink);font:inherit}
.card.on{border-color:var(--accent)}.card img,.card .ph{width:100%;aspect-ratio:9/16;object-fit:cover;border-radius:6px;display:block;background:var(--line)}
.card b{display:block;font-size:13px;margin-top:5px}.card small{color:var(--muted);font-size:11.5px}
button.go{margin-top:18px;width:100%;padding:13px;border:0;border-radius:11px;background:var(--accent);color:var(--accent-ink);font-family:inherit;font-size:16px;font-weight:600;line-height:1;cursor:pointer}button.go:disabled{opacity:.5;cursor:default}
.bar{height:10px;background:var(--line);border-radius:10px;overflow:hidden;margin:10px 0}.bar span{display:block;height:100%;width:0;background:var(--accent);transition:width .6s}
.status{display:flex;justify-content:space-between;gap:10px}.muted{color:var(--muted)}.err{color:#c4372b}
pre{background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:10px;max-height:170px;overflow:auto;font-size:12px;white-space:pre-wrap}
.model{margin-top:8px;font-size:13px;color:var(--muted)}.model button{margin-left:6px;padding:5px 10px;border:1px solid var(--accent);border-radius:8px;background:transparent;color:var(--accent);font:inherit;cursor:pointer}.model pre{max-height:110px}.model a{color:var(--accent)}
.result video,.result img,.live img{width:100%;max-height:520px;object-fit:contain;border-radius:10px;background:#000}.live img{background:var(--bg)}
a.dl{display:inline-block;margin-top:10px;color:var(--accent);font-weight:600}
</style></head><body><main>
<h1>NoobPainterSimulation</h1><p class="sub">Pictures painted, drawn and carved stroke by stroke, by a simulation. Runs on this computer only.</p>
<div class="grid">
 <section class="panel">
  <h2>1 · Picture</h2>
  <div class="drop" id="drop"><div id="dropview">Drop a picture here, or click to choose</div></div>
  <input type="file" id="file" accept="image/*" hidden>
  <label for="idea">Or describe an idea</label>
  <input type="text" id="idea" placeholder="e.g. a lighthouse on a cliff in a storm">
  <div class="model" id="model"></div>
  <p class="muted" id="manynote"></p>
  <h2 style="margin-top:20px">2 · Style</h2>
  <div class="styles" id="styles"></div>
  <h2 style="margin-top:20px">3 · What to make</h2>
  <div class="row" id="make"><label><input type="radio" name="make" value="video" checked> The painting video <span class="muted">(watch it painted, stroke by stroke)</span></label>
   <label><input type="radio" name="make" value="picture"> Just the finished picture <span class="muted">(faster)</span></label></div>
  <p class="muted" id="makenote"></p>
  <div class="row" id="videoopts"><label for="secs">Length (seconds)</label><input type="number" id="secs" min="10" max="90" value="30" style="width:90px">
   <label id="handlabel"><input type="checkbox" id="hand"> Show a hand holding <span id="handtool">the brush</span></label></div>
  <p class="muted" id="handnote"></p>
  <div id="musicopts"><h2 style="margin-top:20px">4 · Music</h2>
   <div class="row"><label><input type="radio" name="music" value="none" checked> No music</label>
    <label><input type="radio" name="music" value="library" id="mlib"> Calm classical <span class="muted">(free, built in)</span></label>
    <label><input type="radio" name="music" value="own" id="mown"> My own music</label></div>
   <div class="model" id="musicnote"></div>
   <input type="file" id="musicfile" accept="audio/*,.mp3,.m4a,.wav,.ogg,.flac" hidden></div>
  <h2 style="margin-top:20px">Extras</h2>
  <div class="model" id="blender" style="margin-top:0"><b>Blender</b> · for the carving styles</div>
  <button class="go" id="go">Paint it</button><p class="err" id="formerr"></p>
 </section>
 <section class="panel">
  <h2>Progress</h2>
  <div class="status"><b id="stage">Waiting for a picture</b><span class="muted" id="pct"></span></div>
  <div class="bar"><span id="bar"></span></div><p class="muted" id="detail"></p>
  <div class="live" id="live"></div><div class="result" id="result"></div>
  <details style="margin-top:12px"><summary class="muted">Log</summary><pre id="log"></pre></details>
 </section>
</div></main>
<script>
const $=s=>document.querySelector(s);let uploads=[],style='contemporary',lastPreview=0;
const NAMES={anim_van_gogh:'Van Gogh (moving)',anim_oil_morph:'Oil paint (moving)',bw_exaggerated:'B&W exaggerated',compass_spiral:'Compass spiral',relief_wood:'Relief: wood',relief_marble:'Relief: marble',relief_clay:'Relief: clay',old_master:'Old Master',van_gogh:'Van Gogh',bizarre_manga:'Bizarre manga',comic_halftone:'Comic halftone'};
let styleRows=[];const pictureOnly=()=>document.querySelector('input[name=make]:checked').value==='picture';
// "just the picture" has no video options, and the moving and ink styles are videos by nature
function syncMake(){const p=pictureOnly(),row=styleRows.find(r=>r.name===style);
 $('#videoopts').style.display=p?'none':'';$('#musicopts').style.display=p?'none':'';
 $('#makenote').textContent=p&&row&&row.video_only?`${nice(style)} is only made as a video: choose another style for a picture.`:'';
 $('#go').textContent=p?'Paint the picture':'Paint it';
 // not every style has a hand: the compass draws alone, the moving and halftone styles have none
 const tool=row?row.hand:'the brush';$('#hand').disabled=!tool;if(!tool)$('#hand').checked=false;
 $('#handlabel').style.opacity=tool?'':'.5';$('#handtool').textContent=tool||'a tool';
 $('#handnote').textContent=tool?'':`${nice(style)} has no hand to show.`;
 // what this style takes: Oil paint (moving) several pictures; the moving styles a clip; the rest one picture
 const multi=!!(row&&row.multi),clip=!!(row&&row.clip),vid=uploads[0]&&uploads[0].kind==='video';
 $('#file').multiple=multi;$('#file').accept=clip?'image/*,video/*':'image/*';
 if(!uploads.length)$('#dropview').textContent=dropText();
 $('#manynote').innerHTML=vid&&!clip?`<span class="err">${nice(style)} takes a picture, not a video: choose a picture, or Van Gogh (moving) or Oil paint (moving) for this clip.</span>`:
  uploads.length>1&&multi?`Your ${uploads.length} pictures are the poses, in order: each smears into the next, and the last back into the first.`:
  uploads.length>1?`${nice(style)} paints one picture: picture 1. Only Oil paint (moving) uses several.`:'';
 document.querySelectorAll('.thumbs figure').forEach((f,i)=>f.classList.toggle('unused',i>0&&!multi));
 if(row&&row.blender&&blenderInfo&&!blenderInfo.found)$('#manynote').innerHTML=`<span class="err">${nice(style)} needs Blender: install or connect it under Extras, below.</span>`;}
const dropText=()=>{const row=styleRows.find(r=>r.name===style)||{};
 return row.multi?'Drop pictures here (up to 8, in order), or a short clip, or click to choose':
  row.clip?'Drop a picture or a short clip here, or click to choose':'Drop a picture here, or click to choose';};
document.querySelectorAll('input[name=make]').forEach(r=>r.onchange=syncMake);
let ownMusic=null,musicLib=false;
const musicChoice=()=>document.querySelector('input[name=music]:checked').value;
async function music(){const r=await fetch('/api/music');const m=await r.json();musicLib=m.installed;const el=$('#musicnote');
 $('#mlib').disabled=!m.installed;if(!m.installed&&musicChoice()==='library')document.querySelector('input[name=music][value=none]').checked=true;
 if(m.install==='running'){el.innerHTML=`Downloading the classical music (about 500 MB)…<pre>${(m.log||[]).join('\n')}</pre>`;setTimeout(music,3000);return;}
 const mine=ownMusic?`Your music: ${ownMusic.name.replace(/[<>&]/g,'')} <button type="button" id="changemusic">Change</button>`:'';
 if(m.installed){el.innerHTML=mine;return;}
 el.innerHTML=(m.install==='failed'?'<span class="err">The download stopped — press again to resume.</span> ':'')+
  `The calm classical music isn't downloaded yet (about 500 MB, free).<button type="button" id="getmusic">Download it</button>`+(mine?'<br>'+mine:'');}
document.querySelectorAll('input[name=music]').forEach(r=>r.onchange=()=>{if(r.value==='own'&&r.checked&&!ownMusic)$('#musicfile').click();});
$('#musicfile').onchange=async e=>{const f=e.target.files[0];if(!f)return;$('#formerr').textContent='';$('#musicnote').textContent='Uploading your music…';
 const r=await fetch('/api/upload?name='+encodeURIComponent(f.name),{method:'POST',body:f});const j=await r.json();
 if(!r.ok||j.kind!=='audio'){$('#formerr').textContent=j.error||'That isn\'t a music file';ownMusic=null;document.querySelector('input[name=music][value=none]').checked=true;music();return;}
 ownMusic={file:j.file,name:f.name};$('#mown').checked=true;music();};
document.addEventListener('click',async e=>{if(e.target.id!=='getmusic')return;e.target.disabled=true;
 const r=await fetch('/api/music/install',{method:'POST',body:'{}'});if(r.ok)music();});
document.addEventListener('click',e=>{if(e.target.id==='changemusic')$('#musicfile').click();});
const nice=n=>NAMES[n]||n.replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase());
async function loadStyles(){const r=await fetch('/api/styles');const j=await r.json();styleRows=j.styles;
 $('#styles').innerHTML=j.styles.map(s=>`<button type="button" class="card${s.name===style?' on':''}" data-style="${s.name}">${s.preview?`<img src="/previews/${s.name}-final.jpg" data-still="/previews/${s.name}-final.jpg" data-play="/previews/${s.name}.gif" alt="" loading="lazy">`:'<div class="ph"></div>'}<b>${nice(s.name)}</b><small>${s.medium}${s.note?' · '+s.note:''}${s.blender&&blenderInfo&&!blenderInfo.found?' · <span class="err">needs Blender</span>':''}</small></button>`).join('');syncMake();}
// the finished piece as a still; the preview of its making plays on hover
$('#styles').addEventListener('mouseover',e=>{const i=e.target.closest('.card')?.querySelector('img');if(i&&i.dataset.play&&i.src.indexOf('.gif')<0)i.src=i.dataset.play;});
$('#styles').addEventListener('mouseout',e=>{const c=e.target.closest('.card');if(!c||c.contains(e.relatedTarget))return;const i=c.querySelector('img');if(i&&i.dataset.still)i.src=i.dataset.still;});
$('#styles').addEventListener('click',e=>{const c=e.target.closest('.card');if(!c)return;style=c.dataset.style;document.querySelectorAll('.card').forEach(x=>x.classList.toggle('on',x===c));syncMake();});
const drop=$('#drop');drop.onclick=e=>{if(!e.target.closest('.thumbs button'))$('#file').click();};
drop.ondragover=e=>{e.preventDefault();drop.classList.add('over')};drop.ondragleave=()=>drop.classList.remove('over');
drop.ondrop=e=>{e.preventDefault();drop.classList.remove('over');send([...e.dataTransfer.files]);};
$('#file').onchange=e=>{send([...e.target.files]);e.target.value='';};
// several pictures (Oil paint (moving) makes them its poses, in this order), or one video
function showUploads(){const el=$('#dropview');if(!uploads.length){el.textContent=dropText();syncMake();return;}
 if(uploads[0].kind==='video'){el.innerHTML=`<video src="${uploads[0].url}" muted autoplay loop playsinline></video><small>${uploads[0].name}</small>`;syncMake();return;}
 const one=uploads.length===1;
 el.innerHTML=(one?`<img src="${uploads[0].url}" alt="">`:'')+`<div class="thumbs">`+(one?'':uploads.map((u,i)=>`<figure><img src="${u.url}" alt=""><figcaption>${i+1}</figcaption><span>`+
  `<button type="button" data-move="${i}" data-by="-1" title="Earlier"${i?'':' disabled'}>‹</button><button type="button" data-drop="${i}" title="Remove">×</button>`+
  `<button type="button" data-move="${i}" data-by="1" title="Later"${i<uploads.length-1?'':' disabled'}>›</button></span></figure>`).join(''))+
  `</div><small>${one?uploads[0].name:uploads.length+' pictures'} · `+((styleRows.find(r=>r.name===style)||{}).multi?'click or drop to add more':'click or drop to replace')+`</small>`;syncMake();}
drop.addEventListener('click',e=>{const b=e.target.closest('.thumbs button');if(!b)return;e.stopPropagation();
 if(b.dataset.drop!==undefined)uploads.splice(+b.dataset.drop,1);
 else{const i=+b.dataset.move,j=i+(+b.dataset.by);[uploads[i],uploads[j]]=[uploads[j],uploads[i]];}showUploads();});
async function send(files){if(!files.length)return;$('#formerr').textContent='';
 const row=styleRows.find(r=>r.name===style)||{};
 if(!row.multi&&files.length>1){files=files.slice(0,1);$('#formerr').textContent=`${nice(style)} takes one file at a time: only Oil paint (moving) takes several pictures.`;}
 const isVid=f=>/^video\//.test(f.type)||/\.(mp4|mov|m4v|webm)$/i.test(f.name);
 if(!row.clip&&files.some(isVid)){files=files.filter(f=>!isVid(f));
  $('#formerr').textContent=`${nice(style)} takes a picture, not a video: Van Gogh (moving) and Oil paint (moving) take a clip.`;if(!files.length)return;}
 $('#dropview').textContent='Uploading…';
 for(const f of files){const r=await fetch('/api/upload?name='+encodeURIComponent(f.name),{method:'POST',body:f});const j=await r.json();
  if(!r.ok){$('#formerr').textContent=j.error;continue;}
  if(j.kind==='audio'){$('#formerr').textContent='That\'s music: add it under Music, below.';continue;}
  const u={file:j.file,kind:j.kind,name:f.name.replace(/[<>&"]/g,''),url:URL.createObjectURL(f)};
  if(u.kind==='video'||(uploads[0]&&uploads[0].kind==='video')||!row.multi)uploads=[u];   // one at a time; a video alone
  else if(uploads.length<8)uploads.push(u);else $('#formerr').textContent='8 pictures at most';}
 showUploads();}
$('#go').onclick=async()=>{$('#formerr').textContent='';
 const body={style,idea:$('#idea').value,files:((styleRows.find(r=>r.name===style)||{}).multi?uploads:uploads.slice(0,1)).map(u=>u.file),seconds:+$('#secs').value,hand:$('#hand').checked,playback:!pictureOnly(),music:musicChoice(),music_file:ownMusic&&ownMusic.file};
 const r=await fetch('/api/paint',{method:'POST',body:JSON.stringify(body)});const j=await r.json();
 if(!r.ok){$('#formerr').textContent=j.error;return;}$('#result').innerHTML='';poll();};
async function poll(){const r=await fetch('/api/job');const j=await r.json();
 const st=j.status;$('#go').disabled=st==='running';
 $('#stage').textContent=st==='running'?({reference:'Preparing the picture',download:'Downloading (needed once)',plan:'Planning the strokes',paint:'Painting',assemble:'Cutting the video'}[j.stage]||'Working'):st==='done'?'Done':st==='failed'?'Something went wrong':'Waiting for a picture';
 $('#pct').textContent=st?Math.floor(j.percent||0)+'%':'';$('#bar').style.width=(j.percent||0)+'%';$('#detail').textContent=j.error||j.detail||'';
 $('#log').textContent=(j.log||[]).join('\n');
 if(j.preview&&j.preview_at!==lastPreview&&st==='running'){lastPreview=j.preview_at;$('#live').innerHTML=`<img src="${j.preview}?t=${lastPreview}" alt="The canvas so far">`;}
 if(st==='done'&&j.video){$('#live').innerHTML='';$('#result').innerHTML=`<video src="${j.video}" controls autoplay playsinline></video><a class="dl" href="${j.video}" download="painting.mp4">Download the video</a>`+(j.image?` &nbsp; <a class="dl" href="${j.image}" download="painting.png">Download the picture</a>`:'');}
 else if(st==='done'&&j.image){$('#live').innerHTML='';$('#result').innerHTML=`<img src="${j.image}" alt="The finished painting"><a class="dl" href="${j.image}" download="painting.png">Download the picture</a>`;}
 if(st==='running')setTimeout(poll,2000);}
async function model(){const r=await fetch('/api/model');const m=await r.json();const el=$('#model');
 if(m.installed){el.innerHTML='✓ Local image model ready: ideas can be drawn on this computer.';$('#idea').disabled=false;return;}
 $('#idea').disabled=true;$('#idea').placeholder='Install the local image model to describe an idea';
 if(m.install==='running'){el.innerHTML=`Installing the local image model (about 20 GB, resumable)…<pre>${(m.log||[]).join('\n')}</pre>`;setTimeout(model,3000);return;}
 el.innerHTML=(m.install==='failed'?'<span class="err">The install stopped — press again to resume.</span> ':'')+
  `To paint from a typed idea, this computer needs a local image model (Qwen-Image, about 20 GB, free).<button type="button" id="getmodel">Install it</button> <a href="https://github.com/Dankwa-Wadie/NoobPainterSimulation#the-local-image-model-optional" target="_blank" rel="noopener">What is this?</a>`;}
// Blender, for the carving styles: found in the usual places, or pointed at; looked for afresh
let blenderInfo=null;
// Blender, for the carving styles: install it here, or connect one you already have
async function blender(how){const el=$('#blender');
 const r=await fetch('/api/blender',how?{method:'POST',body:JSON.stringify(how)}:undefined);const b=await r.json();
 if(how&&how.install){setTimeout(blender,1000);return;}
 const had=blenderInfo&&blenderInfo.found;blenderInfo=b;
 const esc=t=>String(t||'').replace(/[<>&"]/g,'');const head='<b>Blender</b> · for the carving styles<br>';
 if(b.install==='running'){el.innerHTML=head+`Installing ${esc(b.offer)} (about 300 MB to download, 1 GB unpacked)…<pre>${esc((b.log||[]).join('\n'))}</pre>`;setTimeout(blender,2000);return;}
 if(b.found){el.innerHTML=head+`✓ ${esc(b.version)} connected <span class="muted">(${esc(b.path)})</span> <button type="button" data-blend="look">Look again</button>`+
  (b.saved?` <button type="button" data-blend="forget">Forget this location</button>`:'');}
 else el.innerHTML=head+(b.error?`<span class="err">${esc(b.error)}</span><br>`:'')+
  (b.install==='failed'?`<span class="err">The install stopped.</span><pre>${esc((b.log||[]).join('\n'))}</pre>`:'')+
  (b.offer?`Not installed yet. <button type="button" data-blend="install">${b.install==='failed'?'Try again':'Install '+esc(b.offer)}</button> <span class="muted">(free, from blender.org; about 300 MB)</span><br>`:
   `Not installed yet: <a href="https://www.blender.org/download/" target="_blank" rel="noopener">get it from blender.org</a>.<br>`)+
  `Already have Blender? <button type="button" data-blend="look">Find it</button> or paste where it is:`+
  `<div class="blendpath"><input type="text" id="blendpath" placeholder="e.g. C:\\Program Files\\Blender Foundation\\Blender 4.5\\blender.exe, or /Applications/Blender.app"><button type="button" data-blend="set">Connect</button></div>`;
 if(had!==b.found||!had)loadStyles();}
document.addEventListener('click',e=>{const k=e.target.dataset&&e.target.dataset.blend;if(!k)return;e.target.disabled=true;
 blender(k==='set'?{path:$('#blendpath').value}:k==='forget'?{forget:true}:k==='install'?{install:true}:{look:true});});
document.addEventListener('click',async e=>{if(e.target.id!=='getmodel')return;e.target.disabled=true;
 const r=await fetch('/api/model/install',{method:'POST',body:'{}'});if(r.ok)model();});
loadStyles();poll();model();music();blender();
</script></body></html>"""
