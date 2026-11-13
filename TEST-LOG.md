# Skald — Visual & Manual TEST LOG

> The dated record of what has and hasn't been tested here. Output (heavy renders/screenshots)
> stays gitignored; THIS log is committed. Every entry LEADS WITH THE TYPE IN CAPS and ends with a
> **VERDICT** line. Law: `command_center/.claude/skills/artifact-discipline.md` (memory
> feedback-artifact-discipline). Rolled out 2026-07-17.
>
> Entry template:
> ```
> ## TYPE · short-title · YYYY-MM-DD
>   varied:  the controlled variable / what changed
>   output:  path (if any) · sheet: _contact_sheet.png (for visual tests)
>   result:  N tests · pass/fail
>   VERDICT: ⟨pending | conclusion⟩
> ```

## Test surface — computed 2026-07-17 (factual inventory, NOT a pass claim)
  runner:  pytest
  run:     `python -m pytest`
  test files (2):
    - tests/test_hardware.py
    - tests/test_skald.py
  Last full-suite result: **NOT CAPTURED** — run the suite and log the first entry below.

---

## Log (newest first)

## RELEASE · 1.4.0 · 2026-11-13
  authorization: owner requested uploading the current Skald version to GitHub
  result: **102 passed in 4.82s** with Python 3.14, repository-local test temp
  checks: all seven runtime modules and HD artwork builder compile; PowerShell
    launcher --help succeeds; git diff --check passes; publication text scan found
    no credential-pattern or user-home-path matches
  contents: writing tools/settings/history, recording/display/delivery repairs,
    approved antialiased blue-knotwork artwork and continuous live rune stream
  exclusions: personal overlay_state.json edits, local QA, engine binaries/models,
    private settings/history and gitignored handoff remain outside the release
  limits: no new live microphone/desktop testing; existing owner paste confirmation
    and isolated native delivery evidence are recorded below. Clean-machine install
    and additional hardware verification are not claimed.
  VERDICT: release checks PASS; 1.4.0 prepared for owner-authorized publication

## VISUAL/REGRESSION · approved blue-knotwork stream · 2026-11-13
  art: built-in ImageGen edit of owner-approved mockup; transparent blue-only
    source assets/wave-hd-source.png, provenance/prompt assets/wave-hd/README.md
  implementation: native-size antialiased sprites, continuous rightward movement,
    fixed circular source, voice-reactive amplitude, separate evenly spaced runes
  automated: **102 passed in 5.72s** after renderer integration; no dependency added
  asset preparation: 72 sprites at six sizes; no nearest-neighbor enlargement in
    the HD path, bounded two-size image cache, original sprite fallback retained
  visual output: qa/wave-hd-preview.png, offline 440x80 and 720x128 rendering of
    actual drawing functions. Inspected and softened source/ribbon join. Final
    mask update executed successfully through this offline preview afterward.
  live validation: owner handles testing; no desktop automation or forced restart
  VERDICT: implemented and offline-rendered; live visual acceptance pending owner

## REGRESSION · frozen overlay and blocked delivery · 2026-11-12
  cause: missing queue import made the first empty UI poll raise NameError, which
    the broad exception handler hid; rich clipboard preservation also refused delivery
  fixes: restored queue import, narrowed teardown handling, native UTF-16 delivery
    for rich clipboards; rune/gold controls and stronger voice-reactive rune stream
  automated: **102 passed in 4.16s**, including empty-queue rearming, next status,
    Unicode/surrogate delivery, cancellation, focus changes, blocked native input
  native: isolated Windows text field received Unicode and actual local-GPU ASR
    output through WritingSession; status pasted, clipboard sequence unchanged,
    updated transcript/status visible, 61 rune particles recorded
  limitation: native check revealed CR handling; normalization changed to LF after
    the check. Final teardown exception narrowing also followed the suite run.
    Owner explicitly took over further testing; no additional live testing performed.
  VERDICT: frozen-display regression fixed; native delivery demonstrated; real-mic
    and final visual acceptance left to owner as requested

## AUTOMATED · v1.4 writing and recovery regression suite · 2026-11-09
  varied: vocabulary, editing, modes, settings, SQLite recovery, recording lifecycle,
    delivery guards, classic fallback, and Windows launch discovery
  runner: installed Python 3.14; `-m pytest tests -q --tb=short --basetemp=qa/test-run-06`
  result: **97 passed in 6.05s**
  checks: py_compile passed for all six runtime modules; git diff --check passed
    (Git emitted only its existing LF-to-CRLF conversion notices)
  launcher: PowerShell launcher --help completed with exit 0 after Python discovery
    was corrected to handle a redirected LOCALAPPDATA environment
  coverage: literal vocabulary and remembered corrections; undo/replace/paragraphs;
    meaning-sensitive formatting; settings validation and atomic saves; Unicode history
    search, pinning, retention, concurrency and deletion; synthetic audio through record,
    pause, resume, stop, cancellation, shutdown and finalization; no-save behavior;
    refinement preserving edits and all long-session text; cancellation before injection;
    plain-text clipboard restoration, newer user copies, rich-format holds and window
    changes (native delivery mocked); invalid CLI limits rejected before file writes
  iteration record: initial temp-fixture runs reported 61 passed / 24 setup errors
    because the sandbox temp directory was unavailable, then because qa did not yet
    exist. After creating repository-local qa, development runs passed 85 (1.66s),
    85 (1.66s), 92 (2.00s), and 96 (5.99s); final result above includes subsequent fixes.
  deviation: new recovery writes use SQLite rather than duplicating daily Markdown
    files, so original/final text can be searched, edited, pinned and deleted together.
    Earlier Markdown archives are untouched. Clean is conservative filler removal,
    not semantic rewriting. No new runtime dependencies, cloud APIs, push, or release.
  VERDICT: automated regression suite PASS; release remains pending live verification

## MANUAL · native desktop verification interrupted · 2026-11-09
  varied: synthetic --demo preview with isolated temporary settings/history, no mic,
    model download, or automatic paste
  tool: computer-use skill, node_repl + @oai/sky
  result: initial app approval/capture timed out; subsequent capture reported a
    minimized window and then no screenshot targets for the frameless process.
    Demo was adjusted to use a normal taskbar window. The next desktop request
    reported that the user stopped Computer Use with physical Escape.
  action: no further Computer Use calls after that stop; continued only code checks
    and repository documentation. No screenshot or taste approval was obtained.
  remaining: actual overlay/dialog layout, keyboard navigation, real microphone and
    CPU/GPU speech accuracy, native clipboard formats/window switching, clean install,
    and packaged/signed distribution
  VERDICT: UNVERIFIED; not a visual or end-to-end desktop pass

## AUTOMATED · existing deterministic-core suite · 2026-11-09
  varied: no source changes; baseline for owner-requested product review
  runner: installed Python 3.14 interpreter, `-m pytest tests -q`
  result: 27 passed in 2.72s
  scope: command parsing, audio helpers, hardware selection, horn generation
  limitation: no live microphone, target-app paste, shutdown recovery, or clean-install verification
  VERDICT: existing unit suite passes; end-to-end dictation behavior remains unverified in this review
