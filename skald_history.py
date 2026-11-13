"""Optional local recovery journal. One connection per operation for worker safety."""
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path
import sqlite3


class History:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.create_function("casefold", 1, str.casefold, deterministic=True)
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS dictations (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, original TEXT NOT NULL,
                output TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
                pinned INTEGER NOT NULL DEFAULT 0)""")
            with connection:
                yield connection
        finally:
            connection.close()

    def save(self, ident, created_at, original, output, mode, status="draft"):
        with self._connect() as connection:
            connection.execute("""INSERT INTO dictations
                (id, created_at, original, output, mode, status) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET original=excluded.original,
                output=excluded.output, mode=excluded.mode, status=excluded.status""",
                (ident, created_at, original, output, mode, status))

    def recent(self, query="", limit=100):
        if not self.path.exists():
            return []
        with self._connect() as connection:
            return [dict(row) for row in connection.execute("""SELECT * FROM dictations
                WHERE instr(casefold(original || char(10) || output), casefold(?)) > 0
                ORDER BY pinned DESC, created_at DESC LIMIT ?""", (query, limit))]

    def pin(self, ident, pinned):
        with self._connect() as connection:
            connection.execute("UPDATE dictations SET pinned=? WHERE id=?", (int(pinned), ident))

    def delete(self, ident):
        with self._connect() as connection:
            connection.execute("DELETE FROM dictations WHERE id=?", (ident,))

    def prune(self, days):
        if not days or not self.path.exists():
            return
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._connect() as connection:
            connection.execute("DELETE FROM dictations WHERE pinned=0 AND created_at < ?", (cutoff,))
