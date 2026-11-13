from unittest.mock import Mock
import pytest
import skald as sk


@pytest.fixture
def clipboard(monkeypatch):
    state = {"text": "previous copy", "seq": 1, "window": 123}
    def copy(text):
        state["text"] = text
        state["seq"] += 1
    monkeypatch.setattr(sk.pyperclip, "copy", copy)
    monkeypatch.setattr(sk.pyperclip, "paste", lambda: state["text"])
    monkeypatch.setattr(sk, "clipboard_sequence", lambda: state["seq"])
    monkeypatch.setattr(sk, "clipboard_is_plain_text", lambda: True)
    monkeypatch.setattr(sk, "foreground_window", lambda: state["window"])
    monkeypatch.setattr(sk.time, "sleep", lambda value: None)
    monkeypatch.setattr(sk.pyautogui, "hotkey", Mock())
    monkeypatch.setattr(sk.SETTINGS, "preserve_clipboard", True)
    return state


def test_paste_restores_previous_plaintext(clipboard):
    assert sk._paste_text("dictation", target=123)
    assert clipboard["text"] == "previous copy"
    sk.pyautogui.hotkey.assert_called_once_with("ctrl", "v")


def test_focus_change_holds_dictation_without_touching_clipboard(clipboard):
    clipboard["window"] = 456
    assert not sk._paste_text("dictation", target=123)
    assert clipboard["text"] == "previous copy"
    sk.pyautogui.hotkey.assert_not_called()


def test_focus_change_during_paste_delay_still_restores_clipboard(clipboard, monkeypatch):
    monkeypatch.setattr(sk.time, "sleep", lambda value: clipboard.update(window=456))
    assert not sk._paste_text("dictation", target=123)
    assert clipboard["text"] == "previous copy"
    sk.pyautogui.hotkey.assert_not_called()


def test_rich_clipboard_is_not_destroyed(clipboard, monkeypatch):
    monkeypatch.setattr(sk, "clipboard_is_plain_text", lambda: False)
    insert = Mock(return_value=True)
    monkeypatch.setattr(sk, "insert_unicode", insert)
    assert sk._paste_text("dictation", target=123)
    assert insert.call_args.args == ("dictation",)
    assert insert.call_args.kwargs["target"] == 123
    assert clipboard["text"] == "previous copy"
    sk.pyautogui.hotkey.assert_not_called()


def test_overlay_keeps_polling_after_empty_queue_and_displays_next_status():
    import queue
    overlay = sk._Overlay.__new__(sk._Overlay)
    overlay._alive = True
    overlay.root = Mock()
    overlay.sess = Mock(recording=False, paused=False, ui_q=queue.Queue())
    overlay.status = Mock()
    overlay.record_btn = Mock()
    overlay.pause_btn = Mock()
    overlay._tick()
    overlay.root.after.assert_called_once_with(50, overlay._tick)
    overlay.sess.ui_q.put(("status", ("idle", "Pasted; tap to talk")))
    overlay._tick()
    overlay.status.config.assert_called_with(text="Pasted; tap to talk", fg=overlay.SUB)
    assert overlay.root.after.call_count == 2


def test_new_user_copy_wins_even_if_it_matches_dictation(clipboard, monkeypatch):
    def user_copy(*args):
        sk.pyperclip.copy("dictation")
    monkeypatch.setattr(sk.pyautogui, "hotkey", user_copy)
    assert sk._paste_text("dictation", target=123)
    assert clipboard["text"] == "dictation"


def test_clipboard_restored_when_automation_fails(clipboard, monkeypatch):
    monkeypatch.setattr(sk.pyautogui, "hotkey", Mock(side_effect=RuntimeError("failed")))
    with pytest.raises(RuntimeError):
        sk._paste_text("dictation", target=123)
    assert clipboard["text"] == "previous copy"


def test_cancel_while_waiting_to_paste_restores_clipboard(clipboard, monkeypatch):
    state = {"allowed": True}
    monkeypatch.setattr(sk.time, "sleep", lambda seconds: state.update(allowed=False))
    assert not sk._paste_text("dictation", target=123, allowed=lambda: state["allowed"])
    assert clipboard["text"] == "previous copy"
    sk.pyautogui.hotkey.assert_not_called()


def test_model_override_is_shared_by_both_modes(monkeypatch):
    monkeypatch.setattr(sk, "SETTINGS", sk.Settings())
    monkeypatch.setattr(sk, "CONFIG", dict(sk.CONFIG))
    sk.apply_settings(sk.Settings(model="tiny.en"))
    assert sk.CONFIG["MODEL_SIZE"] == sk.CONFIG["LIVE_MODEL"] == "tiny.en"


def test_arbitrary_spoken_app_name_is_never_sent_to_shell(monkeypatch):
    popen = Mock()
    monkeypatch.setattr(sk.subprocess, "Popen", popen)
    sk._open_target('unknown" & calc & "')
    popen.assert_not_called()


def test_numeric_microphone_setting_and_language_resolution(monkeypatch):
    monkeypatch.setattr(sk, "SETTINGS", sk.Settings())
    monkeypatch.setattr(sk, "CONFIG", dict(sk.CONFIG))
    sk.apply_settings(sk.Settings(device="18", language="de"))
    assert sk.CONFIG["DEVICE"] == 18
    sk.resolve_auto_models({"cpu_cores": 4})
    assert sk.CONFIG["MODEL_SIZE"] == sk.CONFIG["LIVE_MODEL"] == "base"


def test_remember_spelling_is_persisted_without_cascading_replacements(tmp_path, monkeypatch):
    monkeypatch.setattr(sk, "SETTINGS_PATH", tmp_path / "skald.toml")
    monkeypatch.setattr(sk, "SETTINGS", sk.Settings())
    monkeypatch.setattr(sk, "CONFIG", dict(sk.CONFIG))
    overlay = sk._Overlay.__new__(sk._Overlay)
    overlay.sess = Mock()
    overlay.sess.settings = sk.Settings(vocabulary={"SKULL": "wrong", "other": "kept"})
    overlay._refresh_controls = Mock()
    overlay._learn_spelling("skull", "Skald")
    loaded = sk.Settings.load(sk.SETTINGS_PATH)
    assert loaded.vocabulary == {"other": "kept", "skull": "Skald"}


def test_app_command_is_held_if_its_destination_changed(clipboard, monkeypatch):
    clipboard["window"] = 456
    send = Mock()
    monkeypatch.setattr(sk, "_send_text", send)
    assert not sk.route_command("skald send example", target=123)
    send.assert_not_called()


def test_classic_unsaved_held_draft_can_be_recovered_from_console(clipboard, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sk, "SETTINGS", sk.Settings(save_history=False))
    monkeypatch.setattr(sk, "CONFIG", dict(sk.CONFIG, SAVE_TRANSCRIPTS=False))
    monkeypatch.setattr(sk, "HISTORY", sk.History(tmp_path / "history.sqlite3"))
    monkeypatch.setattr(sk, "_classic_target", 456)  # different window; must hold
    monkeypatch.setattr(sk, "transcribe_any", lambda model, audio: "Keep this thought.")
    sk.transcribe_and_paste(None, None)
    assert "Unsaved draft held for manual copy:\nKeep this thought." in capsys.readouterr().out
    assert not sk.HISTORY.path.exists()
    sk.pyautogui.hotkey.assert_not_called()
