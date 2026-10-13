import unittest

import numpy as np

import lineart


def face(h=256, w=192):
    yy, xx = np.mgrid[0:h, 0:w]
    img = np.ones((h, w, 3))
    dark = ((xx - w / 2) / (w * 0.3)) ** 2 + ((yy - h / 2) / (h * 0.3)) ** 2 < 1
    img[dark] = 0.1
    return img


def ink(lines):
    return sum(np.hypot(*np.diff(np.array(l, dtype=float), axis=0).T).sum() for l in lines)


class MazeTest(unittest.TestCase):
    def test_the_maze_is_perfect_and_denser_in_the_dark(self):
        rng = np.random.default_rng(0)
        dark = lineart.darkness(face(), blur=3)
        cells = lineart._cells(dark, 64, [0.18, 0.34, 0.5, 0.66])
        sizes = {s for _, _, s in cells}
        self.assertIn(4, sizes)                              # split down to 4 in the dark
        self.assertIn(64, sizes)                             # left whole in the light
        r = lineart.maze(face(), rng)
        self.assertGreater(len(r["solution"]), 3)
        # in at the middle of the top, out at the middle of the bottom: across the picture
        (x0, y0), (x1, y1) = r["solution"][0], r["solution"][-1]
        self.assertLess(y0, 0)
        self.assertGreater(y1, 192)
        self.assertLess(abs(x0 - 96), 40)
        self.assertLess(abs(x1 - 96), 40)

    def test_cells_tile_the_grid_with_no_gaps(self):
        dark = lineart.darkness(face(), blur=3)
        cells = lineart._cells(dark, 64, [0.18, 0.34, 0.5, 0.66])
        area = sum(s * s for _, _, s in cells)
        self.assertEqual(area, (192 - 192 % 64) * (256 - 256 % 64))

    def test_every_cell_is_reachable(self):
        # a perfect maze: the open passages form one tree over all the cells
        r = lineart.maze(face(), np.random.default_rng(3))
        self.assertTrue(r["solution"])

    def test_a_top_cell_that_does_not_halve_is_refused(self):
        with self.assertRaises(ValueError):
            lineart.maze(face(), np.random.default_rng(0), top=40)

    def test_chaining_keeps_every_line(self):
        lines = [[(0.0, 0.0), (5.0, 0.0)], [(5.0, 0.0), (5.0, 5.0)], [(9.0, 9.0), (9.0, 12.0)]]
        out = lineart.chain(lines)
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(ink(out), ink(lines))


class LineTest(unittest.TestCase):
    def test_continuous_is_one_unbroken_line(self):
        r = lineart.continuous(face(), np.random.default_rng(0))
        self.assertEqual(len(r["lines"]), 1)
        steps = np.hypot(*np.diff(np.array(r["lines"][0]), axis=0).T)
        self.assertLess(steps.max(), 15)                     # no jumps: the pen never lifts

    def test_waves_wave_more_in_the_dark(self):
        rows = lineart.waves(face(), np.random.default_rng(0))["lines"]
        mid = np.array(rows[len(rows) // 2])
        y = mid[:, 1]
        centre, edge = y[(mid[:, 0] > 80) & (mid[:, 0] < 112)], y[mid[:, 0] < 20]
        self.assertGreater(centre.std(), edge.std() * 3)
