"""Antialiased blue-ribbon artwork with continuous, time-based canvas motion."""
import json
from pathlib import Path
import tkinter as tk


class RibbonFrames:
    def __init__(self, directory):
        self.directory = Path(directory)
        manifest = json.loads((self.directory / "manifest.json").read_text(encoding="utf-8"))
        self.heights = manifest["heights"]
        self.levels, self.width = manifest["levels"], manifest["width"]
        self.cache = {}
        self.offset = 0.0

    def _bucket(self, height):
        size = min(self.heights, key=lambda value: abs(value - height))
        if size not in self.cache:
            self.cache[size] = (
                [tk.PhotoImage(file=str(self.directory / f"stream_H{size}_L{level}.png"))
                 for level in range(self.levels)],
                [tk.PhotoImage(file=str(self.directory / f"source_H{size}_L{level}.png"))
                 for level in range(3)],
                tk.PhotoImage(file=str(self.directory / f"edge_H{size}.png")),
                tk.PhotoImage(file=str(self.directory / f"head_H{size}.png")),
            )
            # Keep only the active and previous resize buckets.
            if len(self.cache) > 2:
                del self.cache[next(iter(self.cache))]
        return size, self.cache[size]

    def draw(self, canvas, width, height, level, dt, background):
        size, (streams, sources, edge, head) = self._bucket(height)
        mid = height / 2
        voice_level = min(self.levels - 1, round(level * (self.levels - 1)))
        source_level = 0 if level < .12 else (1 if level < .55 else 2)
        # Move the same sharp tile continuously instead of jumping among widely
        # separated animation frames. Source seal stays anchored at the left.
        self.offset = (self.offset + (24 + 40 * level) * dt) % self.width
        start = size - 18
        x = start - self.width + self.offset
        while x < width:
            canvas.create_image(round(x), mid, anchor="w", image=streams[voice_level])
            x += self.width
        canvas.create_image(0, mid, anchor="w", image=head)
        canvas.create_image(0, mid, anchor="w", image=sources[source_level])
        canvas.create_image(width - 64, mid, anchor="w", image=edge)
        return float(size - 8)
