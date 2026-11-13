"""Writing behavior and settings: preserve meaning, apply explicit edits only."""
from dataclasses import replace
import pytest

from skald_writing import Draft, Settings, apply_vocabulary, format_text


@pytest.mark.parametrize("text", [
    "Do not send $1,250.50 before 11/09.", "I I meant Tuesday, actually Thursday.",
    "I like this. Actually, keep it.", "Version 1.4.0 uses RX 7900 GRE.",
])
def test_exact_preserves_words(text):
    assert format_text(text, "exact") == text


def test_clean_keeps_negations_repetitions_and_technical_values():
    assert format_text("Um, do not change -5.5 to 5.5. Uh, I I mean API_v2.", "clean") == (
        "do not change -5.5 to 5.5. I I mean API_v2.")


def test_clean_keeps_quoted_words_hyphenated_speech_and_nonenglish():
    assert format_text('Say "um" and uh-huh.', "clean") == 'Say "um" and uh-huh.'
    assert format_text("Um acht Uhr.", "clean", "de") == "Um acht Uhr."
    assert format_text("Um eight.", "clean", "") == "Um eight."
    assert format_text("Um. Do not change this.", "clean") == "Do not change this."


def test_bullets_use_explicit_lines_and_preserve_negative_numbers():
    assert format_text("-5 degrees\n\n- Existing bullet\nVersion 1.2.", "bullets") == (
        "- -5 degrees\n- Existing bullet\n- Version 1.2.")


def test_vocabulary_literal_longest_and_non_cascading():
    vocabulary = {"skull": "Skald", "skull tool": "Skald Desktop", "Skald": "Wrong", "C++": "C plus plus"}
    assert apply_vocabulary("SKULL TOOL, skull, skullcap, C++.", vocabulary) == (
        "Skald Desktop, Skald, skullcap, C plus plus.")


def test_vocabulary_replacement_is_not_a_regex_replacement():
    assert apply_vocabulary("net", {"net": r"C:\new\1"}) == r"C:\new\1"


def test_spoken_paragraphs_replacement_punctuation_and_undo():
    draft = Draft(Settings())
    draft.accept("Meet Tuesday.")
    draft.accept("Skald replace Tuesday with Thursday.")
    draft.accept("skald new paragraph")
    draft.accept("Bring the draft")
    draft.accept("skald period")
    assert draft.output == "Meet Thursday.\n\nBring the draft."
    draft.accept("skald scratch that")
    draft.accept("skald scratch that")
    assert draft.output == "Meet Thursday."
    assert draft.edited
    assert draft.original[1] == "Skald replace Tuesday with Thursday."


def test_replace_only_last_whole_term():
    draft = Draft(Settings(), text="Tuesday and Tuesday. Tuesdays stay.")
    draft.accept("skald replace Tuesday with Thursday")
    assert draft.output == "Tuesday and Thursday. Tuesdays stay."


def test_empty_undo_and_failed_edit_never_become_dictated_words():
    draft = Draft(Settings())
    assert draft.accept("skald scratch that") == "Nothing to undo"
    assert draft.accept("skald replace Tuesday with Thursday") == "Could not find: Tuesday"
    assert draft.output == ""
    assert draft.edited


def test_ordinary_use_of_brand_name_is_dictated():
    draft = Draft(Settings())
    draft.accept("Skald is a Norse bard.")
    assert draft.output == "Skald is a Norse bard."
    assert not draft.edited


def test_snippets_and_cancel():
    draft = Draft(Settings(snippets={"Follow up": "Thanks.\nNext steps:"}))
    draft.accept("skald insert follow up")
    assert draft.output == "Thanks.\nNext steps:"
    assert draft.accept("skald cancel") == "Cancelled"
    draft.accept("ignored after cancellation")
    assert draft.cancelled
    assert "ignored" not in draft.output


def test_commands_can_be_disabled_without_changing_words():
    draft = Draft(Settings(spoken_editing=False))
    draft.accept("skald new paragraph")
    assert draft.output == "skald new paragraph"


def test_settings_roundtrip_unicode_multiline_and_equals(tmp_path):
    settings = Settings(mode="clean", vocabulary={"rune": "ᛋᚲᚨᛚᛞ"},
                        snippets={"formula": "a = b\nnext"}, profiles={"notepad.exe": "exact"})
    path = tmp_path / "settings.toml"
    settings.save(path)
    assert Settings.load(path) == settings


@pytest.mark.parametrize("changes", [
    {"mode": "creative"}, {"save_history": "false"}, {"auto_stop_silence": -1},
    {"auto_stop_silence": float("nan")}, {"auto_stop_silence": float("inf")},
    {"retention_days": True}, {"retention_days": -1}, {"device": 1},
    {"vocabulary": {"": "value"}}, {"vocabulary": {"x": "a", "X": "b"}},
    {"profiles": {"app.exe": "creative"}},
])
def test_invalid_settings_rejected(changes):
    with pytest.raises((ValueError, TypeError)):
        replace(Settings(), **changes).validate()


def test_bad_settings_do_not_replace_last_good_file(tmp_path):
    path = tmp_path / "settings.toml"
    Settings().save(path)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        Settings(mode="invalid").save(path)
    assert path.read_bytes() == before


def test_unknown_settings_are_reported(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('mdoe = "clean"', encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown settings: mdoe"):
        Settings.load(path)
