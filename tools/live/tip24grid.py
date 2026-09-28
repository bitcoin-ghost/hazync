#!/usr/bin/env python3
"""Where the block-map cells go. Pure arithmetic — no PIL, so it can be tested anywhere.

⛔ THIS IS SPLIT OUT BECAUSE CI HAS NO PILLOW. `tip24e` and `tip24live` both import PIL at module
scope, so a test that imports either dies with `ModuleNotFoundError: No module named 'PIL'` before
it asserts anything. Every other test in this directory works around that by reading the renderer's
SOURCE as text, which can check that a line exists but not that the arithmetic is right. Geometry is
exactly the thing worth executing, so it lives here instead.

⛔ 144 CELLS IS 24 HOURS OF CHAIN. On a one-hour run at most ~6 tip blocks can ever exist, so the
grid was ~96% empty by construction and read as "3 of 144". The box stays where it is; only the
number of slots and their size change, so a short run gets a few large cells and a full day still
gets the film's 12x12 of 36px.

⚠ NOTHING IS HARDCODED HERE. The caller passes the film's geometry from `tip24e`, so there is one
source of truth for it and this module cannot drift from the picture.
"""
import math


def box_width(cols, cell, gap):
    """The width the full grid occupied — the area every layout must fit inside."""
    return cols * (cell + gap) - gap


def grid_geom(n, *, cols, cell, gap, rows):
    """(cols, cell, gap) for `n` slots inside the original grid box.

    `cols`, `cell`, `gap`, `rows` are the film's geometry. A run at or beyond a full day gets it
    back untouched; anything shorter is re-laid to fill the same box with fewer, larger cells.
    """
    n = max(1, int(n))
    if n >= cols * rows:
        return cols, cell, gap
    w = box_width(cols, cell, gap)
    c = max(1, min(n, int(math.ceil(math.sqrt(n)))))
    r = int(math.ceil(n / c))
    # A handful of big cells wants daylight between them; 144 small ones would drown in it.
    g = gap if n > 12 else gap * 3
    size = min((w - (c - 1) * g) / c, (w - (r - 1) * g) / r)
    return c, size, g


def grid_xy(i, x0, y0, cols, cell, gap):
    """Top-left of cell `i`, laid out left-to-right then top-to-bottom."""
    return x0 + (i % cols) * (cell + gap), y0 + (i // cols) * (cell + gap)
