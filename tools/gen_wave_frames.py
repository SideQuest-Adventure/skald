"""Build the overlay's sprite-wave frame bank from assets/wave-keyed.png.

Output (all shipped, so end users need no imaging deps at runtime):
  assets/wave/star_{0,1,2}.png       the voice-source starburst at 3 pulse sizes
  assets/wave/stream_L{0-4}_P{0-7}.png   the flowing wave: 5 amplitude levels x 8 scroll
                                          phases, horizontally wrap-blended so the loop
                                          is seamless

Run from the repo root:  python tools/gen_wave_frames.py
"""
import os
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(HERE, "assets", "wave-keyed.png")
OUT = os.path.join(HERE, "assets", "wave")

BAR_H = 40          # overlay waveform canvas height
STREAM_W = 900      # rendered stream width; the overlay tiles it
STAR_SPLIT = 430    # x where the starburst ends and the stream begins (source px)
LEVELS = [0.30, 0.48, 0.65, 0.82, 1.00]
PHASES = 8
BLEND = 60          # px of wrap crossfade so scrolling loops without a seam


def main():
    os.makedirs(OUT, exist_ok=True)
    art = Image.open(SRC).convert("RGBA")
    w, h = art.size

    star = art.crop((0, 0, STAR_SPLIT, h))
    stream = art.crop((STAR_SPLIT, 0, w, h))

    # Starburst at three pulse sizes (quiet, speaking, loud).
    for i, sh in enumerate((int(BAR_H * 0.8), BAR_H, int(BAR_H * 1.15))):
        s = star.resize((int(star.width * sh / star.height), sh), Image.LANCZOS)
        canvas = Image.new("RGBA", (s.width, int(BAR_H * 1.15)), (0, 0, 0, 0))
        canvas.paste(s, (0, (canvas.height - sh) // 2), s)
        canvas.save(os.path.join(OUT, f"star_{i}.png"))

    # Make the stream horizontally loopable once, at full height.
    base = stream.resize((STREAM_W + BLEND, h), Image.LANCZOS)
    arr = np.asarray(base).astype(np.float32)
    body, tail = arr[:, :STREAM_W], arr[:, STREAM_W:]
    ramp = np.linspace(0.0, 1.0, BLEND)[None, :, None]
    body[:, :BLEND] = body[:, :BLEND] * ramp + tail * (1.0 - ramp)
    loop = Image.fromarray(body.astype(np.uint8), "RGBA")

    for li, amp in enumerate(LEVELS):
        ch = max(6, int(BAR_H * amp))
        squashed = loop.resize((STREAM_W, ch), Image.LANCZOS)
        for p in range(PHASES):
            off = int(STREAM_W * p / PHASES)
            rolled = np.roll(np.asarray(squashed), -off, axis=1)
            frame = Image.new("RGBA", (STREAM_W, BAR_H), (0, 0, 0, 0))
            frame.paste(Image.fromarray(rolled, "RGBA"), (0, (BAR_H - ch) // 2))
            frame.save(os.path.join(OUT, f"stream_L{li}_P{p}.png"))

    total = sum(os.path.getsize(os.path.join(OUT, f)) for f in os.listdir(OUT))
    print(f"frame bank: {len(os.listdir(OUT))} files, {total // 1024} KB -> {OUT}")


if __name__ == "__main__":
    main()
