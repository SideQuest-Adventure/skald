"""Small Windows helpers. No process launch, capture, or clipboard writes."""
import ctypes
from ctypes import wintypes
import os


def _user32():
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    return user


def foreground_window():
    if os.name != "nt":
        return None
    try:
        return _user32().GetForegroundWindow()
    except OSError:
        return None


def foreground_process():
    if os.name != "nt":
        return ""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    pid = wintypes.DWORD()
    user = _user32()
    user.GetWindowThreadProcessId(user.GetForegroundWindow(), ctypes.byref(pid))
    handle = kernel.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            return os.path.basename(buffer.value).lower()
        return ""
    finally:
        kernel.CloseHandle(handle)


def clipboard_is_plain_text():
    """Refuse automatic clipboard replacement if we cannot restore every format.

    Pyperclip can only restore plain text. Images, copied files, rich text, and
    unknown formats remain untouched; delivery can use Unicode input instead.
    """
    if os.name != "nt":
        return False
    user = _user32()
    if not user.OpenClipboard(None):
        return False
    try:
        fmt = 0
        ctypes.set_last_error(0)
        while True:
            fmt = user.EnumClipboardFormats(fmt)
            if not fmt:
                return ctypes.get_last_error() == 0
            if fmt not in (1, 7, 13, 16):  # ANSI/OEM/Unicode text and locale
                return False
    finally:
        user.CloseClipboard()


def clipboard_sequence():
    if os.name != "nt":
        return None
    return _user32().GetClipboardSequenceNumber()


class _KeyInput(ctypes.Structure):
    _fields_ = [("vk", wintypes.WORD), ("scan", wintypes.WORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("extra", ctypes.c_size_t)]


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("data", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]


class _InputUnion(ctypes.Union):
    _fields_ = [("key", _KeyInput), ("mouse", _MouseInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("kind", wintypes.DWORD), ("value", _InputUnion)]


def insert_unicode(text, *, target=None, allowed=lambda: True):
    """Deliver text without replacing a rich/image/file clipboard.

    Send UTF-16 key pairs, including surrogate pairs, in bounded batches. Stop if
    the destination changes or the session is cancelled; never submit with Enter.
    """
    if os.name != "nt" or not text:
        return False
    user = _user32()
    user.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int)
    user.SendInput.restype = wintypes.UINT
    data = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-16-le")
    for offset in range(0, len(data), 128):
        if not allowed() or (target is not None and foreground_window() != target):
            return False
        chunk = data[offset:offset + 128]
        events = (_Input * len(chunk))()
        for index in range(0, len(chunk), 2):
            unit = int.from_bytes(chunk[index:index + 2], "little")
            events[index].kind = events[index + 1].kind = 1
            events[index].key = _KeyInput(0, unit, 0x0004, 0, 0)
            events[index + 1].key = _KeyInput(0, unit, 0x0006, 0, 0)
        if user.SendInput(len(events), events, ctypes.sizeof(_Input)) != len(events):
            raise OSError("Windows blocked text delivery; review or copy your draft")
    return True
