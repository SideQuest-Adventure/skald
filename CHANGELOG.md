# Changelog

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
