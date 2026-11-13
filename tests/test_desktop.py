from unittest.mock import Mock
import ctypes

import pytest
import skald_desktop as desktop


@pytest.fixture
def native(monkeypatch):
    calls = []
    user = Mock()
    def send(count, events, size):
        calls.append([(e.key.scan, e.key.flags) for e in events])
        assert size == ctypes.sizeof(desktop._Input)
        return count
    user.SendInput.side_effect = send
    monkeypatch.setattr(desktop, "_user32", lambda: user)
    monkeypatch.setattr(desktop, "foreground_window", lambda: 123)
    return user, calls


def test_unicode_delivery_handles_non_ascii_surrogates_and_linebreaks(native):
    user, calls = native
    assert desktop.insert_unicode("Rune ᚱ 🐉\nNext", target=123)
    units = [scan for batch in calls for scan, flag in batch if flag == 4]
    encoded = b"".join(n.to_bytes(2, "little") for n in units)
    assert encoded.decode("utf-16-le") == "Rune ᚱ 🐉\nNext"
    assert all(batch[i + 1] == (batch[i][0], 6)
               for batch in calls for i in range(0, len(batch), 2))


def test_unicode_delivery_stops_on_focus_change(native, monkeypatch):
    user, calls = native
    monkeypatch.setattr(desktop, "foreground_window", Mock(side_effect=[123, 456]))
    assert not desktop.insert_unicode("x" * 200, target=123)
    assert len(calls) == 1


def test_unicode_delivery_does_not_insert_cancelled_draft(native):
    user, calls = native
    assert not desktop.insert_unicode("Cancelled", target=123, allowed=lambda: False)
    assert not calls


def test_unicode_delivery_reports_blocked_input(native):
    user, calls = native
    user.SendInput.side_effect = lambda *args: 0
    with pytest.raises(OSError, match="Windows blocked text delivery"):
        desktop.insert_unicode("Draft", target=123)
