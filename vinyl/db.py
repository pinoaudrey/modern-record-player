import json
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

CONTROL_ACTIONS = {"play_pause", "next", "prev", "shuffle", "switch_device"}

SCHEMA_VERSION = 4

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
    on_tag INTEGER NOT NULL DEFAULT 0,
    options TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plays (
    played_at TEXT PRIMARY KEY,
    track_uri TEXT NOT NULL,
    track_name TEXT NOT NULL,
    artist TEXT,
    album_uri TEXT NOT NULL,
    album_name TEXT NOT NULL,
    artwork_url TEXT,
    context_uri TEXT
);
CREATE INDEX IF NOT EXISTS plays_album ON plays (album_uri);
CREATE INDEX IF NOT EXISTS plays_context ON plays (context_uri);
CREATE TABLE IF NOT EXISTS library (
    uri TEXT PRIMARY KEY,
    content_type TEXT NOT NULL,
    name TEXT,
    artist TEXT,
    artwork_url TEXT,
    available INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    uid TEXT PRIMARY KEY,
    track_uri TEXT NOT NULL,
    position_ms INTEGER NOT NULL DEFAULT 0,
    saved_at TEXT NOT NULL
);
"""

_STATEMENTS = [stmt.strip() for stmt in _SCHEMA.split(";") if stmt.strip()]
_PLAYS_DDL = [stmt for stmt in _STATEMENTS if "plays" in stmt or "library" in stmt]
_POSITIONS_DDL = [stmt for stmt in _STATEMENTS if "positions" in stmt]

# version -> statements that bring a database from version-1 up to version
_MIGRATIONS: dict[int, list[str]] = {
    2: ["ALTER TABLE cards ADD COLUMN on_tag INTEGER NOT NULL DEFAULT 0"],
    3: _PLAYS_DDL,  # CREATE IF NOT EXISTS, harmless on a fresh database
    4: ["ALTER TABLE cards ADD COLUMN options TEXT", *_POSITIONS_DDL],
}


@dataclass(frozen=True)
class CardOptions:
    """Per-card playback options, stored as JSON in cards.options."""
    single: bool = False            # play this one thing, then stop (no autoplay)
    resume: bool = False            # continue where the card was interrupted
    shuffle: bool | None = None     # None = leave shuffle alone, else set it before playing

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | None) -> "CardOptions":
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except ValueError:
            return cls()
        if not isinstance(data, dict):
            return cls()
        shuffle = data.get("shuffle")
        return cls(
            single=bool(data.get("single", False)),
            resume=bool(data.get("resume", False)),
            shuffle=None if shuffle is None else bool(shuffle),
        )


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
    options: CardOptions = CardOptions()


@dataclass(frozen=True)
class Position:
    """Where a resume card was when it was interrupted."""
    uid: str
    track_uri: str
    position_ms: int
    saved_at: str


@dataclass(frozen=True)
class Play:
    played_at: str
    track_uri: str
    track_name: str
    artist: str | None
    album_uri: str
    album_name: str
    artwork_url: str | None
    context_uri: str | None


@dataclass(frozen=True)
class LibraryItem:
    uri: str
    content_type: str
    name: str | None
    artist: str | None
    artwork_url: str | None
    available: int
    updated_at: str


@dataclass(frozen=True)
class PlayCount:
    """An album or context with how often it was played in a window."""
    uri: str
    content_type: str
    name: str | None
    artist: str | None
    artwork_url: str | None
    plays: int
    last_played_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def since_days(days: int) -> str:
    """ISO UTC timestamp `days` ago, comparable with stored played_at strings."""
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


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
        options: CardOptions | None = None,
    ) -> None:
        """Register or re-register a content card. Existing options are kept
        unless new ones are given."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO cards (uid, kind, uri, content_type, name, artist, artwork_url, on_tag, options, created_at)
                   VALUES (?, 'content', ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(uid) DO UPDATE SET
                     kind='content', uri=excluded.uri, content_type=excluded.content_type,
                     name=excluded.name, artist=excluded.artist, artwork_url=excluded.artwork_url,
                     on_tag=excluded.on_tag, action=NULL,
                     options=COALESCE(excluded.options, cards.options)""",
                (uid, uri, content_type, name, artist, artwork_url, int(on_tag),
                 options.to_json() if options is not None else None, _now()),
            )
            self._conn.commit()

    def set_card_options(self, uid: str, options: CardOptions) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE cards SET options = ? WHERE uid = ?", (options.to_json(), uid)
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
                     uri=NULL, content_type=NULL, artist=NULL, artwork_url=NULL, on_tag=0,
                     options=NULL""",
                (uid, action, action.replace("_", " ").title(), _now()),
            )
            self._conn.commit()

    def delete_card(self, uid: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM cards WHERE uid = ?", (uid,))
            self._conn.execute("DELETE FROM positions WHERE uid = ?", (uid,))
            self._conn.commit()

    def record_play(self, uid: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE cards SET play_count = play_count + 1, last_played_at = ? WHERE uid = ?",
                (_now(), uid),
            )
            self._conn.commit()


    # --- resume positions ---------------------------------------------------

    def save_position(self, uid: str, track_uri: str, position_ms: int) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO positions (uid, track_uri, position_ms, saved_at)
                   VALUES (?, ?, ?, ?)""",
                (uid, track_uri, int(position_ms), _now()),
            )
            self._conn.commit()

    def get_position(self, uid: str) -> Position | None:
        row = self._conn.execute("SELECT * FROM positions WHERE uid = ?", (uid,)).fetchone()
        return Position(**{k: row[k] for k in row.keys()}) if row else None

    def clear_position(self, uid: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM positions WHERE uid = ?", (uid,))
            self._conn.commit()

    def list_positions(self) -> dict[str, Position]:
        rows = self._conn.execute("SELECT * FROM positions").fetchall()
        return {r["uid"]: Position(**{k: r[k] for k in r.keys()}) for r in rows}

    # --- meta ---------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
            )
            self._conn.commit()

    # --- play history -------------------------------------------------------

    def add_plays(self, plays: list[Play]) -> int:
        """Insert plays not seen before (played_at is unique per account). Returns how many were new."""
        if not plays:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                """INSERT OR IGNORE INTO plays
                   (played_at, track_uri, track_name, artist, album_uri, album_name, artwork_url, context_uri)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [(p.played_at, p.track_uri, p.track_name, p.artist, p.album_uri,
                  p.album_name, p.artwork_url, p.context_uri) for p in plays],
            )
            self._conn.commit()
            return self._conn.total_changes - before

    def recent_plays(self, limit: int = 20) -> list[Play]:
        rows = self._conn.execute(
            "SELECT * FROM plays ORDER BY played_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Play(**{k: r[k] for k in r.keys()}) for r in rows]

    def play_count(self, since: str | None = None) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM plays WHERE played_at >= ?", (since or "",)
        ).fetchone()
        return row["n"]

    def top_albums(self, since: str | None = None, limit: int = 20) -> list[PlayCount]:
        rows = self._conn.execute(
            """SELECT album_uri AS uri, album_name AS name, MIN(artist) AS artist,
                      MAX(artwork_url) AS artwork_url, COUNT(*) AS plays,
                      MAX(played_at) AS last_played_at
               FROM plays WHERE played_at >= ?
               GROUP BY album_uri ORDER BY plays DESC, last_played_at DESC LIMIT ?""",
            (since or "", limit),
        ).fetchall()
        return [PlayCount(uri=r["uri"], content_type="album", name=r["name"], artist=r["artist"],
                          artwork_url=r["artwork_url"], plays=r["plays"],
                          last_played_at=r["last_played_at"]) for r in rows]

    def top_contexts(self, since: str | None = None, limit: int = 20) -> list[PlayCount]:
        """Playlists/artists played from, with names from the library cache when known."""
        rows = self._conn.execute(
            """SELECT p.context_uri AS uri, COUNT(*) AS plays, MAX(p.played_at) AS last_played_at,
                      l.name AS name, l.artist AS artist, l.artwork_url AS artwork_url
               FROM plays p LEFT JOIN library l ON l.uri = p.context_uri
               WHERE p.played_at >= ? AND p.context_uri IS NOT NULL
                 AND p.context_uri NOT LIKE 'spotify:album:%'
                 AND (p.context_uri LIKE 'spotify:playlist:%' OR p.context_uri LIKE 'spotify:artist:%')
               GROUP BY p.context_uri ORDER BY plays DESC, last_played_at DESC LIMIT ?""",
            (since or "", limit),
        ).fetchall()
        return [PlayCount(uri=r["uri"], content_type=r["uri"].split(":")[1], name=r["name"],
                          artist=r["artist"], artwork_url=r["artwork_url"], plays=r["plays"],
                          last_played_at=r["last_played_at"]) for r in rows]

    def card_uris(self) -> set[str]:
        rows = self._conn.execute("SELECT uri FROM cards WHERE uri IS NOT NULL").fetchall()
        return {r["uri"] for r in rows}

    # --- library cache (names/artwork for uris seen in history) -------------

    def library_get(self, uri: str) -> LibraryItem | None:
        row = self._conn.execute("SELECT * FROM library WHERE uri = ?", (uri,)).fetchone()
        return LibraryItem(**{k: row[k] for k in row.keys()}) if row else None

    def library_put(
        self, uri: str, content_type: str, name: str | None, artist: str | None,
        artwork_url: str | None, available: bool = True,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO library (uri, content_type, name, artist, artwork_url, available, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (uri, content_type, name, artist, artwork_url, int(available), _now()),
            )
            self._conn.commit()


def _to_card(row: sqlite3.Row) -> Card:
    data = {k: row[k] for k in row.keys()}
    data["options"] = CardOptions.from_json(data.get("options"))
    return Card(**data)
