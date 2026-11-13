"""End-to-end recording lifecycle with synthetic audio and fake inference/delivery."""
from dataclasses import replace
from threading import Event
from unittest.mock import Mock

import numpy as np
import pytest

from skald_history import History
from skald_session import Run, WritingSession
from skald_writing import Settings


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr("skald_session.foreground_window", lambda: 123)
    monkeypatch.setattr("skald_session.foreground_process", lambda: "notepad.exe")
    return WritingSession(None, None, 1000, settings=Settings(),
        config={"HOTKEY": "key", "SAVE_TRANSCRIPTS": True, "MAX_RECORD_SECONDS": 600,
                "STREAM_SPEECH_RMS": .006, "PHRASE_MIN_MS": 250,
                "PHRASE_SILENCE_MS": 700, "PHRASE_MAX_SECONDS": 12,
                "FINAL_MODEL": None},
        history=History(tmp_path / "history.sqlite3"),
        transcribe=Mock(return_value="Draft text."), resample=lambda a, sr: a,
        gain=lambda a: a, refine_available=lambda: False,
        paste=Mock(return_value=True), chime=Mock(), route_command=Mock(return_value=False))


def feed(session, value=.1, samples=500):
    audio = np.full((samples, 1), value, dtype=np.float32)
    session.audio_cb(audio, samples, None, None)


def finish(session):
    session._stop()
    session.worker.join(timeout=5)
    assert not session.busy


def test_synthetic_recording_is_saved_before_delivery(session):
    def paste(text, **kwargs):
        assert session.history.recent()[0]["output"] == text
        assert kwargs["target"] == 123
        return True
    session.paste.side_effect = paste
    session._start()
    feed(session)
    finish(session)
    assert session.last_run.output == "Draft text."
    assert session.history.recent()[0]["status"] == "pasted"


def test_refined_output_and_original_are_both_recoverable(session):
    run = Run(Settings(), 123)
    run.draft.accept("Original model words.")
    session.refine_available = lambda: True
    session.transcribe.return_value = "Corrected final words."
    session._finalize(run, [np.ones((500, 1), dtype=np.float32)])
    row, = session.history.recent()
    assert row["original"] == "Original model words."
    assert row["output"] == "Corrected final words."


def test_refinement_never_resurrects_undone_words(session):
    run = Run(Settings(), 123)
    run.draft.accept("Keep this.")
    run.draft.accept("Remove this.")
    run.draft.accept("skald scratch that")
    session.refine_available = lambda: True
    session._finalize(run, [np.ones((500, 1), dtype=np.float32)])
    assert run.output == "Keep this."
    session.transcribe.assert_not_called()


def test_long_recording_keeps_all_streamed_words(session):
    session.config.update(MAX_RECORD_SECONDS=1, PHRASE_MAX_SECONDS=.5)
    session.refine_available = lambda: True
    session.transcribe.side_effect = ["First.", "Second.", "Third."]
    session._start()
    for _ in range(3):
        feed(session)
    finish(session)
    assert session.last_run.output == "First. Second. Third."
    assert session.transcribe.call_count == 3


def test_review_mode_never_calls_automation(session):
    session.settings.review_before_paste = True
    session.settings.enable_app_commands = True
    session._start()
    feed(session)
    finish(session)
    session.paste.assert_not_called()
    session.route_command.assert_not_called()
    assert session.history.recent()[0]["status"] == "review"
    assert "review" in [kind for kind, _ in list(session.ui_q.queue)]


def test_saving_off_creates_no_history(session):
    session.settings.save_history = False
    session._start()
    feed(session)
    finish(session)
    assert not session.history.path.exists()
    assert session.last_run.output == "Draft text."


def test_history_failure_holds_draft_in_memory(session):
    session.history.save = Mock(side_effect=OSError("full disk"))
    session._start()
    feed(session)
    finish(session)
    session.paste.assert_not_called()
    assert session.last_run.output == "Draft text."
    assert "History could not be saved" in session.last_run.error


def test_shutdown_flushes_pending_audio_without_pasting(session):
    session._start()
    feed(session)
    session.shutdown()
    session.worker.join(timeout=5)
    assert not session.busy
    session.paste.assert_not_called()
    assert session.history.recent()[0]["output"] == "Draft text."


def test_cancel_during_inference_never_pastes_late_result(session):
    entered, release = Event(), Event()
    def inference(*args):
        entered.set()
        assert release.wait(3)
        return "Late result."
    session.transcribe.side_effect = inference
    session.config["PHRASE_MAX_SECONDS"] = .5
    session._start()
    feed(session)
    assert entered.wait(2)
    session.cancel()
    release.set()
    session.worker.join(timeout=5)
    assert session.last_run.cancelled
    assert session.last_run.output == ""
    session.paste.assert_not_called()


def test_cancel_keeps_original_but_marks_output_discarded(session):
    run = Run(Settings(), 123)
    run.draft.accept("Already recovered speech.")
    run.cancelled = True
    session._finalize(run, [])
    row, = session.history.recent()
    assert row["output"] == ""
    assert row["original"] == "Already recovered speech."
    assert row["status"] == "cancelled"
    session.paste.assert_not_called()


def test_silence_stops_at_owner_requested_prolonged_pause(session):
    session.refine_available = lambda: True
    session._start()
    feed(session, 0, 18000)
    session.worker.join(timeout=5)
    assert not session.busy
    assert not session.recording
    session.transcribe.assert_not_called()
    session.paste.assert_not_called()


def test_pause_discards_audio_until_resume(session):
    session._start()
    session.pause()
    feed(session)
    assert session.active_q is None
    assert session.run.chunks.empty()
    session.pause()
    feed(session)
    finish(session)
    assert session.last_run.output == "Draft text."


def test_app_profile_is_snapshotted_and_does_not_change_default(session):
    session.settings.profiles = {"Notepad.exe": "clean"}
    session.transcribe.return_value = "Um, keep this."
    session._start()
    feed(session)
    finish(session)
    assert session.last_run.output == "keep this."
    assert session.settings.mode == "exact"


def test_inference_failure_exposes_error_and_never_reports_paste_success(session):
    session.transcribe.side_effect = RuntimeError("model unavailable")
    session._start()
    feed(session)
    finish(session)
    session.paste.assert_not_called()
    assert "could not be transcribed" in session.last_run.error


def test_second_start_is_blocked_until_previous_worker_finishes(session):
    entered, release = Event(), Event()
    def inference(*args):
        entered.set()
        assert release.wait(3)
        return "First session."
    session.transcribe.side_effect = inference
    session._start()
    first = session.run
    feed(session)
    session._stop()
    assert entered.wait(2)
    session._start()
    assert session.run is first
    release.set()
    session.worker.join(timeout=5)
    assert session.last_run is first


def test_spoken_edits_survive_full_audio_delivery(session):
    session.config["PHRASE_MAX_SECONDS"] = .5
    session.refine_available = lambda: True
    session.transcribe.side_effect = ["Tuesday", "skald replace Tuesday with Thursday", "skald new paragraph", "Next steps."]
    session._start()
    for _ in range(4):
        feed(session)
    finish(session)
    assert session.last_run.output == "Thursday\n\nNext steps."
    assert session.transcribe.call_count == 4


def test_paste_failure_retains_recovery_copy(session):
    session.paste.side_effect = OSError("clipboard locked")
    session._start()
    feed(session)
    finish(session)
    assert session.history.recent()[0]["output"] == "Draft text."
    assert session.history.recent()[0]["status"] == "held"


def test_delivery_can_be_cancelled_at_the_paste_boundary(session):
    def cancel_before_injection(text, **kwargs):
        session.run.cancelled = True
        assert not kwargs["allowed"]()
        return False
    session.paste.side_effect = cancel_before_injection
    session._start()
    feed(session)
    finish(session)
    assert session.last_run.output == ""
    assert session.history.recent()[0]["status"] == "cancelled"


def test_edited_words_reach_recovery_history_before_paste(session):
    session.settings.vocabulary = {"skull": "Skald"}
    session.config["PHRASE_MAX_SECONDS"] = .5
    session.transcribe.side_effect = ["Use skull", "skald new paragraph", "Next step."]
    session._start()
    for _ in range(3):
        feed(session)
    finish(session)
    row, = session.history.recent()
    assert row["output"] == "Use Skald\n\nNext step."
    assert row["original"].startswith("Use skull")


def test_missing_window_identity_holds_output_for_explicit_copy(session):
    run = Run(Settings(), None)
    run.draft.accept("Keep this safe.")
    session._finalize(run, [])
    assert run.status == "held"
    session.paste.assert_not_called()
