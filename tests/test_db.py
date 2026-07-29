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
