# Blue knotwork stream

Approved direction: 2026-11-13. Refined icy-blue ribbons, woven Norse knotwork,
a circular source seal, and a separate, voice-driven string of gold runes.

Source artwork: `../wave-hd-source.png`. Generated with the built-in ImageGen tool
from the owner-approved mockup. This source contains no baked-in gold runes.
Runtime sprites and `manifest.json` live in this directory. Rebuild with
`python tools/gen_wave_hd.py` from the repository root; Pillow is build-time only.

The runtime uses native-size antialiased sprites at six heights, continuous
time-based horizontal movement, and seven voice-amplitude levels. Gold runes
remain Tk text. They appear only while speech is heard and never accumulate into
a delayed burst. Source and edge masks blend the ribbons into the charcoal UI.

## Generation prompt

Precise production-asset edit of the supplied approved Skald blue magical stream
mockup. Preserve its refined icy blue/cyan/sapphire ribbons, woven Norse knotwork
embedded in the ribbons, luminous white-blue center filament, and the intricate
circular knotwork source with a small starburst at the left. Remove ALL gold runes,
gold letters, gold shapes and their gold glows entirely: the application will
animate them separately, so the blue center must be clear and open. Remove the
charcoal background to actual transparent alpha, preserving delicate blue glows as
semitransparent pixels. Do not replace with black or a checkerboard. Keep the
visual direction and thin smooth high-resolution filament detail exactly. Long
wide horizontal isolated effect, circular source placed at approximately 10%
image width, ribbons flow toward the right across the image. The source ring must
be round, not elliptical. Keep the energy flowing and balanced, with good negative
space along the center line for runtime gold runes, the top/bottom luminous blue
ribbons forming a gently undulating frame around that center. Keep the full
starburst and glows inside canvas bounds, with modest transparent safety margins.
This is the final clean blue-only artwork for a desktop app, not another concept
board. No text, runes, UI, captions, border, watermark, background or new objects.
Preserve the approved artwork style; no coarse pixel blocks.
