<p align="center"><img src="assets/skald-hero.png" alt="Pixel art of a Norse drinking horn on carved stone, with a stream of blue voice-light turning into golden runes" width="420" height="420"></p>

# Skald

Hold a key, speak, and it is written.

Skald is local push-to-talk speech-to-text for Windows. Tap a hotkey, talk, and your words are transcribed on your own machine and pasted into whatever window you are working in. No cloud, no API keys, no subscription, no telemetry. The skalds of the north turned spoken deeds into written saga; this one does it for your rambling first drafts, commit messages, and Discord replies.

## What it does

- **Tap to talk, live.** Tap Right Ctrl to start. A small floating overlay shows your words as you speak, with a live level meter and timer. Tap again to stop; the text lands in your active window instantly.
- **Or hold to talk.** Prefer the old way? `run-classic.bat` gives you hold-to-record, release-to-paste, no window at all.
- **Everything stays on your machine.** Transcription runs locally on [faster-whisper](https://github.com/SYSTRAN/faster-whisper). Your voice never leaves the computer.
- **Never lose a thought.** Every phrase is also appended to a dated transcript file the moment it is heard, so a misplaced click or a crash cannot eat your brainstorm. Old transcripts prune themselves after 30 days (configurable, or turn it off).
- **Spoken commands, if you want them.** Say the word "skald" before a command ("skald search kettlebell form", "skald open chrome") and it acts instead of typing. Fully local, fully deterministic, off by default until you learn it.
- **Chimes you can trust.** A rising two-note when the mic opens, a falling one when it closes. Your ears always know the state. Silence with `--no-chime`.

## Requirements

- Windows 10 or 11
- Python 3.11+
- A microphone

A GPU is not required. On CPU, model size is the main speed lever; see the table below. If you do have a GPU, including an AMD one, see GPU acceleration below: Skald can ride a local whisper.cpp Vulkan server for large-model accuracy at real-time speed.

## Install

```bat
git clone https://github.com/SideQuest-Adventure/skald.git
cd skald
install.bat
```

Or by hand: `pip install -r requirements.txt`

Then check your setup:

```bat
python skald.py --doctor
```

Doctor verifies your Python version, every dependency, your microphone, the model cache, and the clipboard, and tells you exactly what is missing if anything is.

## Run

```bat
run.bat            (live overlay mode)
run-classic.bat    (hold-to-talk, console only)
python skald.py --list    (list audio devices)
```

The first run downloads the Whisper model to your cache directory and warms it up, so the first real sentence is fast.

## Choosing a model

Set `MODEL_SIZE` in the CONFIG block at the top of `skald.py`. On CPU this is the speed and accuracy dial:

| Model | Speed on CPU | Accuracy | Good for |
|---|---|---|---|
| `tiny.en` | fastest | drops tricky words | quick notes |
| `base.en` | fast | solid everyday accuracy | the default |
| `small.en` | moderate | noticeably better | dictating documents |
| `medium.en` | slower | strong | when accuracy matters most |

Other useful CONFIG dials: `HOTKEY`, `SILENCE_THRESHOLD`, `MAX_RECORD_SECONDS`, `SAVE_TRANSCRIPTS`, `TRANSCRIPT_RETENTION_DAYS`, `CHIME`, `LANGUAGE`.

## GPU acceleration (optional, works on AMD)

Skald's `ASR_BACKEND` is `auto`: on every launch it looks for a local whisper.cpp ASR server at `http://127.0.0.1:5075` and uses it when present, which moves transcription to your GPU through Vulkan. Vulkan means this works on AMD and Intel cards, not just NVIDIA. No server found, no problem: Skald quietly falls back to faster-whisper on CPU.

To set it up:

1. Download [koboldcpp](https://github.com/LostRuins/koboldcpp/releases) (`koboldcpp-nocuda.exe`, it embeds whisper.cpp with the Vulkan backend) into an `engines\` folder next to `skald.py`.
2. Download a whisper GGML model, for example `ggml-large-v3-turbo-q5_0.bin`, into the same folder.
3. That is all. Skald auto-starts the server when it launches and reuses it if it is already running. Point `ASR_SERVER_URL` at any other machine on your network if you keep the GPU elsewhere.

With a mid-range GPU this runs the large-v3-turbo model faster than small models run on CPU, which is the best accuracy-per-second available anywhere in local dictation.

## Transcripts

Every dictated phrase is saved to `~/Documents/Skald Transcripts/skald-YYYY-MM-DD.md` with a timestamp, one rolling file per day, independent of where the paste lands. Run with `--no-save` to disable for a session, or set `SAVE_TRANSCRIPTS = False` to disable permanently.

## Troubleshooting

Run `python skald.py --doctor` first; it catches almost everything. The most common fix after that: if the wrong microphone is being used, run `python skald.py --list` and set `DEVICE` in CONFIG to the right index.

## License

MIT. See [LICENSE](LICENSE). From the [SideQuest Adventure](https://sidequestadventure.com) workshop.
