import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

CONTROL_ACTIONS = {"play_pause", "next", "prev", "shuffle", "switch_device"}

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    uid TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('content', 'control')),
    uri TEXT,
    content_type TEXT,
    name TEXT,
    artist TEXT,
    artwork_url TEXT,
    action TEXT,
    created_at TEXT NOT NULL,
    last_played_at TEXT,
    play_count INTEGER NOT NULL DEFAULT 0,
    on_tag INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# version -> statements that bring a database from version-1 up to version
_MIGRATIONS: dict[int, list[str]] = {
    2: ["ALTER TABLE cards ADD COLUMN on_tag INTEGER NOT NULL DEFAULT 0"],
}


@dataclass(frozen=True)
class Card:
    uid: str
    kind: str
    uri: str | None
    content_type: str | None
    name: str | None
    artist: str | None
    artwork_url: str | None
    action: str | None
    created_at: str
    last_played_at: str | None
    play_count: int
    on_tag: int  # 1 if the card's tag memory holds this uri (self-describing card)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._lock:
            fresh = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cards'"
            ).fetchone() is None
            self._conn.executescript(_SCHEMA)
            if fresh:
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        version = self.schema_version()
        for target in range(version + 1, SCHEMA_VERSION + 1):
            for stmt in _MIGRATIONS.get(target, []):
                self._conn.execute(stmt)
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(target),),
            )

    def schema_version(self) -> int:
        row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row["value"]) if row else 1

    def close(self) -> None:
        self._conn.close()

    def get_card(self, uid: str) -> Card | None:
        row = self._conn.execute("SELECT * FROM cards WHERE uid = ?", (uid,)).fetchone()
        return _to_card(row) if row else None

    def list_cards(self) -> list[Card]:
        rows = self._conn.execute(
            "SELECT * FROM cards ORDER BY kind DESC, name COLLATE NOCASE"
        ).fetchall()
        return [_to_card(r) for r in rows]

    def save_content_card(
        self,
        uid: str,
        uri: str,
        content_type: str,
        name: str,
        artist: str | None,
        artwork_url: str | None,
        on_tag: bool = False,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO cards (uid, kind, uri, content_type, name, artist, artwork_url, on_tag, created_at)
                   VALUES (?, 'content', ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(uid) DO UPDATE SET
                     kind='content', uri=excluded.uri, content_type=excluded.content_type,
                     name=excluded.name, artist=excluded.artist, artwork_url=excluded.artwork_url,
                     on_tag=excluded.on_tag, action=NULL""",
                (uid, uri, content_type, name, artist, artwork_url, int(on_tag), _now()),
            )
            self._conn.commit()

    def save_control_card(self, uid: str, action: str) -> None:
        if action not in CONTROL_ACTIONS:
            raise ValueError(f"unknown control action: {action}")
        with self._lock:
            self._conn.execute(
                """INSERT INTO cards (uid, kind, action, name, created_at)
                   VALUES (?, 'control', ?, ?, ?)
                   ON CONFLICT(uid) DO UPDATE SET
                     kind='control', action=excluded.action, name=excluded.name,
                     uri=NULL, content_type=NULL, artist=NULL, artwork_url=NULL, on_tag=0""",
                (uid, action, action.replace("_", " ").title(), _now()),
            )
            self._conn.commit()

    def delete_card(self, uid: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM cards WHERE uid = ?", (uid,))
            self._conn.commit()

    def record_play(self, uid: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE cards SET play_count = play_count + 1, last_played_at = ? WHERE uid = ?",
                (_now(), uid),
            )
            self._conn.commit()


def _to_card(row: sqlite3.Row) -> Card:
    return Card(**{k: row[k] for k in row.keys()})
