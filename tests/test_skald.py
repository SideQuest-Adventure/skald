"""Characterization tests for Skald's deterministic core: the command parser,
resampling, auto-gain, and the WAV encoder. No mic, no model, and no GPU are needed.

Run:  python -m pytest tests -q
(needs numpy plus the runtime deps that let skald.py import: sounddevice, pyperclip,
pyautogui, pynput, faster_whisper)
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import skald as sk  # noqa: E402


# ------------------------------------------------------------ parse_command
def test_plain_dictation_is_not_a_command():
    assert sk.parse_command("open the door for me please") is None
    assert sk.parse_command("I searched everywhere for it") is None


def test_prefix_alone_gives_help():
    assert sk.parse_command("Skald") == ("help", "")
    assert sk.parse_command("skald.") == ("help", "")


def test_open_and_synonyms():
    assert sk.parse_command("skald open chrome") == ("open", "chrome")
    assert sk.parse_command("Skald, launch notepad") == ("open", "notepad")


def test_search_and_google_synonym():
    assert sk.parse_command("skald search kentucky llc fees") == ("search", "kentucky llc fees")
    assert sk.parse_command("skald google best gyms") == ("search", "best gyms")


def test_send_and_send_message():
    assert sk.parse_command("skald send hello team") == ("send", "hello team")
    v, arg = sk.parse_command("skald send message hello team")
    assert v == "send" and arg.endswith("hello team")


def test_type_dictate_paste_normalize():
    assert sk.parse_command("skald type exact words") == ("type", "exact words")
    assert sk.parse_command("skald dictate exact words") == ("type", "exact words")


def test_cancel_phrases():
    for phrase in ("skald cancel", "skald never mind", "skald scratch that"):
        assert sk.parse_command(phrase) == ("cancel", "")


def test_unknown_verb_reported_not_executed():
    v, arg = sk.parse_command("skald teleport home")
    assert v == "unknown"


def test_trailing_punctuation_stripped():
    assert sk.parse_command("Skald open chrome.") == ("open", "chrome")


# ------------------------------------------------------------ audio helpers
def test_resample_halves_48k_to_16k_length():
    audio = np.random.rand(48000).astype(np.float32) * 0.1
    out = sk.resample_to_16k(audio, 48000)
    assert abs(out.size - 16000) <= 2
    assert out.dtype == np.float32


def test_resample_noop_at_16k():
    audio = np.random.rand(16000).astype(np.float32)
    out = sk.resample_to_16k(audio, 16000)
    assert out is audio


def test_auto_gain_lifts_quiet_speech_toward_target():
    quiet = (np.sin(np.linspace(0, 200 * np.pi, 16000)) * 0.005).astype(np.float32)
    boosted = sk.apply_auto_gain(quiet)
    rms = float(np.sqrt(np.mean(boosted ** 2)))
    assert rms > 0.02  # lifted well above the 0.004 silence gate


def test_auto_gain_never_clips():
    quiet = (np.sin(np.linspace(0, 200 * np.pi, 16000)) * 0.004).astype(np.float32)
    quiet[100] = 0.9  # a click that must not be pushed past full scale
    boosted = sk.apply_auto_gain(quiet)
    assert float(np.max(np.abs(boosted))) <= 1.0


def test_auto_gain_noop_on_healthy_audio():
    healthy = (np.sin(np.linspace(0, 200 * np.pi, 16000)) * 0.5).astype(np.float32)
    out = sk.apply_auto_gain(healthy)
    assert float(np.max(np.abs(out))) <= 0.51  # no boost applied


# ------------------------------------------------------------ WAV encode
def test_wav_b64_roundtrip_shape():
    import base64
    import io
    import wave
    audio = (np.sin(np.linspace(0, 100 * np.pi, 16000)) * 0.3).astype(np.float32)
    b64 = sk._wav_b64(audio)
    with wave.open(io.BytesIO(base64.b64decode(b64)), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == 16000
