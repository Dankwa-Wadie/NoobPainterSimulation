import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image, ImageDraw

import brushwork
import stylefit


def ink(size=(300, 400)):
    img = Image.new("RGB", size, (255, 255, 255))
    d = ImageDraw.Draw(img)
    for x in range(0, size[0], 4):
        d.line([(x, 0), (x + 60, size[1])], fill=(0, 0, 0), width=2)
    return img


class MeasureTest(unittest.TestCase):
    def test_flat_is_smooth_and_hatching_is_textured(self):
        flat = stylefit.measure(Image.new("RGB", (300, 400), (120, 80, 60)))
        hatched = stylefit.measure(ink())
        self.assertGreater(flat["smooth"], 0.95)
        self.assertLess(flat["texture"], 0.5)
        self.assertGreater(hatched["texture"], flat["texture"] + 10)
        self.assertGreater(flat["chroma"], hatched["chroma"])

    def test_distance_to_itself_is_zero(self):
        m = stylefit.measure(ink())
        self.assertEqual(stylefit.distance(m, m), 0.0)

    def test_learn_splits_grey_from_colour(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(3):
                ink().save(Path(d) / f"bw{i}.png")
                Image.new("RGB", (300, 400), (200, 60 + i * 10, 40)).save(Path(d) / f"c{i}.png")
            with mock.patch.object(stylefit, "TARGETS", Path(d) / "out" / "t.json"):
                t = stylefit.learn(Path(d))
        self.assertEqual(t["bw_exaggerated"]["examples"], 3)
        self.assertEqual(t["caricature"]["examples"], 3)
        self.assertLess(t["bw_exaggerated"]["chroma"], t["caricature"]["chroma"])


class FinishPassTest(unittest.TestCase):
    def ramp(self):
        x = np.linspace(0, 1, 200)
        return np.repeat(np.repeat(x[None, :, None], 200, axis=0), 3, axis=2)  # dark left, light right

    def test_hatching_only_in_the_darks_and_crossed_in_the_deepest(self):
        rng = np.random.default_rng(0)
        lines = brushwork.plan_hatching(self.ramp(), below=50, cross=25, spacing=8, length=12, rng=rng)
        xs = [line["pts"][1][0] for line in lines]
        self.assertTrue(lines)
        self.assertLess(max(xs), 110)                       # nothing on the light half
        left = sum(1 for x in xs if x < 40)
        mid = sum(1 for x in xs if 60 < x < 100)
        self.assertGreater(left, mid)                       # crossed where darkest

    def test_accents_put_black_in_the_dark_and_white_in_the_light(self):
        rng = np.random.default_rng(0)
        canvas = np.full((200, 200, 3), 0.5)
        dots = brushwork.plan_accents(self.ramp(), canvas, dark=15, light=92, width=4,
                                      budget=200, rng=rng)
        black = [d["pts"][0][0] for d in dots if d["pile"] == 0]
        white = [d["pts"][0][0] for d in dots if d["pile"] == 1]
        self.assertTrue(black and white)
        self.assertLess(max(black), 60)
        self.assertGreater(min(white), 140)
