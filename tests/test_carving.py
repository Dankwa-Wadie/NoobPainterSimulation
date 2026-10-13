import unittest

import numpy as np
from scipy.ndimage import gaussian_filter

import carving


def relief(h=120, w=80, seed=0):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    dome = 0.12 + 0.6 * np.clip(1 - ((xx - w / 2) / 25) ** 2 - ((yy - h / 2) / 35) ** 2, 0, 1)
    return dome + 0.03 * rng.random((h, w))           # a dome with fine texture on it


class CutTest(unittest.TestCase):
    def test_a_cut_never_goes_below_the_surface_or_raises_the_block(self):
        T = relief()
        C = np.ones_like(T)
        before = C.copy()
        for _ in range(30):
            carving.cut(C, T, [(10.0, 10.0), (150.0, 230.0)], r=12, margin=0.0, bite=1.0, scale=0.5)
        self.assertTrue((C >= T - 1e-9).all())
        self.assertTrue((C <= before + 1e-9).all())
        self.assertLess(C.min(), 0.9)                      # it did cut

    def test_a_margin_is_left_for_the_finer_tools(self):
        T = relief()
        C = np.ones_like(T)
        carving.cut(C, T, [(10.0, 10.0), (150.0, 230.0)], r=12, margin=0.1, bite=1.0, scale=0.5)
        self.assertTrue((C >= T + 0.1 - 1e-9).all())


class ShapedTest(unittest.TestCase):
    def fine(self, a):
        return np.abs(a - gaussian_filter(a, 2)).mean()

    def test_big_tools_see_less_detail_and_never_cut_below_the_surface(self):
        T = relief()
        wide, small = carving.shaped(T, 22, 0.5), carving.shaped(T, 5, 0.5)
        self.assertTrue((wide >= T - 1.5e-3).all())
        self.assertTrue((small >= T - 1.5e-3).all())
        self.assertLess(self.fine(wide), self.fine(small))
        self.assertLess(self.fine(small), self.fine(T))

    def test_the_finest_tool_sees_the_true_surface(self):
        T = relief()
        self.assertIs(carving.shaped(T, 0.0, 0.5), T)
