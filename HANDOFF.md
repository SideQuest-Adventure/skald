# Skald — HANDOFF (written 2026-07-10, mid-spin-out)

Skald = the public, brand-published version of the hub's voice_commander (Norse: the bard
who turned speech into saga). Push-to-talk local dictation, faster-whisper, Windows.
Home: this sibling repo. Publishes to github.com/SideQuest-Adventure/skald (org identity,
same private-push-then-flip-public flow used for SideQuest-Adventure/laconic).

## State at handoff

- **Source of truth for the core**: a web-engineer agent was spun out in the prior session
  to produce `skald.py` from `command_center/projects/voice_commander/voice_commander.py`
  (READ-ONLY source; hub keeps its full private version). Its contract, all three parts:
  1. STRIP empire wiring: heartbeat.py (HUD), agent_brain.py (LLM intents), speaker.py +
     voice_lines.py (persona TTS), every Commander/Bastion/empire string. KEEP: live overlay
     mode (persistent single InputStream design), classic mode, transcripts + retention,
     chimes, --list, deterministic spoken commands with prefix renamed "skald".
  2. ADD --doctor (python version, deps, mic, model cache writable, clipboard round-trip).
  3. NORSE OVERLAY THEME (owner spec): charcoal #23252B bg, thin muted-gold #8A6A2F border;
     waveform level meter = center-mirrored bars, ICE-BLUE glow stack (#1E4A66/#3E7FA8/#8FD4F5,
     default THEME_ACCENT="ice"; "amber" alt #6B4E1F/#B07E2E/#F5C879); GOLD #E2A84E for runic
     title "ᛋᚲᚨᛚᛞ SKALD", the "᛭" phrase dividers, and the ✕ close (hover brighten);
     drag-move kept, resize grip added, 30fps cap, audio thread never touches canvas;
     root.iconbitmap(assets/skald.ico).
  If the agent's output was lost, re-run that surgery from this spec.
- **DONE in repo**: README.md (stranger-facing, hero image wired) · LICENSE (MIT, Side Quest
  Adventure LLC) · .gitignore · assets/ = skald.ico + skald-512/256.png (cut from
  Skald-Pixel-source.png, Rath's pick: PIXEL art is THE icon) + skald-hero.jpg (painterly
  version, README hero) + both source PNGs.
- **NOT done**: tests ported (agent contract includes tests/test_skald.py) · gates run
  (AI-tell grep, py_compile, pytest) · git init + brand-authored history
  ("SideQuest Adventure <ops@sidequestadventure.com>") · repo creation + publish + fresh-config
  install verify · project-index/memory updates.

## v1.1 roadmap (Rath-ranked, with his corrections)

1. Settings file (skald.toml) replacing in-code CONFIG edits.
2. System tray (pystray; pixel-horn icon; pause/classic/quit).
3. Spoken editing commands ("skald new line", "skald scratch that").
4. Personal dictionary (user corrections file applied post-transcription).
5. GPU auto-detect (CUDA silently if present) — **and AMD-readiness is a Rath priority:**
   investigate whisper.cpp Vulkan backend as an optional engine (also benefits the empire
   tower's RX 7900 GRE; see hub memory lesson-voice-commander-gpu).
6. VAD auto-stop: **Rath's numbers — a PROLONGED pause only: default ~15-20s, never 2s.**
   Configurable AUTO_STOP_SILENCE seconds, 0 = off.

## Publish flow (repeat of laconic's)

fresh git history authored as the brand -> `gh repo create SideQuest-Adventure/skald --private`
-> push -> verify -> `gh repo edit --visibility public --accept-visibility-change-consequences`
-> description/topics/homepage (sidequestadventure.com) -> fresh CLAUDE_CONFIG_DIR-style
stranger test (clone + install.bat + --doctor) -> announce stays OFF until Rath says.

## Laws that bind this repo

No AI-tells in any user-facing string (no em-dash/ellipsis/arrows) · visuals only from the
paid pipeline or Rath-supplied art (icon = his) · Python (house lane) · deploy/publish gates
human-approved · never commit secrets · hub's voice_commander stays private and untouched.
