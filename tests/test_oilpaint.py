import unittest

import numpy as np
from PIL import Image

import oilpaint


TABLE = [{"recipe": [["lead white", 1]], "rgb": [0.94, 0.91, 0.86]},
         {"recipe": [["bone black", 1]], "rgb": [0.1, 0.1, 0.1]},
         {"recipe": [["vermilion", 1]], "rgb": [0.85, 0.25, 0.15]}]


class ColourTest(unittest.TestCase):
    def test_match_takes_the_nearest_swatch(self):
        self.assertEqual(oilpaint.match(np.array([0.9, 0.3, 0.2]), TABLE), [("vermilion", 1)])
        self.assertEqual(oilpaint.match(np.array([0.0, 0.0, 0.0]), TABLE), [("bone black", 1)])

    def test_candidates_cover_every_tube(self):
        used = {t for r in oilpaint.candidate_mixes() for t, _ in r}
        self.assertEqual(used, set(oilpaint.TUBES))


class PlanTest(unittest.TestCase):
    def picture(self):
        img = Image.new("RGB", (90, 160), (20, 30, 60))
        img.paste((230, 150, 140), (20, 60, 70, 140))
        return img

    def test_regions_run_dark_to_light(self):
        labels, rgb = oilpaint.regions(self.picture(), 2, 40, 0)
        self.assertEqual(labels.shape, (71, 40))
        self.assertLess(rgb[0].sum(), rgb[1].sum())
        self.assertEqual(labels[0, 0], 0)                  # the dark ground is region A

    def test_plan_is_chunked_lua_in_painting_order(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "p.png"
            self.picture().save(src)
            lua = oilpaint.plan(src, {**oilpaint.DEFAULTS, "grid": 40, "block_colours": 2,
                                      "body_colours": 3}, TABLE)
        self.assertTrue(lua.startswith("--@ engine 5\n--@ chunk\ncanvas{"))
        order = [lua.index(s) for s in ("c:hatch", 'hand="broad"', 'hand="body"', 'hand="detail"')]
        self.assertEqual(order, sorted(order))
