"""Line art: pictures made only of pen lines, so the hand draws every mark it makes.

The pencil style shaded with the easel's area hatch, which lays dozens of lines
in one call; the hand could only sweep over them, and its motion didn't match
what appeared. Here every line is a pen stroke the hand follows exactly.

Three kinds, each a list of polylines in canvas units (the canvas is 1000 wide):

  continuous  one unbroken line: wave rows joined end to end, back and forth
              down the page, so the pen never lifts.
  maze        a pictorial maze: a grid that splits into smaller cells where the
              picture is dark, a random spanning tree over the cells (so exactly
              one path joins any two: a perfect, solvable maze), and walls
              wherever two cells aren't joined. Fine corridors read dark, wide
              ones light, so the picture appears from a distance. Walls are
              merged into long straight runs; the solution is kept for the end.
  waves       op art: parallel lines that wave faster where the picture is dark.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np
from PIL import Image, ImageFilter

import oilpaint

LOG = logging.getLogger("mken.lineart")

Line = list[tuple[float, float]]


def darkness(ref: np.ndarray, blur: float = 1.5) -> np.ndarray:
    """0 (white) .. 1 (black) per canvas unit."""
    img = Image.fromarray((ref * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(blur))
    L = oilpaint._lab(np.asarray(img, dtype=np.float64) / 255.0)[..., 0]
    return np.clip(1 - L / 100, 0, 1)


# --- one continuous line --------------------------------------------------------

def continuous(ref: np.ndarray, rng: np.random.Generator, spacing: float = 9.0,
               amplitude: float = 3.6) -> dict[str, Any]:
    """One unbroken line: the wave rows joined end to end, back and forth down the page.

    (A route through stippled dots was tried first: at 22,000 dots its straight
    jumps still crossed the picture and the face barely read.)"""
    rows = waves(ref, rng, spacing, amplitude)["lines"]
    line: Line = []
    for i, row in enumerate(rows):
        line += row if i % 2 == 0 else row[::-1]     # each row back the other way: a U-turn
    return {"lines": [line], "continuous": True}


# --- a pictorial maze -----------------------------------------------------------

def _cells(dark: np.ndarray, top: int, levels: list[float]) -> list[tuple[int, int, int]]:
    """Leaf cells (x, y, size) of a quadtree: split while the cell is darker than
    the level for its depth. `top` must be divisible by 2**len(levels)."""
    h, w = dark.shape
    out = []

    def split(x: int, y: int, s: int, depth: int) -> None:
        cell = dark[y:y + s, x:x + s]
        if depth < len(levels) and cell.size and cell.mean() > levels[depth]:
            half = s // 2
            for dy in (0, half):
                for dx in (0, half):
                    split(x + dx, y + dy, half, depth + 1)
        else:
            out.append((x, y, s))
    for y in range(0, h - h % top, top):
        for x in range(0, w - w % top, top):
            split(x, y, top, 0)
    return out


def _neighbours(cells: list[tuple[int, int, int]]) -> list[tuple[int, int, tuple]]:
    """Pairs of cells sharing part of an edge, with that shared segment."""
    unit = min(s for _, _, s in cells)
    owner: dict[tuple[int, int], int] = {}
    for i, (x, y, s) in enumerate(cells):
        for gy in range(y, y + s, unit):
            for gx in range(x, x + s, unit):
                owner[(gx, gy)] = i
    edges: dict[tuple[int, int], list] = {}
    for (gx, gy), i in owner.items():
        for dx, dy in ((unit, 0), (0, unit)):
            j = owner.get((gx + dx, gy + dy))
            if j is None or j == i:
                continue
            key = (min(i, j), max(i, j))
            # the unit-long shared segment between the two grid squares
            seg = ((gx + unit, gy), (gx + unit, gy + unit)) if dx else ((gx, gy + unit), (gx + unit, gy + unit))
            edges.setdefault(key, []).append(seg)
    return [(a, b, tuple(segs)) for (a, b), segs in edges.items()]


def _merge(segments: list[tuple[tuple[int, int], tuple[int, int]]]) -> list[Line]:
    """Collinear touching unit segments -> long straight lines (fewer pen strokes)."""
    horiz: dict[int, list[tuple[int, int]]] = {}
    vert: dict[int, list[tuple[int, int]]] = {}
    for (x0, y0), (x1, y1) in segments:
        if y0 == y1:
            horiz.setdefault(y0, []).append((min(x0, x1), max(x0, x1)))
        else:
            vert.setdefault(x0, []).append((min(y0, y1), max(y0, y1)))
    out: list[Line] = []
    for table, horizontal in ((horiz, True), (vert, False)):
        for fixed, runs in table.items():
            runs.sort()
            start, end = runs[0]
            for a, b in runs[1:] + [(None, None)]:
                if a is not None and a <= end:
                    end = max(end, b)
                    continue
                out.append([(start, fixed), (end, fixed)] if horizontal else [(fixed, start), (fixed, end)])
                if a is not None:
                    start, end = a, b
    return [[(float(x), float(y)) for x, y in line] for line in out]


def chain(lines: list[Line]) -> list[Line]:
    """Join lines that meet end to end into longer paths, as a pen draws a maze
    without lifting at every corner. Greedy: from each unused line, keep walking
    to an unused line touching the current end."""
    ends: dict[tuple[float, float], list[int]] = {}
    for i, line in enumerate(lines):
        for p in (line[0], line[-1]):
            ends.setdefault(p, []).append(i)
    used = [False] * len(lines)
    out: list[Line] = []
    for i in range(len(lines)):
        if used[i]:
            continue
        used[i] = True
        path = list(lines[i])
        for grow_front in (False, True):
            while True:
                tip = path[0] if grow_front else path[-1]
                nxt = next((j for j in ends.get(tip, []) if not used[j]), None)
                if nxt is None:
                    break
                used[nxt] = True
                seg = lines[nxt] if lines[nxt][0] == tip else lines[nxt][::-1]
                path = (seg[::-1][:-1] + path) if grow_front else (path + seg[1:])
        out.append(path)
    return out


def maze(ref: np.ndarray, rng: np.random.Generator, top: int = 64,
         levels: tuple[float, ...] = (0.18, 0.34, 0.5, 0.66)) -> dict[str, Any]:
    """`top` must halve evenly len(levels) times (64 -> 4 units); 40 left gaps."""
    if top % (2 ** len(levels)):
        raise ValueError(f"maze: top cell {top} doesn't halve {len(levels)} times")
    full = darkness(ref, blur=3)
    # the grid covers whole top cells, centred; the rest is a quiet margin
    h0, w0 = full.shape
    ox, oy = (w0 % top) // 2, (h0 % top) // 2
    dark = full[oy:oy + h0 - h0 % top, ox:ox + w0 - w0 % top]
    cells = _cells(dark, top, list(levels))
    pairs = _neighbours(cells)
    adj: dict[int, list[tuple[int, int]]] = {i: [] for i in range(len(cells))}
    for k, (a, b, _) in enumerate(pairs):
        adj[a].append((b, k))
        adj[b].append((a, k))
    # randomized Prim spanning tree: one path between any two cells. (Depth-first
    # carving made a solution that wound over half the picture; Prim branches
    # often, so the way through is shorter and the dead ends many.)
    #
    # Two stages, so the way through runs through the subject: first only the
    # small (dark) cells are joined, from the dark cell nearest the centre; then
    # the light cells hang off that network as branches and dead ends. In a
    # tree, a path between two dark cells can then only use dark cells, where
    # in one stage it slipped round the subject through the open light margin.
    fine = max(s for _, _, s in cells) // 4
    is_dark = [s <= fine for _, _, s in cells]
    h0, w0 = dark.shape
    darks = [i for i, d in enumerate(is_dark) if d] or [0]
    seed = min(darks, key=lambda i: (cells[i][0] - w0 / 2) ** 2 + (cells[i][1] - h0 / 2) ** 2)
    open_edges: set[int] = set()
    seen = {seed}
    for only_dark in (True, False):
        frontier = [(i, j, k) for i in seen for j, k in adj[i] if j not in seen]
        while frontier:
            pick = int(rng.integers(len(frontier)))
            frontier[pick], frontier[-1] = frontier[-1], frontier[pick]
            _, j, k = frontier.pop()
            if j in seen or (only_dark and not is_dark[j]):
                continue
            seen.add(j)
            open_edges.add(k)
            frontier += [(j, m, e) for m, e in adj[j] if m not in seen]
    walls = [seg for k, (_, _, segs) in enumerate(pairs) if k not in open_edges for seg in segs]
    # The entrance and exit sit mid-top and mid-bottom, so the way through has to
    # cross the picture (corner to corner, it ran round the light border and
    # the red solution barely showed).
    h, w = dark.shape
    W, H = w - w % top, h - h % top

    def cell_at(px: float, py: float) -> int:
        return next(i for i, (x, y, s) in enumerate(cells) if x <= px < x + s and y <= py < y + s)
    start, goal = cell_at(W / 2, 1), cell_at(W / 2, H - 1)
    sx, _, ss = cells[start]
    gx, _, gs = cells[goal]
    border = [[(0.0, 0.0), (float(sx), 0.0)], [(float(sx + ss), 0.0), (float(W), 0.0)],
              [(float(W), 0.0), (float(W), float(H))],
              [(float(W), float(H)), (float(gx + gs), float(H))], [(float(gx), float(H)), (0.0, float(H))],
              [(0.0, float(H)), (0.0, 0.0)]]
    lines = chain(_merge(walls) + border)
    # the solution: the tree path from the entrance cell to the exit cell
    parent = {start: None}
    queue = [start]
    while queue:
        here = queue.pop(0)
        for j, k in adj[here]:
            if k in open_edges and j not in parent:
                parent[j] = here
                queue.append(j)
    path, node = [], goal
    while node is not None:
        x, y, s = cells[node]
        path.append((x + s / 2, y + s / 2))
        node = parent[node]
    solution = [(sx + ss / 2, -top / 3)] + path[::-1] + [(gx + gs / 2, H + top / 3)]
    LOG.info("maze: %d cells, %d wall lines, solution %d steps", len(cells), len(lines), len(path))
    def shift(line: Line) -> Line:
        return [(round(x + ox, 1), round(y + oy, 1)) for x, y in line]
    return {"lines": [shift(line) for line in lines], "solution": shift(solution),
            "cells": len(cells)}


# --- op-art waves ---------------------------------------------------------------

def waves(ref: np.ndarray, rng: np.random.Generator, spacing: float = 9.0,
          amplitude: float = 3.6) -> dict[str, Any]:
    """Horizontal lines whose waves quicken (more ink) where the picture is dark."""
    dark = darkness(ref, blur=2)
    h, w = dark.shape
    lines = []
    for y0 in np.arange(spacing / 2, h, spacing):
        phase, pts = 0.0, []
        for x in np.arange(0, w, 1.5):
            d = dark[int(min(h - 1, y0)), int(min(w - 1, x))]
            phase += 1.5 * (0.05 + 1.2 * d)                  # frequency grows with darkness
            pts.append((round(float(x), 1), round(float(y0 + amplitude * d * np.sin(phase)), 1)))
        lines.append(pts)
    return {"lines": lines}


# --- a compass spiral -------------------------------------------------------------

def spiral(ref: np.ndarray, rng: np.random.Generator, spacing: float = 6.0,
           step: float = 4.0, inward: bool = False) -> dict[str, Any]:
    """A two-centre spiral, as draftsmen draw one with a compass: half-circles that
    swap between two centres `spacing / 2` apart, each a little wider, so the rings
    stand `spacing` apart. Drawn from the centre out, as a spiral is naturally drawn
    (ink from the first arc); `inward` draws it from the outside in instead, which
    hides the middle longer but opens on faint arcs over blank paper. Each arc carries the pencil pressure along it,
    from the picture's darkness: the picture appears in the weight of the rings."""
    dark = darkness(ref, blur=2)
    h, w = dark.shape
    cx, cy = w / 2, h / 2
    d = spacing / 2
    centres = [(cx - d / 2, cy), (cx + d / 2, cy)]
    # Start just outside the picture's content, not at the paper's corners: arcs
    # through blank background barely mark and made a dull first quarter.
    yy, xx = np.nonzero(dark > 0.25)
    reach = (float(np.hypot(xx - cx, yy - cy).max()) if len(xx) else np.hypot(w, h) / 2) + spacing * 3
    arcs, r, k = [], d, 0
    while r < reach:
        ox, oy = centres[k % 2]
        a0 = np.pi if k % 2 == 0 else 0.0               # alternate halves: top, then bottom
        n = max(8, int(np.pi * r / step))
        ang = a0 + np.linspace(0, np.pi, n + 1)
        xs, ys = ox + r * np.cos(ang), oy + r * np.sin(ang)
        inside = (xs >= 0) & (xs < w) & (ys >= 0) & (ys < h)
        if inside.any():
            # only the parts on the paper: a lead lifted off the edge draws nothing
            idx = np.nonzero(inside)[0]
            runs = np.split(idx, np.nonzero(np.diff(idx) > 1)[0] + 1)
            for run in runs:
                if len(run) < 2:
                    continue
                pts = [(round(float(xs[i]), 1), round(float(ys[i]), 1)) for i in run]
                prs = [round(0.1 + 0.9 * float(dark[min(h - 1, int(ys[i])), min(w - 1, int(xs[i]))]) ** 0.8, 2)
                       for i in run]
                arcs.append({"pts": pts, "pressure": prs, "pivot": (round(ox, 1), round(oy, 1)),
                             "radius": round(float(r), 1)})
        r += d
        k += 1
    if inward:
        arcs.reverse()
    return {"lines": [a["pts"] for a in arcs], "arcs": arcs}


KINDS = {"continuous": continuous, "maze": maze, "waves": waves, "spiral": spiral}
