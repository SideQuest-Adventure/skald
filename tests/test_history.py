from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

from skald_history import History


def test_reading_empty_history_creates_no_files(tmp_path):
    history = History(tmp_path / "missing" / "history.sqlite3")
    assert history.recent() == []
    assert not history.path.parent.exists()


def test_recovery_upsert_preserves_original_and_pin(tmp_path):
    history = History(tmp_path / "history.sqlite3")
    now = datetime.now(timezone.utc).isoformat()
    history.save("a", now, "um original", "original", "clean")
    history.pin("a", True)
    history.save("a", now, "um original", "edited draft", "clean", "ready")
    entry, = history.recent()
    assert entry["original"] == "um original"
    assert entry["output"] == "edited draft"
    assert entry["pinned"] == 1
    assert entry["status"] == "ready"
    assert history.recent("%") == []
    assert history.recent("EDITED")[0]["id"] == "a"


def test_retention_keeps_pinned_entries_and_delete_is_scoped(tmp_path):
    history = History(tmp_path / "history.sqlite3")
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    for ident, date in (("old", old), ("pinned", old), ("new", now)):
        history.save(ident, date, ident, ident, "exact")
    history.pin("pinned", True)
    history.prune(30)
    assert {row["id"] for row in history.recent()} == {"pinned", "new"}
    history.delete("new")
    assert [row["id"] for row in history.recent()] == ["pinned"]


def test_concurrent_history_writes_do_not_mix_recordings(tmp_path):
    history = History(tmp_path / "history.sqlite3")
    def save(index):
        history.save(str(index), datetime.now(timezone.utc).isoformat(), str(index), str(index), "exact")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(save, range(20)))
    assert len(history.recent()) == 20
    assert all(row["original"] == row["output"] == row["id"] for row in history.recent())


def test_history_search_handles_unicode_casefold(tmp_path):
    history = History(tmp_path / "history.sqlite3")
    history.save("a", datetime.now(timezone.utc).isoformat(), "José", "Straße", "exact")
    assert history.recent("JOSÉ")[0]["id"] == "a"
    assert history.recent("STRASSE")[0]["id"] == "a"
