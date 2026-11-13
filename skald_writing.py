"""Local writing tools. No microphone, desktop automation, or model imports."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import tempfile
import tomllib


MODES = ("exact", "clean", "bullets")


def data_directory() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "Skald"


@dataclass
class Settings:
    mode: str = "exact"
    review_before_paste: bool = False
    save_history: bool = True
    retention_days: int = 30
    auto_stop_silence: float = 18.0
    spoken_editing: bool = True
    enable_app_commands: bool = False
    hotkey: str = "ctrl_r"
    device: str = ""
    model: str = "auto"
    language: str = "en"
    chime: bool = True
    preserve_clipboard: bool = True
    vocabulary: dict[str, str] = field(default_factory=dict)
    snippets: dict[str, str] = field(default_factory=dict)
    # Optional per-process overrides, e.g. {"notepad.exe": "clean"}.
    profiles: dict[str, str] = field(default_factory=dict)

    def validate(self):
        for name in ("review_before_paste", "save_history", "spoken_editing",
                     "enable_app_commands", "chime", "preserve_clipboard"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be true or false")
        if self.mode not in MODES:
            raise ValueError("mode must be exact, clean, or bullets")
        if type(self.retention_days) is not int or self.retention_days < 0:
            raise ValueError("retention_days must be a nonnegative integer")
        value = self.auto_stop_silence
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("auto_stop_silence must be 0 (off) or positive seconds")
        for name in ("hotkey", "device", "model", "language"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be text")
        if not self.hotkey.strip() or not self.model.strip():
            raise ValueError("hotkey and model cannot be empty")
        for name in ("vocabulary", "snippets", "profiles"):
            table = getattr(self, name)
            if not isinstance(table, dict) or any(
                not isinstance(k, str) or not k.strip() or not isinstance(v, str)
                or not v.strip() for k, v in table.items()
            ):
                raise ValueError(f"{name} must map nonempty text to nonempty text")
            if len({k.casefold() for k in table}) != len(table):
                raise ValueError(f"{name} contains duplicate names ignoring case")
        if any(mode not in MODES for mode in self.profiles.values()):
            raise ValueError("profile values must be exact, clean, or bullets")
        return self

    @classmethod
    def load(cls, path: Path):
        if not path.exists():
            return cls()
        with path.open("rb") as handle:
            values = tomllib.load(handle)
        unknown = values.keys() - cls.__dataclass_fields__.keys()
        if unknown:
            raise ValueError("Unknown settings: " + ", ".join(sorted(unknown)))
        return cls(**values).validate()

    def save(self, path: Path):
        self.validate()
        lines = ["# Skald settings. Everything is processed locally."]
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if isinstance(value, dict):
                continue
            encoded = str(value).lower() if isinstance(value, bool) else json.dumps(value, ensure_ascii=False)
            lines.append(f"{name} = {encoded}")
        for name in ("vocabulary", "snippets", "profiles"):
            lines.extend(("", f"[{name}]"))
            for key, value in getattr(self, name).items():
                lines.append(f"{json.dumps(key, ensure_ascii=False)} = {json.dumps(value, ensure_ascii=False)}")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".skald-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(lines) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def apply_vocabulary(text: str, vocabulary: dict[str, str]) -> str:
    """Literal, whole-term, case-insensitive replacements in a single pass.

    Longest aliases win; replacements never cascade into another alias.
    """
    if not vocabulary:
        return text
    lookup = {key.casefold(): value for key, value in vocabulary.items()}
    terms = sorted(vocabulary, key=len, reverse=True)
    pattern = r"(?<!\w)(?:" + "|".join(re.escape(term) for term in terms) + r")(?!\w)"
    def replacement(match):
        value = lookup.get(match[0].casefold())
        if value is not None:
            return value
        # Python's Unicode IGNORECASE also matches dotted/dotless I variants.
        return next(vocabulary[term] for term in terms if re.fullmatch(re.escape(term), match[0], re.I))
    return re.sub(pattern, replacement, text, flags=re.I)


def format_text(text: str, mode: str, language="en") -> str:
    if mode not in MODES:
        raise ValueError(f"Unknown writing mode: {mode}")
    text = text.strip()
    if mode == "exact":
        return text
    # Only explicit hesitation sounds. Keep "like", "actually", repeated words,
    # false starts, numbers, and negations; their meaning requires human judgment.
    if language and language.lower().split("-")[0] == "en":
        text = re.sub(r'''(?<![\w"'/-])(?:um+|uh+|erm+)(?![\w-])[,.;:!?]*[ \t]*''', "", text, flags=re.I)
    text = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"(?m)^[ \t]+|[ \t]+$", "", text).strip()
    if mode == "clean":
        return text
    # Deliberately use spoken line breaks, not guessed sentence boundaries.
    return "\n".join("- " + re.sub(r"^[-*]\s+", "", line.strip())
                     for line in text.splitlines() if line.strip())


@dataclass
class Draft:
    settings: Settings
    text: str = ""
    original: list[str] = field(default_factory=list)
    undo_stack: list[str] = field(default_factory=list)
    edited: bool = False
    cancelled: bool = False

    @property
    def output(self):
        return format_text(self.text, self.settings.mode, self.settings.language)

    def _change(self, text):
        if text != self.text:
            self.undo_stack.append(self.text)
            self.text = text

    def accept(self, phrase: str) -> str:
        phrase = phrase.strip()
        if not phrase or self.cancelled:
            return ""
        self.original.append(phrase)
        command = re.fullmatch(r"skald[ ,]+(.+?)[.!?]*", phrase, flags=re.I | re.S)
        if command and self.settings.spoken_editing:
            rest = command[1].strip()
            low = rest.casefold()
            breaks = {"new line": "\n", "new paragraph": "\n\n", "next bullet": "\n"}
            punctuation = {"period": ".", "comma": ",", "question mark": "?",
                           "exclamation mark": "!", "colon": ":", "semicolon": ";"}
            if (low in breaks or low in punctuation or low in
                    ("scratch that", "undo", "undo last phrase", "cancel", "discard")
                    or low.startswith(("replace ", "insert "))):
                self.edited = True
            if low in breaks:
                self._change(self.text.rstrip() + breaks[low])
            elif low in punctuation:
                self._change(self.text.rstrip() + punctuation[low])
            elif low in ("scratch that", "undo", "undo last phrase"):
                if not self.undo_stack:
                    return "Nothing to undo"
                self.text = self.undo_stack.pop()
            elif low in ("cancel", "discard"):
                self.cancelled = True
                return "Cancelled"
            elif low.startswith("replace "):
                match = re.fullmatch(r"replace (.+?) with (.+)", rest, flags=re.I | re.S)
                if not match:
                    return "Say: skald replace old words with new words"
                matches = list(re.finditer(r"(?<!\w)" + re.escape(match[1]) + r"(?!\w)", self.text, re.I))
                if not matches:
                    return f"Could not find: {match[1]}"
                found = matches[-1]
                self._change(self.text[:found.start()] + match[2] + self.text[found.end():])
            elif low.startswith("insert "):
                name = rest[7:].strip().casefold()
                snippet = next((v for k, v in self.settings.snippets.items() if k.casefold() == name), None)
                if snippet is None:
                    return f"No shortcut named: {rest[7:]}"
                self._append(snippet)
            else:
                self._append(apply_vocabulary(phrase, self.settings.vocabulary))
                return ""
            self.edited = True
            return "Edited" if not self.cancelled else "Cancelled"
        self._append(apply_vocabulary(phrase, self.settings.vocabulary))
        return ""

    def _append(self, phrase):
        gap = "" if not self.text or self.text[-1].isspace() or phrase[0] in ",.;:!?" else " "
        self._change(self.text + gap + phrase)
