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

    from vinyl.db import SCHEMA_VERSION
    db = Database(path)
    assert db.schema_version() == SCHEMA_VERSION
    card = db.get_card("old")
    assert card.name == "Old Album" and card.play_count == 3 and card.on_tag == 0
    db.save_content_card("old", "spotify:album:a", "album", "Old Album", None, None, on_tag=True)
    assert db.get_card("old").on_tag == 1
    assert db.play_count() == 0  # v3 tables exist
    from vinyl.db import CardOptions
    assert card.options == CardOptions()  # v4 column
    db.save_position("old", "spotify:track:t", 1)  # v4 table
    db.close()
    assert Database(path).schema_version() == SCHEMA_VERSION  # reopening doesn't re-run migrations


# --- per-card options and resume positions -----------------------------------

def test_card_options_default_and_roundtrip(db):
    from vinyl.db import CardOptions
    db.save_content_card("1", "spotify:album:a", "album", "A", None, None)
    assert db.get_card("1").options == CardOptions()
    db.set_card_options("1", CardOptions(single=True, resume=True, shuffle=False))
    assert db.get_card("1").options == CardOptions(single=True, resume=True, shuffle=False)
    db.set_card_options("1", CardOptions(shuffle=True))
    assert db.get_card("1").options.shuffle is True
    db.set_card_options("1", CardOptions())
    assert db.get_card("1").options.shuffle is None


def test_options_survive_reregister_unless_given(db):
    from vinyl.db import CardOptions
    db.save_content_card("1", "spotify:album:a", "album", "A", None, None,
                         options=CardOptions(single=True))
    db.save_content_card("1", "spotify:album:b", "album", "B", None, None)
    assert db.get_card("1").options == CardOptions(single=True)
    db.save_content_card("1", "spotify:album:c", "album", "C", None, None,
                         options=CardOptions(resume=True))
    assert db.get_card("1").options == CardOptions(resume=True)
    db.save_control_card("1", "next")
    assert db.get_card("1").options == CardOptions()


def test_options_json_is_forgiving():
    from vinyl.db import CardOptions
    assert CardOptions.from_json(None) == CardOptions()
    assert CardOptions.from_json("not json") == CardOptions()
    assert CardOptions.from_json("[1, 2]") == CardOptions()
    assert CardOptions.from_json('{"single": 1}') == CardOptions(single=True)
    assert CardOptions.from_json('{"shuffle": false}').shuffle is False
    assert CardOptions.from_json(CardOptions(resume=True).to_json()) == CardOptions(resume=True)


def test_positions(db):
    db.save_content_card("1", "spotify:album:a", "album", "A", None, None)
    assert db.get_position("1") is None
    db.save_position("1", "spotify:track:t", 12345)
    pos = db.get_position("1")
    assert (pos.uid, pos.track_uri, pos.position_ms) == ("1", "spotify:track:t", 12345)
    assert pos.saved_at
    db.save_position("1", "spotify:track:u", 99)
    assert db.get_position("1").position_ms == 99
    assert list(db.list_positions()) == ["1"]
    db.clear_position("1")
    assert db.get_position("1") is None
    db.clear_position("1")  # idempotent


def test_deleting_a_card_drops_its_position(db):
    db.save_content_card("1", "spotify:album:a", "album", "A", None, None)
    db.save_position("1", "spotify:track:t", 1)
    db.delete_card("1")
    assert db.get_position("1") is None


def test_migrates_v3_database(tmp_path):
    import sqlite3
    from vinyl.db import SCHEMA_VERSION, CardOptions, Database

    path = tmp_path / "v3.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE cards (
            uid TEXT PRIMARY KEY, kind TEXT NOT NULL, uri TEXT, content_type TEXT,
            name TEXT, artist TEXT, artwork_url TEXT, action TEXT,
            created_at TEXT NOT NULL, last_played_at TEXT, play_count INTEGER NOT NULL DEFAULT 0,
            on_tag INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE plays (played_at TEXT PRIMARY KEY, track_uri TEXT NOT NULL, track_name TEXT NOT NULL,
            artist TEXT, album_uri TEXT NOT NULL, album_name TEXT NOT NULL, artwork_url TEXT, context_uri TEXT);
        CREATE TABLE library (uri TEXT PRIMARY KEY, content_type TEXT NOT NULL, name TEXT, artist TEXT,
            artwork_url TEXT, available INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '3');
        INSERT INTO cards (uid, kind, uri, content_type, name, created_at, play_count, on_tag)
            VALUES ('old', 'content', 'spotify:album:a', 'album', 'Old Album', '2026-01-01', 3, 1);
    """)
    conn.commit()
    conn.close()

    db = Database(path)
    assert db.schema_version() == SCHEMA_VERSION == 4
    card = db.get_card("old")
    assert card.name == "Old Album" and card.on_tag == 1
    assert card.options == CardOptions()
    db.set_card_options("old", CardOptions(single=True))
    assert db.get_card("old").options.single is True
    db.save_position("old", "spotify:track:t", 5)
    assert db.get_position("old").position_ms == 5
    db.close()
    assert Database(path).schema_version() == SCHEMA_VERSION
