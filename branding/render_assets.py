#!/usr/bin/env python3
"""Rasterize Framewipe brand marks with Pillow. Run from the repo root:

    python3 branding/render_assets.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
INK = (18, 20, 16, 255)       # #121410
PAPER = (244, 241, 234, 255)  # #f4f1ea
WIPE = (196, 245, 66, 255)    # #c4f542
INK_RGB = INK[:3]
PAPER_RGB = PAPER[:3]
WIPE_RGB = WIPE[:3]


def draw_mark(im: Image.Image, ox: int, oy: int, size: int, ink, wipe, width=None):
    """Draw the 2x2 wiped-frame mark into im at (ox, oy) of `size` pixels."""
    d = ImageDraw.Draw(im)
    # Geometry matches branding/mark.svg viewBox 0..32
    s = size / 32.0
    sw = max(1.5, 2.2 * s) if width is None else width

    def R(x, y, w, h, rx):
        x0, y0 = ox + x * s, oy + y * s
        x1, y1 = ox + (x + w) * s, oy + (y + h) * s
        rr = max(1, rx * s)
        d.rounded_rectangle([x0, y0, x1, y1], radius=rr, outline=ink, width=int(round(sw)))

    R(3, 3, 11.5, 11.5, 2.2)
    R(17.5, 3, 11.5, 11.5, 2.2)
    R(3, 17.5, 11.5, 11.5, 2.2)
    # Open L for the wiped cell
    x0 = ox + 17.5 * s
    y0 = oy + 17.5 * s
    x1 = ox + 29.0 * s
    y1 = oy + 29.0 * s
    d.line([(x0, y1), (x1 - 2.2 * s, y1)], fill=wipe, width=int(round(sw)))
    d.line([(x1, y1 - 2.2 * s), (x1, y0)], fill=wipe, width=int(round(sw)))
    # round the corner join
    r = max(1, 2.2 * s)
    d.arc([x1 - 2 * r, y1 - 2 * r, x1, y1], start=0, end=90, fill=wipe, width=int(round(sw)))


def _font(size: int):
    for name in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        p = Path(name)
        if p.is_file():
            try:
                return ImageFont.truetype(str(p), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def save_png(im: Image.Image, path: Path):
    im.save(path, format="PNG", optimize=True)
    print("wrote", path)


def main():
    # 32px and 16px marks on paper
    for n in (16, 32, 64, 180):
        im = Image.new("RGBA", (n, n), PAPER)
        draw_mark(im, 0, 0, n, INK, WIPE)
        name = "favicon-%d.png" % n if n in (16, 32) else ("apple-touch-icon.png" if n == 180 else "mark-%d.png" % n)
        save_png(im, HERE / name)

    # ICO
    ico32 = Image.new("RGBA", (32, 32), PAPER)
    draw_mark(ico32, 0, 0, 32, INK, WIPE)
    ico16 = Image.new("RGBA", (16, 16), PAPER)
    draw_mark(ico16, 0, 0, 16, INK, WIPE)
    ico32.save(HERE / "favicon.ico", format="ICO", sizes=[(16, 16), (32, 32)])
    print("wrote", HERE / "favicon.ico")

    # OG 1200x630
    W, H = 1200, 630
    og = Image.new("RGB", (W, H), PAPER_RGB)
    d = ImageDraw.Draw(og)
    mark = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
    draw_mark(mark, 0, 0, 160, INK, WIPE, width=10)
    og.paste(mark, (80, (H - 160) // 2), mark)
    font = _font(72)
    small = _font(28)
    d.text((280, 230), "Framewipe", font=font, fill=INK_RGB)
    d.text((280, 330), "Prep frames locally. Nothing uploaded.", font=small, fill=(92, 97, 86))
    save_png(og, HERE / "og.png")


if __name__ == "__main__":
    main()
