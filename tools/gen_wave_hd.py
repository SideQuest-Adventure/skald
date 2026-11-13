"""Prepare approved ImageGen artwork for Tk; Pillow is build-time only.

Run from the repository root: python tools/gen_wave_hd.py
No creative artwork is drawn here: crop, seamless-wrap, and antialiased sizing.
"""
import json
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets" / "wave-hd-source.png"
OUTPUT = ROOT / "assets" / "wave-hd"
HEIGHTS = (48, 64, 80, 96, 128, 160)
AMPLITUDES = (.48, .58, .67, .76, .84, .92, 1.0)
WIDTH, BLEND = 640, 96


def main():
    OUTPUT.mkdir(exist_ok=True)
    art = Image.open(SOURCE).convert("RGBA")
    # The source's circular seal is centered at (210,355). The stream crop keeps
    # the open central channel and both knotwork ribbons, excluding the seal/tip.
    seal = art.crop((0, 140, 430, 570))
    pixels = np.array(seal)
    fade = np.clip((430 - np.arange(430)) / 80, 0, 1)
    pixels[:, :, 3] = (pixels[:, :, 3] * fade[None, :]).astype(np.uint8)
    seal = Image.fromarray(pixels)
    base = art.crop((440, 150, 2020, 560)).resize((WIDTH + BLEND, 160), Image.Resampling.LANCZOS)
    # Premultiplied wrap blending preserves translucent cyan edges without dark
    # seams. Last and first tile pixels meet at neighboring source coordinates.
    pixels = np.asarray(base, dtype=np.float32) / 255
    pixels[:, :, :3] *= pixels[:, :, 3:4]
    body, tail = pixels[:, :WIDTH].copy(), pixels[:, WIDTH:]
    ramp = np.linspace(0, 1, BLEND)[None, :, None]
    body[:, :BLEND] = body[:, :BLEND] * ramp + tail * (1 - ramp)
    body[:, :, :3] /= np.maximum(body[:, :, 3:4], 1 / 255)
    loop = Image.fromarray(np.clip(body * 255, 0, 255).astype(np.uint8))
    for height in HEIGHTS:
        edge = np.zeros((height, 64, 4), dtype=np.uint8)
        edge[:, :, :3] = (35, 37, 43)
        edge[:, :, 3] = np.round(np.linspace(0, 1, 64) ** 1.6 * 255).astype(np.uint8)
        Image.fromarray(edge).save(OUTPUT / f"edge_H{height}.png", optimize=True)
        head = np.zeros((height, height, 4), dtype=np.uint8)
        head[:, :, :3] = (35, 37, 43)
        reveal = np.clip((np.arange(height) / height - .55) / .40, 0, 1)
        head[:, :, 3] = np.round((1 - reveal) * 255).astype(np.uint8)
        Image.fromarray(head).save(OUTPUT / f"head_H{height}.png", optimize=True)
        for level, amplitude in enumerate(AMPLITUDES):
            image = loop.resize((WIDTH, max(8, round(height * amplitude))), Image.Resampling.LANCZOS)
            alpha = image.getchannel("A").point(lambda value: round(value * (.60 + .40 * level / 6)))
            image.putalpha(alpha)
            canvas = Image.new("RGBA", (WIDTH, height))
            canvas.paste(image, (0, (height - image.height) // 2))
            canvas.save(OUTPUT / f"stream_H{height}_L{level}.png", optimize=True)
        for level, amplitude in enumerate((.80, .90, 1.0)):
            size = round(height * amplitude)
            image = seal.resize((size, size), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (height, height))
            canvas.paste(image, ((height - size) // 2, (height - size) // 2))
            canvas.save(OUTPUT / f"source_H{height}_L{level}.png", optimize=True)
    (OUTPUT / "manifest.json").write_text(json.dumps({
        "heights": HEIGHTS, "levels": len(AMPLITUDES), "width": WIDTH,
        "source": "../wave-hd-source.png", "style": "approved-blue-knotwork",
    }, indent=2) + "\n", encoding="utf-8")
    print(f"Prepared {len(HEIGHTS) * (len(AMPLITUDES) + 5)} antialiased sprites")


if __name__ == "__main__":
    main()
