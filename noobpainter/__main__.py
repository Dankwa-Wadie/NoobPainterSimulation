"""python -m noobpainter paint picture.jpg --style van_gogh --out out/my-painting"""
import argparse
import logging
import sys

import noobpainter  # noqa: F401  (puts the engine's modules on the path)


def main() -> int:
    import brushwork
    ap = argparse.ArgumentParser(prog="noobpainter", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("paint", help="paint a picture (or an idea, or a clip) into a video")
    p.add_argument("picture", nargs="?", help="a picture to paint (PNG/JPEG); or use --idea")
    p.add_argument("--idea", default="", help="what to paint, drawn first by a local image model")
    p.add_argument("--video", help="a clip to repaint frame by frame (anim_* styles)")
    p.add_argument("--style", default="contemporary",
                   help="a style (see `styles`), or anim_van_gogh / anim_oil_morph")
    p.add_argument("--out", default="out/painting", help="output folder")
    p.add_argument("--seconds", type=float, default=45.0)
    p.add_argument("--hand", action="store_true",
                   help="show a hand holding the brush/pencil/tool on every stroke")
    sub.add_parser("styles", help="list the styles")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if a.cmd == "styles":
        for name, st in brushwork.STYLES.items():
            print(f"{name:16s} {st.get('medium', 'oil')}")
        print("anim_van_gogh    animation\nanim_oil_morph   animation")
        return 0
    if not (a.picture or a.idea or a.video):
        ap.error("give a picture, an --idea or a --video")
    import pipeline
    res = pipeline.make(a.idea, a.style, a.out, {"painting": {"seconds": a.seconds},
                                                 "handplay": {"hand_overlay": a.hand}},
                        picture=a.picture, video=a.video)
    print(res["video"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
