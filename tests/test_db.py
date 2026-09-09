import pytest


def test_save_and_get_content_card(db):
    db.save_content_card(
        uid="123", uri="spotify:album:x" + "0" * 21, content_type="album",
        name="Dreamland", artist="Glass Animals", artwork_url="http://img",
    )
    card = db.get_card("123")
    assert card.kind == "content"
    assert card.name == "Dreamland"
    assert card.play_count == 0


def test_reregister_overwrites(db):
    db.save_content_card("123", "spotify:album:a", "album", "First", None, None)
    db.save_content_card("123", "spotify:playlist:b", "playlist", "Second", None, None)
    card = db.get_card("123")
    assert card.content_type == "playlist"
    assert card.name == "Second"
    assert len(db.list_cards()) == 1


def test_control_card(db):
    db.save_control_card("999", "play_pause")
    card = db.get_card("999")
    assert card.kind == "control"
    assert card.action == "play_pause"
    assert card.uri is None


def test_control_card_rejects_unknown_action(db):
    with pytest.raises(ValueError):
        db.save_control_card("999", "self_destruct")


def test_content_to_control_conversion_clears_uri(db):
    db.save_content_card("123", "spotify:album:a", "album", "Album", None, None)
    db.save_control_card("123", "next")
    card = db.get_card("123")
    assert card.kind == "control"
    assert card.uri is None


def test_record_play(db):
    db.save_content_card("123", "spotify:album:a", "album", "Album", None, None)
    db.record_play("123")
    db.record_play("123")
    card = db.get_card("123")
    assert card.play_count == 2
    assert card.last_played_at is not None


def test_delete(db):
    db.save_content_card("123", "spotify:album:a", "album", "Album", None, None)
    db.delete_card("123")
    assert db.get_card("123") is None


def test_on_tag_flag_saved_and_cleared_on_reregister(db):
    db.save_content_card("1", "spotify:album:a", "album", "A", None, None, on_tag=True)
    assert db.get_card("1").on_tag == 1
    db.save_content_card("1", "spotify:album:b", "album", "B", None, None)
    assert db.get_card("1").on_tag == 0
    db.save_control_card("1", "next")
    assert db.get_card("1").on_tag == 0


def test_fresh_db_is_at_latest_schema(db):
    from vinyl.db import SCHEMA_VERSION
    assert db.schema_version() == SCHEMA_VERSION


def test_migrates_slice1_database(tmp_path):
    import sqlite3
    from vinyl.db import Database

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE cards (
            uid TEXT PRIMARY KEY, kind TEXT NOT NULL, uri TEXT, content_type TEXT,
            name TEXT, artist TEXT, artwork_url TEXT, action TEXT,
            created_at TEXT NOT NULL, last_played_at TEXT, play_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '1');
        INSERT INTO cards (uid, kind, uri, content_type, name, created_at, play_count)
            VALUES ('old', 'content', 'spotify:album:a', 'album', 'Old Album', '2026-01-01', 3);
    """)
    conn.commit()
    conn.close()

    db = Database(path)
    assert db.schema_version() == 2
    card = db.get_card("old")
    assert card.name == "Old Album" and card.play_count == 3 and card.on_tag == 0
    db.save_content_card("old", "spotify:album:a", "album", "Old Album", None, None, on_tag=True)
    assert db.get_card("old").on_tag == 1
    db.close()
    assert Database(path).schema_version() == 2  # reopening doesn't re-run migrations
