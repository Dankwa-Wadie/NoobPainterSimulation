"""Fetch calm classical recordings that are free to use, with their licences.

The compositions are long out of copyright, but most recordings are not, and
YouTube's Content ID matches recordings. These come from Wikimedia Commons'
Musopen collections (Musopen's Kickstarter and "Set Chopin Free" released
their recordings to the public domain). A file is kept only when Commons
itself lists it as public domain or CC0; each one's licence, performer and
source page go into assets/music/classical/manifest.json.

    python tools/fetch_classical.py            # what is missing
"""
from __future__ import annotations

import json
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "music" / "classical"
API = "https://commons.wikimedia.org/w/api.php"
CATEGORIES = ["Category:Musopen", "Category:Set Chopin Free", "Category:Musopen Kickstarter Project"]
try:                                   # python.org builds ship no CA bundle of their own
    import certifi
    CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    CTX = ssl.create_default_context()
FREE = {"public domain", "cc0", "pd", "cc-zero"}
# Calm, slow pieces that sit under a painting; matched against file titles.
WANT = ["nocturne", "largo", "adagio", "gymnop", "clair de lune", "reverie", "rêverie",
        "andante", "lento", "berceuse", "prelude", "air", "canon", "arabesque", "pavane",
        "song without words", "meditation", "romance", "barcarolle"]
AVOID = ["violin b", "violin f", "allegro", "presto", "vivace", "march", "scherzo", "overture",
         "polonaise", "etude", "walk down"]


UA = {"User-Agent": "NoobPainterSimulation/1.0 (local music library tool)"}


def fetch(url: str, timeout: int = 60, tries: int = 6) -> bytes:
    """GET with backoff: Wikimedia answers 429 to anything hasty."""
    import time
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                        timeout=timeout, context=CTX) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code != 429 or k == tries - 1:
                raise
            wait = int(e.headers.get("Retry-After") or 0) or 15 * (k + 1)
            print(f"  (rate limited; waiting {wait}s)")
            time.sleep(wait)
    raise RuntimeError("unreachable")


def api(**params: str) -> dict:
    return json.loads(fetch(API + "?" + urllib.parse.urlencode({**params, "format": "json"})))


def members(category: str) -> list[str]:
    out, cont = [], {}
    while True:
        res = api(action="query", list="categorymembers", cmtitle=category, cmtype="file",
                  cmlimit="500", **cont)
        out += [m["title"] for m in res["query"]["categorymembers"]]
        if "continue" not in res:
            return out
        cont = {"cmcontinue": res["continue"]["cmcontinue"]}


def info(titles: list[str]) -> list[dict]:
    res = api(action="query", prop="imageinfo", titles="|".join(titles),
              iiprop="url|extmetadata|size|mime")
    return [p for p in res["query"]["pages"].values() if "imageinfo" in p]


def wanted(title: str) -> bool:
    t = title.lower()
    return (t.endswith((".ogg", ".oga", ".mp3", ".flac", ".opus"))
            and any(w in t for w in WANT) and not any(a in t for a in AVOID))


VOICE_LABELS = {"Speech", "Male speech, man speaking", "Female speech, woman speaking",
                "Conversation", "Chatter", "Crowd", "Hubbub, speech noise, speech babble", "Cough",
                "Applause", "Laughter", "Narration, monologue", "Whispering", "Singing",
                "Child speech, kid speaking"}


def scan_voices(manifest: list[dict], seconds: float = 60.0) -> None:
    """Mark each track's `voices`: AudioSet speech/crowd/cough/applause sounds found in
    2 s windows of its opening (a live recording had someone talking under the music).
    Uses the AST AudioSet classifier, locally."""
    import numpy as np
    from transformers import pipeline
    clf = pipeline("audio-classification", model="MIT/ast-finetuned-audioset-10-10-0.4593", top_k=12)
    for m in manifest:
        if "voices" in m:
            continue
        raw = subprocess.run(["ffmpeg", "-v", "error", "-t", f"{seconds:g}", "-i", str(OUT / m["file"]),
                              "-ac", "1", "-ar", "16000", "-f", "f32le", "-"], capture_output=True).stdout
        a = np.frombuffer(raw, dtype=np.float32)
        m["voices"] = [(round(i / 16000), r["label"], round(r["score"], 2))
                       for i in range(0, max(1, len(a) - 32000), 32000)
                       for r in clf({"raw": a[i:i + 32000], "sampling_rate": 16000})
                       if r["label"] in VOICE_LABELS and r["score"] > 0.15]
        print(f"  {'VOICES' if m['voices'] else 'clean'}: {m['title']}")


def main(limit: int = 24) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else []
    have = {m["title"] for m in manifest}
    titles = sorted({t for c in CATEGORIES for t in members(c) if wanted(t)} - have)
    print(f"{len(titles)} candidate recordings")
    for i in range(0, len(titles), 40):
        for page in info(titles[i:i + 40]):
            if len(manifest) >= limit:
                break
            ii = page["imageinfo"][0]
            meta = ii.get("extmetadata", {})
            licence = meta.get("LicenseShortName", {}).get("value", "")
            if licence.strip().lower() not in FREE and not licence.lower().startswith("public domain"):
                print(f"  skip ({licence or 'no licence'}): {page['title']}")
                continue
            name = page["title"].removeprefix("File:")
            stem = "".join(c if c.isalnum() else "-" for c in Path(name).stem.lower()).strip("-")
            dst = OUT / f"{stem[:80]}.m4a"
            tmp = OUT / f".dl-{stem[:60]}{Path(name).suffix}"
            tmp.write_bytes(fetch(ii["url"], timeout=600))
            import time
            time.sleep(3)                          # a polite pace between files
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp), "-vn", "-c:a", "aac",
                            "-b:a", "192k", str(dst)], check=True)
            tmp.unlink()
            secs = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                         "-of", "csv=p=0", str(dst)], capture_output=True,
                                        text=True, encoding="utf-8", errors="replace").stdout or 0)
            manifest.append({"file": dst.name, "title": page["title"], "licence": licence,
                             "artist": meta.get("Artist", {}).get("value", ""),
                             "source": ii.get("descriptionurl", ""), "seconds": round(secs, 1)})
            manifest_path.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
            print(f"  ok ({licence}, {secs:.0f}s): {name}")
    scan_voices(manifest)
    manifest_path.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(manifest)} recordings in {OUT}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 24)
