"""Stylefit: tune a painting style until it measures like a set of examples.

A style here is how strokes are laid, not a model: so "learning" it means
measuring what makes the examples look the way they do, then searching the
painter's settings (brushwork.STYLES) for those that paint pictures measuring
the same. The measures:

  black      share of near-black pixels (deep shadow, ink)
  p5         the 5th-percentile lightness: how dark the darks get
  smooth     share of pixels with almost no gradient (airbrushed, blended)
  texture    mean high-frequency detail (hatching, grain, visible strokes)
  dark_edges share of the strongest edges that are dark lines or shadow cores
  chroma     mean colourfulness

    python stylefit.py learn "a folder of example pictures"   # targets per style -> assets/styles/
    python stylefit.py tune caricature              # search settings on a test picture
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageFilter

import oilpaint

LOG = logging.getLogger("mken.stylefit")
ROOT = Path(__file__).resolve().parent.parent          # the repo root (assets/, vendor/)
TARGETS = ROOT / "assets" / "styles" / "targets.json"
# How far each measure may stray before it counts a whole unit of distance.
SCALE = {"black": 0.15, "p5": 15.0, "smooth": 0.1, "texture": 2.5, "dark_edges": 0.12,
         "chroma": 8.0}


def measure(img: Image.Image) -> dict[str, float]:
    rgb = np.asarray(img.convert("RGB").resize((384, int(384 * img.height / img.width))),
                     dtype=np.float64) / 255.0
    g = np.asarray(img.convert("L").resize((512, int(512 * img.height / img.width))), dtype=np.float64)
    lab = oilpaint._lab(rgb)
    gy, gx = np.gradient(g)
    mag = np.hypot(gx, gy)
    blur = np.asarray(Image.fromarray(g.astype(np.uint8)).filter(ImageFilter.GaussianBlur(3)),
                      dtype=np.float64)
    edges = mag > np.percentile(mag, 92)
    return {"black": float((g < 30).mean()), "p5": float(np.percentile(g, 5)),
            "smooth": float((mag < 2).mean()), "texture": float(np.abs(g - blur).mean()),
            "dark_edges": float((g[edges] < 60).mean()),
            "chroma": float(np.hypot(lab[..., 1], lab[..., 2]).mean())}


def learn(folder: Path) -> dict[str, dict[str, float]]:
    """Targets per style from example pictures: the grey, gritty ones are B&W,
    the colourful, smooth ones caricature. Each target is the group's median."""
    rows = []
    for f in sorted(folder.iterdir()):
        if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            rows.append(measure(Image.open(f)))
    bw = [r for r in rows if r["chroma"] < 7]
    car = [r for r in rows if r["chroma"] >= 7 and r["texture"] < 7]
    targets = {name: {k: float(np.median([r[k] for r in group])) for k in SCALE}
               for name, group in (("bw_exaggerated", bw), ("caricature", car)) if group}
    for name, group in (("bw_exaggerated", bw), ("caricature", car)):
        targets.setdefault(name, {})["examples"] = len(group)
    TARGETS.parent.mkdir(parents=True, exist_ok=True)
    TARGETS.write_text(json.dumps(targets, indent=1))
    return targets


def distance(m: dict[str, float], target: dict[str, float]) -> float:
    return float(np.sqrt(sum(((m[k] - target[k]) / SCALE[k]) ** 2 for k in SCALE)))


def _box(a: np.ndarray, r: int = 4) -> np.ndarray:
    k = 2 * r + 1
    c = np.pad(np.pad(a, r, mode="edge").cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)


def fidelity(painting: Image.Image, reference: Image.Image, width: int = 192) -> float:
    """SSIM of the painting against its reference: shapes, but also local brightness
    and contrast, so a face crushed into black scores low (correlation alone did not:
    0.86 for a painting whose subject had gone; SSIM 0.26 against 0.46 for a clear one)."""
    def grey(img: Image.Image) -> np.ndarray:
        return np.asarray(img.convert("L").resize((width, int(width * img.height / img.width)),
                                                  Image.LANCZOS), dtype=np.float64)
    x, y = grey(painting), grey(reference.resize(painting.size))
    mx, my = _box(x), _box(y)
    sx, sy, sxy = _box(x * x) - mx * mx, _box(y * y) - my * my, _box(x * y) - mx * my
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    return float(np.mean(((2 * mx * my + c1) * (2 * sxy + c2)) /
                         ((mx * mx + my * my + c1) * (sx + sy + c2))))


# Style alone can be matched by smearing and darkening the subject away (it
# was, on the first run), so losing the reference costs as much as the style.
FIDELITY_WEIGHT = 10.0


def score(painting: Image.Image, reference: Image.Image, target: dict[str, float]) -> dict[str, float]:
    m = measure(painting)
    f = fidelity(painting, reference)
    return {"style": distance(m, target), "fidelity": f,
            "total": distance(m, target) + FIDELITY_WEIGHT * (1 - f), **m}


# Settings to try per style: each is laid over brushwork.STYLES[style].
CANDIDATES: dict[str, list[dict[str, Any]]] = {
    "caricature": [
        {"ground": [["vine black", 1], ["raw umber", 1]], "accents": {"dark": 18, "light": 90, "width": 4}},
        {"ground": [["vine black", 1], ["raw umber", 1]], "blend": 1,
         "accents": {"dark": 18, "light": 90, "width": 4}},
        {"ground": [["raw umber", 1], ["lead white", 1]], "blend": 1,
         "accents": {"dark": 22, "light": 88, "width": 3}, "contour": [2.5, [["vine black", 1]], 700]},
        {"ground": [["vine black", 1], ["raw umber", 1]], "contrast": 1.35,
         "accents": {"dark": 20, "light": 88, "width": 3}, "contour": [3.0, [["vine black", 1]], 700]},
    ],
    "bw_exaggerated": [
        {"hatch": {"below": 50, "cross": 28, "spacing": 7, "length": 18, "width": 1.6},
         "accents": {"dark": 15, "light": 90, "width": 4}},
        {"hatch": {"below": 45, "cross": 25, "spacing": 9, "length": 22, "width": 2.0},
         "accents": {"dark": 20, "light": 90, "width": 5}},
        {"hatch": {"below": 40, "cross": 22, "spacing": 8, "length": 18, "width": 1.6},
         "accents": {"dark": 15, "light": 92, "width": 3}, "contrast": 1.3},
        {"hatch": {"below": 50, "cross": 28, "spacing": 8, "length": 20, "width": 1.8},
         "accents": {"dark": 18, "light": 90, "width": 4}, "blend": 1},
    ],
}


def tune(style: str, reference: Path, out: Path) -> dict[str, Any]:
    """Paint each candidate quickly, measure it, keep the closest to the target."""
    import brushwork
    target = json.loads(TARGETS.read_text())[style]
    results = []
    for i, cand in enumerate(CANDIDATES[style]):
        folder = out / f"{style}-{i}"
        cfg = {"brushwork": {"style": style, **cand, "frames": False, "width": 1000}}
        res = brushwork.paint(reference, folder, cfg)
        grade = brushwork.settings(cfg)["grade"]           # measured as the video shows it
        sc = score(brushwork.apply_grade(Image.open(res["painting"]).convert("RGB"), grade),
                   Image.open(reference).convert("RGB"), target)
        results.append({"candidate": i, "settings": cand, "measures": sc, "distance": sc["total"],
                        "painting": str(res["painting"]), "reference": str(reference)})
        LOG.info("%s #%d: total %.2f (style %.2f, fidelity %.2f)  %s", style, i, sc["total"],
                 sc["style"], sc["fidelity"], "  ".join(f"{k} {sc[k]:.2f}/{target[k]:.2f}" for k in SCALE))
    best = min(results, key=lambda r: r["distance"])
    (out / f"{style}-results.json").write_text(json.dumps(results, indent=1))
    return best


GRADES = [[lo, hi, g] for lo in (20, 28, 34, 40, 46) for hi in (225, 240, 252) for g in (0.85, 1.0, 1.15)]


def fit_grade(style: str, out: Path) -> dict[str, Any]:
    """Re-measure every tuned painting under each levels grade; keep the best pair.

    Grading is applied to the video after painting, so this needs no repaint."""
    import brushwork
    target = json.loads(TARGETS.read_text())[style]
    results = json.loads((out / f"{style}-results.json").read_text())
    best = None
    for r in results:
        img = Image.open(r["painting"]).convert("RGB")
        ref = Image.open(r["reference"]).convert("RGB")
        for grade in GRADES:
            sc = score(brushwork.apply_grade(img, grade), ref, target)
            if best is None or sc["total"] < best["distance"]:
                best = {**r, "settings": {**r["settings"], "grade": grade},
                        "distance": sc["total"], "measures": sc}
    (out / f"{style}-best.json").write_text(json.dumps(best, indent=1))
    LOG.info("%s: candidate %d with grade %s, total %.2f (style %.2f, fidelity %.2f)", style,
             best["candidate"], best["settings"]["grade"], best["distance"],
             best["measures"]["style"], best["measures"]["fidelity"])
    return best


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("learn")
    a1.add_argument("folder")
    a2 = sub.add_parser("tune")
    a2.add_argument("style")
    a2.add_argument("reference")
    a2.add_argument("out")
    a3 = sub.add_parser("grade")
    a3.add_argument("style")
    a3.add_argument("out")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    if a.cmd == "learn":
        print(json.dumps(learn(Path(a.folder).expanduser()), indent=1))
    elif a.cmd == "grade":
        print(json.dumps(fit_grade(a.style, Path(a.out))["settings"], indent=1))
    else:
        print(json.dumps(tune(a.style, Path(a.reference), Path(a.out)), indent=1))


if __name__ == "__main__":
    main()
