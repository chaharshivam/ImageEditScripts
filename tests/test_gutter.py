"""Gutter 50/50 fallback when no white interior gutter exists."""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

import split_grids as sg


def test_detect_axis_falls_back_without_gutter():
    img = Image.new("RGB", (200, 200), (0, 0, 0))
    # Four textured-enough colored quadrants, no white gutter.
    pix = img.load()
    for y in range(200):
        for x in range(200):
            if y < 100 and x < 100:
                pix[x, y] = (180, 40, 40)
            elif y < 100:
                pix[x, y] = (40, 180, 40)
            elif x < 100:
                pix[x, y] = (40, 40, 180)
            else:
                pix[x, y] = (180, 180, 40)
    gray = np.asarray(img, dtype=np.float32).mean(axis=2)
    rows = sg.detect_axis(gray, 0, 243.0, 6.0, 4, 0.20)
    cols = sg.detect_axis(gray, 1, 243.0, 6.0, 4, 0.20)
    assert rows.detected is False
    assert cols.detected is False
    assert rows.lo_end == 100
    assert cols.lo_end == 100


def test_process_grid_warns_on_fallback(tmp_path):
    src_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    src_dir.mkdir()
    img = Image.new("RGB", (120, 120), (30, 80, 120))
    img.save(src_dir / "grid.png")
    args = sg.build_parser().parse_args([str(src_dir), str(out_dir), "--dry-run"])
    sink = []

    class Capture(sg.Log):
        def _emit(self, text, stream=None):
            sink.append(text)

    log = Capture(quiet=False, color=False)
    stats = sg.Stats()
    sg.process_grid(src_dir / "grid.png", src_dir, out_dir, args,
                    sg.Upscaler("lanczos"), log, stats, 1, 1)
    joined = "\n".join(sink)
    assert "FALLING BACK TO 50/50" in joined
