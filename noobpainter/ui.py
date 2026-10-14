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
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import brushwork
import progress

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out" / "ui"
UPLOADS = OUT / "uploads"
PREVIEWS = ROOT / "docs" / "media" / "styles"
MAX_UPLOAD = 500 * 1024 ** 2

MEDIUM_LABEL = {"oil": "Oil paint", "pencil": "Pencil", "pen": "Pen", "compass": "Compass",
                "carving": "Carving", "animation": "Moving painting"}
NOTES = {"relief_wood": "carved in Blender", "relief_marble": "carved in Blender",
         "relief_clay": "moulded in Blender", "anim_van_gogh": "takes a picture or a clip",
         "anim_oil_morph": "takes a picture or a clip"}

_job: dict = {}
_model: dict = {"status": "idle", "log": []}


def model_state() -> dict:
    """Whether the optional local image model is installed, and any install's progress."""
    import localimage
    cfg = localimage.settings(None)
    return {"installed": localimage.installed(cfg), "missing": localimage.missing_files(cfg),
            "install": _model.get("status"), "log": _model.get("log", [])[-12:]}


def _install_model() -> None:
    import subprocess
    _model.update(status="running", log=[])
    proc = subprocess.Popen(["bash", str(ROOT / "tools" / "install_image_model.sh")], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in proc.stdout:
        line = line.rstrip().split("\r")[-1]
        if line:
            _model["log"] = (_model["log"] + [line])[-40:]
    _model["status"] = "done" if proc.wait() == 0 else "failed"


def styles() -> list[dict]:
    rows = []
    for name, st in brushwork.STYLES.items():
        medium = st.get("medium", "oil")
        if st.get("process") == "mould":
            medium = "carving"
        rows.append({"name": name, "medium": MEDIUM_LABEL.get(medium, medium),
                     "note": NOTES.get(name, ""), "preview": (PREVIEWS / f"{name}.gif").exists()})
    for name in ("anim_van_gogh", "anim_oil_morph"):
        rows.append({"name": name, "medium": MEDIUM_LABEL["animation"], "note": NOTES[name],
                     "preview": (PREVIEWS / f"{name}.gif").exists()})
    for name, note in (("bizarre_manga", "inked, then coloured"), ("comic_halftone", "dots, then a slow blur")):
        rows.append({"name": name, "medium": "Ink", "note": note,
                     "preview": (PREVIEWS / f"{name}.gif").exists()})
    rows.sort(key=lambda r: r["name"] != "contemporary")     # the default first
    return rows


def _run(job: dict) -> None:
    import pipeline
    try:
        res = pipeline.make(job["idea"], job["style"], job["out"],
                            {"painting": {"seconds": job["seconds"]},
                             "handplay": {"hand_overlay": job["hand"]}},
                            picture=job.get("picture"), video=job.get("video"))
        job.update(status="done", video=str(res["video"]), title=res.get("title", ""),
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
        if url.path == "/api/job":
            state = progress.snapshot()
            data = {k: _job.get(k) for k in ("status", "style", "error", "title", "description")}
            data.update({k: state.get(k) for k in ("stage", "detail", "percent", "log")})
            if state.get("preview"):
                data["preview"] = "/out/" + str(Path(state["preview"]).resolve().relative_to(OUT.resolve())) \
                    if Path(state["preview"]).resolve().is_relative_to(OUT.resolve()) else None
                data["preview_at"] = state.get("preview_at")
            if _job.get("video"):
                data["video"] = "/out/" + str(Path(_job["video"]).resolve().relative_to(OUT.resolve()))
            return self._json(200, data)
        if url.path.startswith("/previews/"):
            return self._file(PREVIEWS / urllib.parse.unquote(url.path[len("/previews/"):]), PREVIEWS)
        if url.path.startswith("/out/"):
            return self._file(OUT / urllib.parse.unquote(url.path[len("/out/"):]), OUT)
        self._json(404, {"error": "not found"})

    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        if url.path == "/api/upload":
            if not 0 < length <= MAX_UPLOAD:
                return self._json(400, {"error": "empty or too large (500 MB at most)"})
            name = urllib.parse.parse_qs(url.query).get("name", ["file"])[0]
            ext = Path(name).suffix.lower()
            if ext not in {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".m4v", ".webm"}:
                return self._json(400, {"error": "a picture (PNG, JPEG, WEBP) or a video (MP4, MOV, WEBM)"})
            data = self.rfile.read(length)
            UPLOADS.mkdir(parents=True, exist_ok=True)
            dst = UPLOADS / f"{hashlib.sha1(data).hexdigest()[:16]}{ext}"
            dst.write_bytes(data)
            kind = "video" if ext in {".mp4", ".mov", ".m4v", ".webm"} else "picture"
            return self._json(200, {"file": dst.name, "kind": kind})
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
            upload = UPLOADS / Path(str(b.get("file", ""))).name if b.get("file") else None
            if upload and not upload.exists():
                return self._json(400, {"error": "upload the file again"})
            idea = str(b.get("idea", "")).strip()[:300]
            if not upload and not idea:
                return self._json(400, {"error": "choose a picture or describe an idea"})
            is_video = bool(upload and upload.suffix.lower() in {".mp4", ".mov", ".m4v", ".webm"})
            if is_video and not style.startswith("anim_"):
                return self._json(400, {"error": "a video clip needs a Moving painting style"})
            out = OUT / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            _job.clear()
            _job.update(status="running", style=style, idea=idea or "a picture", out=out,
                        seconds=max(10.0, min(90.0, float(b.get("seconds", 30)))), hand=bool(b.get("hand")),
                        picture=None if is_video else upload, video=upload if is_video else None)
            threading.Thread(target=_run, args=(_job,), daemon=True).start()
            return self._json(200, {"ok": True})
        self._json(404, {"error": "not found"})


def serve(port: int = 8770, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"NoobPainterSimulation is running at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NoobPainterSimulation</title>
<style>
:root{--bg:#f6f3ee;--panel:#fff;--ink:#1f1c18;--muted:#6f675d;--line:#e3ddd3;--accent:#b5562f;--accent-ink:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#16140f;--panel:#201d17;--ink:#efe9df;--muted:#a59b8c;--line:#353027;--accent:#e07a4f;--accent-ink:#16140f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,system-ui,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 60px}h1{margin:0 0 4px;font-size:26px}p.sub{margin:0 0 22px;color:var(--muted)}
.grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.15fr);gap:20px}@media(max-width:820px){.grid{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px}h2{font-size:15px;margin:0 0 12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.drop{border:2px dashed var(--line);border-radius:12px;padding:22px;text-align:center;cursor:pointer;color:var(--muted)}.drop.over{border-color:var(--accent);color:var(--ink)}
.drop img,.drop video{max-width:100%;max-height:220px;border-radius:8px;display:block;margin:0 auto 8px}
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
.result video,.live img{width:100%;max-height:520px;object-fit:contain;border-radius:10px;background:#000}.live img{background:var(--bg)}
a.dl{display:inline-block;margin-top:10px;color:var(--accent);font-weight:600}
</style></head><body><main>
<h1>NoobPainterSimulation</h1><p class="sub">Pictures painted, drawn and carved stroke by stroke, by a simulation. Runs on this computer only.</p>
<div class="grid">
 <section class="panel">
  <h2>1 · Picture</h2>
  <div class="drop" id="drop"><div id="dropview">Drop a picture or a short video here, or click to choose</div></div>
  <input type="file" id="file" accept="image/*,video/*" hidden>
  <label for="idea">Or describe an idea</label>
  <input type="text" id="idea" placeholder="e.g. a lighthouse on a cliff in a storm">
  <div class="model" id="model"></div>
  <h2 style="margin-top:20px">2 · Style</h2>
  <div class="styles" id="styles"></div>
  <h2 style="margin-top:20px">3 · Options</h2>
  <div class="row"><label for="secs">Length (seconds)</label><input type="number" id="secs" min="10" max="90" value="30" style="width:90px">
   <label><input type="checkbox" id="hand"> Show a hand holding the brush</label></div>
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
const $=s=>document.querySelector(s);let upload=null,style='contemporary',lastPreview=0;
const NAMES={anim_van_gogh:'Van Gogh (moving)',anim_oil_morph:'Oil paint (moving)',bw_exaggerated:'B&W exaggerated',compass_spiral:'Compass spiral',relief_wood:'Relief: wood',relief_marble:'Relief: marble',relief_clay:'Relief: clay',old_master:'Old Master',van_gogh:'Van Gogh',bizarre_manga:'Bizarre manga',comic_halftone:'Comic halftone'};
const nice=n=>NAMES[n]||n.replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase());
async function loadStyles(){const r=await fetch('/api/styles');const j=await r.json();
 $('#styles').innerHTML=j.styles.map(s=>`<button type="button" class="card${s.name===style?' on':''}" data-style="${s.name}">${s.preview?`<img src="/previews/${s.name}-final.jpg" data-still="/previews/${s.name}-final.jpg" data-play="/previews/${s.name}.gif" alt="" loading="lazy">`:'<div class="ph"></div>'}<b>${nice(s.name)}</b><small>${s.medium}${s.note?' · '+s.note:''}</small></button>`).join('');}
// the finished piece as a still; the preview of its making plays on hover
$('#styles').addEventListener('mouseover',e=>{const i=e.target.closest('.card')?.querySelector('img');if(i&&i.dataset.play&&i.src.indexOf('.gif')<0)i.src=i.dataset.play;});
$('#styles').addEventListener('mouseout',e=>{const c=e.target.closest('.card');if(!c||c.contains(e.relatedTarget))return;const i=c.querySelector('img');if(i&&i.dataset.still)i.src=i.dataset.still;});
$('#styles').addEventListener('click',e=>{const c=e.target.closest('.card');if(!c)return;style=c.dataset.style;document.querySelectorAll('.card').forEach(x=>x.classList.toggle('on',x===c));});
const drop=$('#drop');drop.onclick=()=>$('#file').click();
drop.ondragover=e=>{e.preventDefault();drop.classList.add('over')};drop.ondragleave=()=>drop.classList.remove('over');
drop.ondrop=e=>{e.preventDefault();drop.classList.remove('over');if(e.dataTransfer.files[0])send(e.dataTransfer.files[0]);};
$('#file').onchange=e=>{if(e.target.files[0])send(e.target.files[0]);};
async function send(f){$('#formerr').textContent='';$('#dropview').textContent='Uploading…';
 const r=await fetch('/api/upload?name='+encodeURIComponent(f.name),{method:'POST',body:f});const j=await r.json();
 if(!r.ok){$('#dropview').textContent='Drop a picture or a short video here, or click to choose';$('#formerr').textContent=j.error;return;}
 upload=j;const url=URL.createObjectURL(f);
 $('#dropview').innerHTML=(j.kind==='video'?`<video src="${url}" muted autoplay loop playsinline></video>`:`<img src="${url}" alt="">`)+`<small>${f.name}</small>`;
 if(j.kind==='video'&&!style.startsWith('anim_')){style='anim_van_gogh';loadStyles();}}
$('#go').onclick=async()=>{$('#formerr').textContent='';
 const body={style,idea:$('#idea').value,file:upload&&upload.file,seconds:+$('#secs').value,hand:$('#hand').checked};
 const r=await fetch('/api/paint',{method:'POST',body:JSON.stringify(body)});const j=await r.json();
 if(!r.ok){$('#formerr').textContent=j.error;return;}$('#result').innerHTML='';poll();};
async function poll(){const r=await fetch('/api/job');const j=await r.json();
 const st=j.status;$('#go').disabled=st==='running';
 $('#stage').textContent=st==='running'?({reference:'Preparing the picture',plan:'Planning the strokes',paint:'Painting',assemble:'Cutting the video'}[j.stage]||'Working'):st==='done'?'Done':st==='failed'?'Something went wrong':'Waiting for a picture';
 $('#pct').textContent=st?Math.floor(j.percent||0)+'%':'';$('#bar').style.width=(j.percent||0)+'%';$('#detail').textContent=j.error||j.detail||'';
 $('#log').textContent=(j.log||[]).join('\n');
 if(j.preview&&j.preview_at!==lastPreview&&st==='running'){lastPreview=j.preview_at;$('#live').innerHTML=`<img src="${j.preview}?t=${lastPreview}" alt="The canvas so far">`;}
 if(st==='done'&&j.video){$('#live').innerHTML='';$('#result').innerHTML=`<video src="${j.video}" controls autoplay playsinline></video><a class="dl" href="${j.video}" download>Download the video</a><p class="muted" style="white-space:pre-wrap">${(j.title||'')}</p>`;}
 if(st==='running')setTimeout(poll,2000);}
async function model(){const r=await fetch('/api/model');const m=await r.json();const el=$('#model');
 if(m.installed){el.innerHTML='✓ Local image model ready: ideas can be drawn on this computer.';$('#idea').disabled=false;return;}
 $('#idea').disabled=true;$('#idea').placeholder='Install the local image model to describe an idea';
 if(m.install==='running'){el.innerHTML=`Installing the local image model (about 20 GB, resumable)…<pre>${(m.log||[]).join('\n')}</pre>`;setTimeout(model,3000);return;}
 el.innerHTML=(m.install==='failed'?'<span class="err">The install stopped — press again to resume.</span> ':'')+
  `To paint from a typed idea, this computer needs a local image model (Qwen-Image, about 20 GB, free).<button type="button" id="getmodel">Install it</button> <a href="https://github.com/Dankwa-Wadie/NoobPainterSimulation#the-local-image-model-optional" target="_blank" rel="noopener">What is this?</a>`;}
document.addEventListener('click',async e=>{if(e.target.id!=='getmodel')return;e.target.disabled=true;
 const r=await fetch('/api/model/install',{method:'POST',body:'{}'});if(r.ok)model();});
loadStyles();poll();model();
</script></body></html>"""
