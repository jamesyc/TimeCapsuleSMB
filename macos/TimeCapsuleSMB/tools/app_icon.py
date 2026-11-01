#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow", "scipy"]
# [tool.uv]
# exclude-newer = "1 day"
# ///
"""Writes Assets/AppIcon/TimeCapsuleSMB.icon from Assets/AppIcon/tcs.jpg.

Run it after changing tcs.jpg; package_app.py compiles the result with actool.
The artwork becomes one full-bleed layer. macOS clips it to its own icon
shape, on macOS 26 and in the flattened copies actool renders for macOS 14
and 15, so nothing outside the artwork's rounded square may show: the black
corners of tcs.jpg are filled with the gray beside them. The layer keeps the
JPEG's full resolution and gets a light sharpening, because macOS shrinks it
once more for every size it draws.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from scipy import ndimage

ASSETS = Path(__file__).resolve().parent.parent / "Assets" / "AppIcon"
SOURCE = ASSETS / "tcs.jpg"
ICON = ASSETS / "TimeCapsuleSMB.icon"
LAYER = "artwork.png"
# Icon Composer's macOS canvas, in points.
CANVAS = 1024
# Pixels dropped inside the artwork's edge before filling: its bevel and the
# dark JPEG fringe would otherwise smear into the corners.
EDGE = 14


def artwork_mask(art: Image.Image) -> np.ndarray:
    width, height = art.size
    bright = art.convert("L").point(lambda value: 255 if value > 40 else 0)
    for corner in [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]:
        ImageDraw.floodfill(bright, corner, 128)
    inside = np.asarray(bright) != 128
    # The straight edges run into the image border with the same fringe.
    inside[:EDGE, :] = inside[-EDGE:, :] = False
    inside[:, :EDGE] = inside[:, -EDGE:] = False
    return ndimage.binary_erosion(inside, iterations=EDGE)


def fill_corners(art: Image.Image) -> Image.Image:
    pixels = np.asarray(art)
    inside = artwork_mask(art)
    _, (rows, columns) = ndimage.distance_transform_edt(~inside, return_indices=True)
    nearest = Image.fromarray(pixels[rows, columns]).filter(ImageFilter.GaussianBlur(6))
    return Image.fromarray(np.where(inside[..., None], pixels, np.asarray(nearest)))


def main() -> None:
    art = Image.open(SOURCE).convert("RGB")
    layer = fill_corners(art).filter(ImageFilter.UnsharpMask(radius=1.2, percent=50, threshold=1))
    (ICON / "Assets").mkdir(parents=True, exist_ok=True)
    layer.save(ICON / "Assets" / LAYER, optimize=True)
    document = {
        "fill": "automatic",
        "groups": [
            {
                "layers": [
                    {
                        "glass": False,
                        "image-name": LAYER,
                        "name": "artwork",
                        "position": {"scale": round(CANVAS / art.width, 6), "translation-in-points": [0, 0]},
                    }
                ],
                "shadow": {"kind": "neutral", "opacity": 0.5},
                "translucency": {"enabled": False, "value": 0.0},
            }
        ],
        "supported-platforms": {"squares": ["macOS"]},
    }
    (ICON / "icon.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
