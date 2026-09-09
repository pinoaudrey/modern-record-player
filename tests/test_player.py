import pytest

from vinyl.player import Player
from vinyl.reader import FakeReader, Scan
from vinyl.spotify import ResolvedContent

URI = "spotify:album:22py1IeIi51c0GBYEHQTsI"
CONTENT = ResolvedContent(uri=URI, content_type="album", name="Dreamland",
                          artist="Glass Animals", artwork_url="http://img")


def make_player(db, fake_spotify, fake_sounds, cooldown=0.0, reader=None):
    return Player(db, fake_spotify, reader or FakeReader(), fake_sounds,
                  scan_cooldown=cooldown, write_timeout=0.05)


def test_known_content_card_plays(db, fake_spotify, fake_sounds):
    db.save_content_card("42", "spotify:album:a", "album", "Album", None, None)
    player = make_player(db, fake_spotify, fake_sounds)
    player.handle_scan("42")
    assert fake_spotify.played == ["spotify:album:a"]
    assert "accept" in fake_sounds.played
    assert db.get_card("42").play_count == 1


def test_unknown_card_becomes_pending(db, fake_spotify, fake_sounds):
    player = make_player(db, fake_spotify, fake_sounds)
    player.handle_scan("777")
    assert player.pending_scan.uid == "777"
    assert fake_spotify.played == []
    assert "error" in fake_sounds.played


def test_unknown_card_with_garbage_text_is_pending(db, fake_spotify, fake_sounds):
    player = make_player(db, fake_spotify, fake_sounds)
    player.handle_scan(Scan(uid="777", text="hello world"))
    assert player.pending_scan.uid == "777"


def test_clear_pending_only_matching_uid(db, fake_spotify, fake_sounds):
    player = make_player(db, fake_spotify, fake_sounds)
    player.handle_scan("777")
    player.clear_pending("111")
    assert player.pending_scan.uid == "777"
    player.clear_pending("777")
    assert player.pending_scan is None


def test_control_card_dispatches(db, fake_spotify, fake_sounds):
    db.save_control_card("1", "next")
    db.save_control_card("2", "shuffle")
    player = make_player(db, fake_spotify, fake_sounds)
    player.handle_scan("1")
    player.handle_scan("2")
    assert fake_spotify.calls == ["next", "shuffle"]
    assert "shuffle_on" in fake_sounds.played


def test_cooldown_ignores_repeat_scans(db, fake_spotify, fake_sounds):
    db.save_content_card("42", "spotify:album:a", "album", "Album", None, None)
    player = make_player(db, fake_spotify, fake_sounds, cooldown=60.0)
    player.handle_scan("42")
    player.handle_scan("42")
    assert fake_spotify.played == ["spotify:album:a"]


def test_card_resting_on_reader_stays_quiet(db, fake_spotify, fake_sounds, monkeypatch):
    db.save_content_card("42", "spotify:album:a", "album", "Album", None, None)
    player = make_player(db, fake_spotify, fake_sounds, cooldown=2.0)
    clock = [1000.0]
    monkeypatch.setattr("vinyl.player.time.monotonic", lambda: clock[0])

    player.handle_scan("42")                    # placed on the reader
    for _ in range(10):                         # polled every 0.5s for 5s
        clock[0] += 0.5
        player.handle_scan("42")
    assert fake_spotify.played == ["spotify:album:a"]

    clock[0] += 2.5                             # lifted, put back
    player.handle_scan("42")
    assert fake_spotify.played == ["spotify:album:a"] * 2


def test_different_card_bypasses_cooldown(db, fake_spotify, fake_sounds):
    db.save_content_card("42", "spotify:album:a", "album", "A", None, None)
    db.save_content_card("43", "spotify:album:b", "album", "B", None, None)
    player = make_player(db, fake_spotify, fake_sounds, cooldown=60.0)
    player.handle_scan("42")
    player.handle_scan("43")
    assert fake_spotify.played == ["spotify:album:a", "spotify:album:b"]


# --- self-describing cards ---------------------------------------------------

def test_unknown_card_with_uri_on_tag_registers_and_plays(db, fake_spotify, fake_sounds):
    player = make_player(db, fake_spotify, fake_sounds)
    player.handle_scan(Scan(uid="900", text=URI))
    card = db.get_card("900")
    assert card.name == "Dreamland"
    assert card.on_tag == 1
    assert fake_spotify.played == [URI]
    assert player.pending_scan is None


def test_tag_card_plays_even_when_lookup_fails(db, fake_spotify, fake_sounds):
    fake_spotify.resolve_error = RuntimeError("offline")
    player = make_player(db, fake_spotify, fake_sounds)
    player.handle_scan(Scan(uid="900", text=URI))
    card = db.get_card("900")
    assert card.uri == URI
    assert card.name.startswith("album ")
    assert fake_spotify.played == [URI]


def test_db_wins_over_tag_for_known_cards(db, fake_spotify, fake_sounds):
    db.save_content_card("900", "spotify:album:other", "album", "Other", None, None)
    player = make_player(db, fake_spotify, fake_sounds)
    player.handle_scan(Scan(uid="900", text=URI))
    assert fake_spotify.played == ["spotify:album:other"]


# --- writing -----------------------------------------------------------------

def test_armed_write_writes_next_card_and_registers(db, fake_spotify, fake_sounds):
    reader = FakeReader()
    player = make_player(db, fake_spotify, fake_sounds, cooldown=2.0, reader=reader)
    player.arm_write(CONTENT)
    assert player.pending_write.content.name == "Dreamland"

    reader.inject("55")
    player.handle_scan(reader.poll(0.1))

    assert reader.tag_text("55") == URI
    card = db.get_card("55")
    assert card.uri == URI and card.on_tag == 1
    assert player.pending_write is None
    assert player.last_write.ok and player.last_write.uid == "55"
    assert "written" in fake_sounds.played
    assert fake_spotify.played == []          # writing is not playing

    player.handle_scan("55")                  # card still resting there
    assert fake_spotify.played == []


def test_armed_write_bypasses_cooldown_for_same_card(db, fake_spotify, fake_sounds):
    reader = FakeReader()
    player = make_player(db, fake_spotify, fake_sounds, cooldown=60.0, reader=reader)
    reader.inject("55")
    player.handle_scan(reader.poll(0.1))       # unknown, pending
    assert player.pending_scan.uid == "55"

    player.arm_write(CONTENT)
    reader.inject("55")
    player.handle_scan(reader.poll(0.1))       # same card, within cooldown
    assert player.last_write.ok
    assert player.pending_scan is None


def test_write_failure_is_reported_and_disarms(db, fake_spotify, fake_sounds):
    class BrokenReader(FakeReader):
        def write(self, text, timeout):
            raise RuntimeError("card did not accept the write")

    player = make_player(db, fake_spotify, fake_sounds, reader=BrokenReader())
    player.arm_write(CONTENT)
    player.handle_scan("55")
    assert player.pending_write is None
    assert player.last_write.ok is False
    assert "did not accept" in player.last_write.error
    assert db.get_card("55") is None
    assert "error" in fake_sounds.played


def test_cancel_write(db, fake_spotify, fake_sounds):
    player = make_player(db, fake_spotify, fake_sounds)
    player.arm_write(CONTENT)
    player.cancel_write()
    assert player.pending_write is None
    player.handle_scan("55")
    assert player.pending_scan.uid == "55"
