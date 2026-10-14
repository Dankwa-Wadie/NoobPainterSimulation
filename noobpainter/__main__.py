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
    p.add_argument("--music", default="library",
                   help='"library" (calm classical, if fetched), "none", or a music file of your own')
    p.add_argument("--picture-only", action="store_true",
                   help="just the finished picture (painting.png), no video; faster")
    sub.add_parser("styles", help="list the styles")
    u = sub.add_parser("ui", help="open the web app (local only)")
    u.add_argument("--port", type=int, default=8770)
    u.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if a.cmd == "styles":
        for name, st in brushwork.STYLES.items():
            print(f"{name:16s} {st.get('medium', 'oil')}")
        print("anim_van_gogh    animation\nanim_oil_morph   animation")
        print("comic_halftone   ink\nbizarre_manga    ink")
        return 0
    if a.cmd == "ui":
        import ui
        ui.serve(a.port, not a.no_browser)
        return 0
    if not (a.picture or a.idea or a.video):
        ap.error("give a picture, an --idea or a --video")
    import pipeline
    res = pipeline.make(a.idea, a.style, a.out, {"painting": {"seconds": a.seconds, "playback": not a.picture_only,
                                                              "music": a.music},
                                                 "handplay": {"hand_overlay": a.hand}},
                        picture=a.picture, video=a.video)
    print(res.get("video") or res["image"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
