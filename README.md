<p align="center"><img src="assets/skald-hero.png" alt="Pixel art of a Norse drinking horn on carved stone, with a stream of blue voice-light turning into golden runes" width="420" height="420"></p>

# Skald

Speak freely. Shape your draft. Keep your words.

Local speech-to-text for Windows, with a Norse overlay and optional AMD, NVIDIA,
and Intel GPU acceleration through Vulkan. No account, subscription, paid API,
or application telemetry. Speech models download once before offline use.

**Version 1.4.0.** Download the latest source from
[GitHub Releases](https://github.com/SideQuest-Adventure/skald/releases/latest).

## Start

Requires Windows 10/11, Python 3.11+, and a microphone.

```bat
git clone https://github.com/SideQuest-Adventure/skald.git
cd skald
install.bat
run.bat
```

The installer creates `.venv` inside Skald and installs `requirements.txt` there.
The launcher finds Python automatically and prefers this environment. If a command
fails, installation stops and displays the error. Manual installation remains
`python -m pip install -r requirements.txt`.

Tap **Right Ctrl** to record, speak, and tap again to finish. Or use **Record** and
**Finish** on the overlay. **Pause/Resume** lets you stop to think without finishing.
**Cancel** prevents delivery. A rising horn starts recording; a falling horn stops it.
The microphone stream stays open while Skald runs for device reliability; audio is
buffered for transcription only during recording and discarded while paused or idle.

Blue knotwork ribbons glide behind a steady string of gold runes that follows your
voice. The circular source glows as you speak; the artwork stays sharp as you resize.

Skald stops after **18 seconds of silence** by default. Change that in Settings;
`0` disables automatic stopping. This is separate from the short pauses that stream
words into your draft.

## Make the words usable

Click the mode button to cycle through these modes, or choose one in **Settings**.

| Mode | Behavior |
|---|---|
| Exact | Keeps recognized wording, with your explicit vocabulary corrections and edits. |
| Clean | Removes English hesitation sounds such as um, uh, and erm; tidies spacing. |
| Bullets | Adds a bullet to each nonempty line. Use spoken line breaks to separate items. |

Clean preserves repeated words, false starts, names, numbers, negations, and words
such as "like" and "actually". It does not infer what you meant. Non-English and
auto-detected language sessions skip English filler removal. Bullets does not
summarize, reorder, or split guessed sentences.

### Personal vocabulary

In **Settings > Vocabulary**, enter one correction per line:

```text
skull = Skald
```

Corrections match whole terms, ignoring case. Longer phrases win and replacements
do not cascade into other replacements. They change the output, not the saved
original speech. In **Review**, fill in **Heard** and **Use**, then click
**Remember spelling** to update the draft and remember the correction for later.

### Spoken editing

Say commands as their own phrases, with a short pause before and after:

| Say | Result in the current draft |
|---|---|
| skald new line | Start a new line. |
| skald new paragraph | Start a new paragraph. |
| skald next bullet | Start a new line for Bullets mode. |
| skald period / comma / question mark | Insert punctuation. |
| skald exclamation mark / colon / semicolon | Insert punctuation. |
| skald scratch that / undo | Undo the last phrase or edit. |
| skald replace Tuesday with Thursday | Replace the last whole-term match. |
| skald insert follow up | Insert a saved text shortcut. |
| skald cancel | Cancel this recording without pasting. |

These edits operate on **the current recording**. They do not delete or change text
already pasted into another app. Ordinary sentences such as "Skald is a Norse bard"
remain dictation. Disable spoken editing in Settings when dictating the commands
literally. A final accuracy pass cannot overwrite your edits.

In **Settings > Shortcuts**, define entries such as:

```text
follow up = Thanks for your time.\nHere are the next steps.
```

`\n` creates a line break. Shortcuts insert text, without executing it.

## Brain-dump mode

Enable **Brain-dump mode: review before pasting** in Settings, or launch:

```bat
run.bat --review
```

Talk, pause, resume, and finish. Review opens with an editable **Draft** and a separate
**Original speech** tab. **Save draft** updates the draft; **Copy draft** copies it so
you can choose your destination yourself. Review never auto-pastes or sends a message.
You can open the latest draft with the overlay's **Review** button at any time after
recording finishes.

## History and privacy

**History** searches both original speech and final drafts. Double-click an entry to
review or edit it. Pin important entries so retention leaves them alone. Delete an
entry to remove it from Skald's recovery database.

Each recognized phrase is journaled while recording. Final output is saved before
paste, including GPU-refined words. Closing Skald normally finishes pending speech
and saves it without initiating a paste. Abrupt termination can still lose audio
that has not yet been transcribed.

Defaults:

- Settings: `%LOCALAPPDATA%\Skald\skald.toml`.
- History: `%LOCALAPPDATA%\Skald\history.sqlite3`, containing plain text, not audio.
- Unpinned history expires after 30 days, checked at startup. Set retention to `0`
  to keep it indefinitely.
- Saving is on by default, visible on the overlay. Turn it off in Settings or pass
  `--no-save` to stop automatic dictation writes for that run.
- Turning saving off does not erase earlier entries. Cancel prevents delivery but
  retains already-journaled original speech when saving is on.

Older `Documents\Skald Transcripts\skald-YYYY-MM-DD.md` files remain untouched.
The new History window searches the SQLite journal, not those older files. Review
can copy older recovered speech from the database. Settings and window position
are saved separately from dictation history.

Automatic paste checks the foreground **window** against the one used when recording
started. It holds the draft for Review/Copy if the window changed. It cannot verify
a particular field or browser tab. Use review-before-paste when the destination matters.

With clipboard preservation enabled, automatic paste restores earlier plain text
if you have not copied something newer. Rich text, images, copied files, and unknown
formats are left untouched; Skald inserts Unicode text directly into the same window.
Explicit **Copy** replaces the clipboard. You can disable preservation in Settings
to use the old paste behavior.
Windows clipboard history and synced folders have their own retention policies.

App launch/search/send commands are **off by default**, separately from draft editing.
The advanced Controls option enables them. They may open applications, perform a web
search, or paste and press Enter; enable them only if you want those actions.

## Settings and app profiles

Use **Settings** to choose your mode, vocabulary, shortcuts, hotkey, microphone,
model, language, saving, and prolonged-silence timeout. Microphone and model changes
require a restart. Choose a named hotkey such as `ctrl_r` or `f8`, or a single character.

In **App profiles**, optionally set a writing mode per process:

```text
notepad.exe = clean
```

Profiles inspect only the foreground executable's name at record start. They do not
read document content, window titles, or browser pages. Other apps use your default.

For a separate configuration, copy [skald.example.toml](skald.example.toml) to
`skald.toml`, edit it, and launch `run.bat --config skald.toml`. History and window
position then live beside that file. Keep that folder private. CLI overrides apply
to the current run; explicitly saving settings in the UI persists the displayed choices.

Useful commands:

```bat
run.bat --mode clean
run.bat --review --no-save
run.bat --list
run.bat --doctor
run.bat --test
run.bat --demo
run-classic.bat
```

Demo uses synthetic text and isolated temporary data, without opening the mic,
downloading a model, or automatically pasting. Classic records while the hotkey is
held and has no review window; review-only drafts are held in History instead.
With saving off, a held classic draft is printed in the console for manual recovery.

## GPU acceleration

By default Skald looks for a local whisper.cpp ASR server at `http://127.0.0.1:5075`.
It uses the server if available and falls back to faster-whisper on CPU otherwise.

1. Get [koboldcpp](https://github.com/LostRuins/koboldcpp/releases) with Vulkan support
   and place `koboldcpp-nocuda.exe` in `engines\` beside `skald.py`.
2. Place `ggml-large-v3-turbo-q5_0.bin` in the same folder.
3. Launch Skald. It starts the server if these files exist and reuses an existing server.

The server stays running after Skald exits so its model can remain warm. CPU mode
chooses a model by core count; explicit model selection overrides that choice. Speed
and accuracy depend on your hardware, microphone, language, and model. Localhost is
the default; manually changing `ASR_SERVER_URL` to another host sends audio there.

## Development

```bat
python -m pytest tests -q
```

Tests use synthetic audio and fake delivery. They cover writing behavior, settings,
recovery, cancellation, long sessions, shutdown, and clipboard/window guards. See
[TEST-LOG.md](TEST-LOG.md) for results and the remaining live-verification limits.

## License

MIT. See [LICENSE](LICENSE). From [SideQuest Adventure](https://sidequestadventure.com).
