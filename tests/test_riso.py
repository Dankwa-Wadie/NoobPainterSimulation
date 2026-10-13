import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

import riso


class SeparateTest(unittest.TestCase):
    def test_paper_takes_no_ink_and_an_ink_takes_itself(self):
        cfg = riso.settings()
        paper, inks = riso._rgb(cfg["paper"]), riso.inks_for(cfg)
        rgb = np.stack([paper, inks[0] * paper, inks[1] * paper])[None]
        cover = riso.separate(rgb, inks, paper)[0]
        self.assertTrue(np.all(cover[0] < 0.05))
        self.assertGreater(cover[1, 0], 0.8)
        self.assertLess(cover[1, 1], 0.2)
        self.assertGreater(cover[2, 1], 0.8)

    def test_coverage_stays_in_range(self):
        rgb = np.random.default_rng(0).random((8, 8, 3))
        cover = riso.separate(rgb, riso.inks_for(riso.settings()), np.ones(3))
        self.assertTrue(np.all((cover >= 0) & (cover <= 1)))


class ApplyTest(unittest.TestCase):
    def test_print_is_seeded_and_sized(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "in.png"
            Image.linear_gradient("L").convert("RGB").resize((40, 60)).save(src)
            a = riso.apply(src, Path(d) / "a.png", size=(80, 120), seed=3)
            b = riso.apply(src, Path(d) / "b.png", size=(80, 120), seed=3)
            self.assertEqual(Image.open(a).size, (80, 120))
            self.assertEqual(Image.open(a).tobytes(), Image.open(b).tobytes())

    def test_named_palettes_use_known_inks(self):
        for names in riso.PALETTES.values():
            self.assertTrue(set(names) <= set(riso.INKS))
