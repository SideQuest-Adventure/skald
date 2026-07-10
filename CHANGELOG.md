# Changelog

## 1.3.0

- Hardware auto-detect: Skald now reads your GPU and CPU itself, so you don't need to
  know whether your card is NVIDIA (CUDA) or AMD (Vulkan) or what a Whisper model is.
  `--doctor` names every GPU in plain words (discrete and Vulkan-capable vs integrated),
  shows your CPU core count, and says which engine fits this machine. If you have a
  capable GPU but no server set up, startup tells you the one download that unlocks
  30-70x realtime; integrated-only boxes are told the CPU engine is the right call.
- Model sizes default to `auto`: 8+ CPU cores get small.en, 4-7 get base.en, weaker
  machines get tiny.en, for both classic and live mode (and the GPU-death fallback),
  so push-to-talk stays snappy on a budget laptop out of the box. `--model` still
  overrides everything.
- The war horn is softened: a darker harmonic mix whose brightness decays over the
  note like a real horn losing its edge, a slower breathier swell, a longer natural
  tail, and a lower default volume. Rising = mic open, falling = mic closed, unchanged.

## 1.2.3

- The chimes are now a synthesized Viking war horn: a rising horn call means the mic is
  OPEN and listening, a shorter falling blast means it is CLOSED. The two cues differ in
  length, direction, and register so the state is never in doubt.

## 1.2.2

- More power: full voice now gusts up to two runes per tick, holds up to 20 in flight,
  and loud runes render larger. Quiet speech stays sparse, as it should.

## 1.2.1

- The wave grows with the window: the canvas takes about a fifth of the window height
  (40 to 160 px), and the art thickens through cached integer zoom, so stretching the
  overlay taller shows a bigger, more detailed current. The starburst and rune lane
  scale with it.

## 1.2.0

The wave is now the owner's own soundwave art, brought to life.

- The overlay level meter is a sprite wave: a starburst voice-source that breathes with
  your volume, and a blue rune-stream that scrolls rightward, swelling from a calm line
  in silence to the full braided current when you speak (5 amplitude levels x 8 scroll
  phases, pre-rendered frame bank in assets/wave, regenerable via tools/gen_wave_frames.py).
- Gold Elder Futhark runes spawn off the starburst while you talk and ride the current,
  aging from bright gold to ember before they fade.
- If the frame bank is missing the overlay quietly falls back to the bar meter.

## 1.1.1

Overlay polish from first field use.

- The waveform now builds to the RIGHT: new sound enters at the left and history flows
  rightward, like a line being written.
- Molten-gold waveform is the new default (THEME_ACCENT "gold"); ice blue and amber
  remain one CONFIG word away. Chrome gold is now a true gold rather than orange.
- The transcript flows as one continuous passage; the per-phrase rune dividers are gone.
- Transcript face is now Palatino at 12pt for a printed-saga feel.

## 1.1.0

- `--doctor` now reports the ASR engine that will actually carry your voice: whether a
  local GPU server is reachable, whether Skald can auto-start one from `engines\`, or
  which faster-whisper CPU model it will fall back to, with a pointer to the GPU setup.
- README documents GPU acceleration through a local whisper.cpp Vulkan server, which
  works on AMD and Intel GPUs as well as NVIDIA. Skald has probed for this server since
  1.0.0 (`ASR_BACKEND: auto`); now the docs say so honestly.

## 1.0.0

First public release.

- Push-to-talk speech-to-text for Windows: tap Right Ctrl, speak, tap again, and the text
  pastes into your active window.
- Live floating overlay with a voice-reactive waveform, runic styling, drag, and resize.
- Classic hold-to-talk mode (`run-classic.bat`).
- Dated transcripts saved as you speak, with retention pruning.
- Spoken commands behind the "skald" prefix (open, search, dictate, help).
- `--doctor` self-diagnostic and `--list` device picker.
- Local ASR: whisper.cpp Vulkan server when available, faster-whisper CPU otherwise.
