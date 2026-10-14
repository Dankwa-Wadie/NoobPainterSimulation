import unittest
from pathlib import Path

import numpy as np
from PIL import Image

import brushwork
import oilpaint
import handplay
import pipeline as painting


class StyleTest(unittest.TestCase):
    def test_every_style_resolves(self):
        for style in brushwork.STYLES:
            cfg = brushwork.settings({"brushwork": {"style": style}})
            self.assertIn(cfg["palette"], oilpaint.PALETTES)
            self.assertTrue(cfg["prompt"])
            self.assertEqual(cfg["layers"][0][5], 0)        # the first layer covers the canvas

    def test_unknown_style_is_refused(self):
        with self.assertRaises(ValueError):
            brushwork.settings({"brushwork": {"style": "cubist"}})

    def test_posterize_flattens_the_reference(self):
        rng = np.random.default_rng(0)
        ref = rng.random((60, 40, 3))
        flat = brushwork.prepare(ref, {"posterize": 4, "saturate": 1, "contrast": 1})
        self.assertLessEqual(len(np.unique(flat.reshape(-1, 3).round(3), axis=0)), 4)

    def test_contours_follow_edges(self):
        ref = np.ones((200, 200, 3))
        ref[:, 100:] = 0.0                                  # one vertical edge at x = 100
        lines = brushwork.plan_contours(ref, 3.0, 50, np.random.default_rng(0))
        self.assertTrue(lines)
        xs = [x for line in lines for x, _ in line["pts"]]
        self.assertLess(abs(np.median(xs) - 100), 6)


class PlaybackTest(unittest.TestCase):
    def strokes(self, n=40):
        return [{"t0": i * 2.0, "t1": i * 2.0 + 1.5, "pts": [(0, 0), (10, 10)], "width": 5}
                for i in range(n)]

    def test_schedule_fits_the_length_and_ends_on_the_painting(self):
        video, hand = handplay.schedule(self.strokes(), 30.0, handplay.DEFAULTS)
        self.assertAlmostEqual(video[-1], 30.0, delta=1.0)
        self.assertGreaterEqual(hand[-1], self.strokes()[-1]["t1"])
        self.assertTrue(np.all(np.diff(video) > 0) and np.all(np.diff(hand) >= 0))

    def test_teaser_is_blurred_and_captioned(self):
        img = Image.new("RGB", (108, 192), (200, 30, 30))
        img.paste((20, 20, 200), (0, 96, 108, 192))
        out = handplay.teaser_frame(img, 0.0, handplay.DEFAULTS)
        self.assertEqual(out.size, img.size)
        mid = np.asarray(out)[96]
        self.assertTrue(30 < mid[54][0] < 200)              # the edge is smeared


class DescriptionTest(unittest.TestCase):
    def test_says_it_is_simulated_and_asks_no_guesses_by_default(self):
        text = painting.describe("a fox", "van_gogh", None)
        self.assertIn("not a human artist", text)
        self.assertNotIn("guess", text.lower())             # the guessing games are off
        self.assertIn("Van Gogh", text)
        self.assertNotIn("fox", text)

    def test_the_old_shorts_description_still_asks_for_guesses(self):
        text = painting.describe("a fox", "van_gogh", None, shorts=True)
        self.assertIn("guess", text.lower())
        self.assertNotIn("fox", text)                       # the answer stays out
        self.assertIn("carved", painting.describe("an owl", "relief_wood", None).lower())
        self.assertIn("drawn", painting.describe("an owl", "maze", None).lower())


class HiddenSubjectTest(unittest.TestCase):
    def test_subject_strokes_come_last_after_the_block_in(self):
        subject = np.zeros((100, 100), dtype=bool)
        subject[40:60, 40:60] = True
        def st(x, y):
            return {"pts": [(x, y), (x + 2, y)]}
        groups = [{"layer": 0, "strokes": [st(50, 50), st(5, 5)]},
                  {"layer": 1, "strokes": [st(50, 50), st(10, 10)]},
                  {"layer": 2, "strokes": [st(45, 45), st(90, 90)]}]
        order = brushwork.reorder(groups, subject)
        flat = [s for g in order for s in g["strokes"]]
        self.assertEqual(len(flat), 6)
        self.assertEqual([g["layer"] for g in order], [0, 1, 2, 1, 2])
        self.assertTrue(all(s.get("subject") for s in flat[-2:]))
        self.assertFalse(any(s.get("subject") for s in flat[:4]))

    def test_no_mask_keeps_the_order(self):
        groups = [{"layer": 0, "strokes": []}, {"layer": 1, "strokes": []}]
        self.assertIs(brushwork.reorder(groups, None), groups)


class CameraTest(unittest.TestCase):
    def test_the_close_up_stays_on_the_canvas(self):
        cam = handplay.Camera((200, 400), (100, 200), 2.0, 0.01)
        img = Image.new("RGB", (200, 400))
        img.paste((255, 0, 0), (0, 0, 100, 200))            # the top-left quarter is red
        out = cam.view(img, np.array([0.0, 0.0]), 1.0)      # aimed past the corner
        self.assertEqual(out.size, (100, 200))
        self.assertEqual(out.getpixel((50, 100)), (255, 0, 0))

    def test_zoom_one_is_the_whole_frame(self):
        cam = handplay.Camera((200, 400), (100, 200), 2.0, 0.5)
        img = Image.new("RGB", (200, 400), (0, 0, 255))
        img.paste((255, 0, 0), (0, 0, 100, 400))
        out = cam.view(img, None, 0.04, zoom=1.0)
        self.assertEqual(out.getpixel((10, 100))[0], 255)
        self.assertEqual(out.getpixel((90, 100))[2], 255)

    def test_the_teaser_keeps_shape_but_not_detail(self):
        img = Image.new("RGB", (540, 960), (255, 255, 255))
        img.paste((0, 0, 0), (170, 280, 370, 680))          # a big shape: still readable
        for x in range(190, 350, 6):                        # fine stripes inside it: gone
            img.paste((255, 255, 255), (x, 300, x + 2, 660))
        cfg = {**handplay.DEFAULTS, "teaser_text": ""}
        out = np.asarray(handplay.teaser_frame(img, 1.0, cfg), dtype=float).mean(-1)
        self.assertLess(out[480, 270], 140)                 # the shape is still there...
        self.assertGreater(out[480, 40], 230)               # ...against its background
        row = out[480, 200:340]
        self.assertLess(row.max() - row.min(), 25)          # ...but the stripes have melted



class DrawingLuaTest(unittest.TestCase):
    def test_no_drawing_pass_overwrites_the_canvas_globals(self):
        import re
        g = {"tool": "2B", "pressure": 0.6, "width": 2, "strokes": [{"pts": [(1, 1), (5, 5)]}],
             "bands": ("AB", 1, 2)}
        shade = {"tool": "6B", "shade": [40, 2.2, 18], "pressure": 0.8, "width": 45,
                 "strokes": [{"pts": [(0, 0)], "tile": [0, 0, 90], "angle": 0.5, "cross": True}]}
        lua = "\n".join(brushwork.drawing_lua(g, 0) + brushwork.drawing_lua(shade, 1))
        self.assertIsNone(re.search(r"^\s*(W|H)\s*=", lua, re.M))


class AliveTest(unittest.TestCase):
    def test_near_moves_more_than_far_and_it_starts_still(self):
        from unittest import mock
        img = Image.new("RGB", (120, 200), (20, 20, 20))
        for x in range(0, 120, 6):                          # stripes, so movement shows
            img.paste((230, 230, 230), (x, 0, x + 3, 200))
        depth = np.zeros((200, 120))
        depth[:, 60:] = 1.0                                # right half near, left half far
        with mock.patch("carving.depth", return_value=depth):
            alive = handplay.Alive(img, 0.05)
        still = np.asarray(alive.frame(0.0), dtype=float)
        self.assertLess(np.abs(still - np.asarray(img, dtype=float)).mean(), 1.0)
        moved = np.asarray(alive.frame(1.25), dtype=float)  # a quarter of the sway: the furthest
        far = np.abs(moved[:, 5:50] - still[:, 5:50]).mean()
        near = np.abs(moved[:, 70:115] - still[:, 70:115]).mean()
        self.assertGreater(near, 0)
        self.assertAlmostEqual(far, near, delta=near * 0.5)  # both halves move, opposite ways


class PictureOnlyTest(unittest.TestCase):
    def test_the_picture_is_graded_like_the_video_and_the_frames_go(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            painted = Path(td) / "painted"
            (painted / "frames").mkdir(parents=True)
            (painted / "frames" / "f.png").write_bytes(b"x")
            Image.new("RGB", (8, 8), (100, 100, 100)).save(painted / "painting.png")
            grade = [20, 220, 1.0]
            (painted / "strokes.json").write_text(json.dumps({"grade": grade}), encoding="utf-8")
            res = painting._picture_only(painted, Path(td), "van_gogh", owns=False)
            got = Image.open(res["image"]).getpixel((0, 0))
            self.assertEqual(got, (brushwork.grade_lut(grade)[100],) * 3)
            self.assertFalse((painted / "frames").exists())
            self.assertNotIn("video", res)

    def test_video_only_styles_refuse_a_picture(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            for style in painting.VIDEO_ONLY:
                with self.assertRaises(ValueError):
                    painting.make("a cat", style, td, {"painting": {"playback": False}})


class MusicChoiceTest(unittest.TestCase):
    def test_none_own_and_library(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            own = Path(td) / "song.mp3"
            own.write_bytes(b"x")
            self.assertIsNone(handplay.choose_music(30, 0, {"painting": {"music": "none"}}))
            self.assertEqual(handplay.choose_music(30, 0, {"painting": {"music": str(own)}}), own)
            self.assertIsNone(handplay.choose_music(30, 0, {"painting": {"music": str(own) + "x"}}))
            with mock.patch.object(handplay, "pick_music", return_value=own) as pick:
                self.assertEqual(handplay.choose_music(30, 7, {}), own)      # the library by default
                pick.assert_called_once_with(30, 7)


class AppVideoTest(unittest.TestCase):
    def test_the_app_video_has_no_shorts_games(self):
        """No teaser, no "Did you guess it?", never upside down: only with shorts on."""
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            Image.new("RGB", (40, 70), (90, 90, 90)).save(out / "reference.png")
            painted = out / "painted"
            (painted / "frames").mkdir(parents=True)
            (painted / "frames" / "f.png").write_bytes(b"x")
            Image.new("RGB", (40, 70)).save(painted / "painting.png")
            (painted / "strokes.json").write_text("{}", encoding="utf-8")
            seen = {}

            def render(frames, strokes, video, seconds, size, music, config=None):
                seen.update(config["handplay"])
                Path(video).write_bytes(b"x")
            for shorts in (False, True):
                seen.clear()
                with mock.patch.object(handplay, "render", render), \
                     mock.patch.object(painting, "reference", lambda *a, **k: out / "reference.png"):
                    res = painting.make("x", "van_gogh", out, {"painting": {"shorts": shorts,
                                        "music": "none", "upside_down_every": 1, "keep_frames": True}})
                if shorts:
                    self.assertNotIn("teaser", seen)
                    self.assertTrue(seen["upside_down"])
                else:
                    self.assertEqual((seen["teaser"], seen["ending_text"], seen["upside_down"]), (0, "", False))
                self.assertTrue(Path(res["image"]).exists())          # the picture comes with the video


class HandOptionTest(unittest.TestCase):
    def test_the_hand_matches_the_style_and_is_off_where_there_is_none(self):
        import ui
        hands = {r["name"]: r["hand"] for r in ui.styles()}
        self.assertEqual(hands["contemporary"], "the brush")
        self.assertEqual(hands["pencil"], "the pencil")
        self.assertEqual(hands["relief_clay"], "the modelling tool")
        for name in ("compass_spiral", "anim_van_gogh", "anim_oil_morph", "comic_halftone"):
            self.assertIsNone(hands[name], name)
