from vinyl.player import Player
from vinyl.reader import FakeReader


def make_player(db, fake_spotify, fake_sounds, cooldown=0.0):
    return Player(db, fake_spotify, FakeReader(), fake_sounds, scan_cooldown=cooldown)


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


def test_different_card_bypasses_cooldown(db, fake_spotify, fake_sounds):
    db.save_content_card("42", "spotify:album:a", "album", "A", None, None)
    db.save_content_card("43", "spotify:album:b", "album", "B", None, None)
    player = make_player(db, fake_spotify, fake_sounds, cooldown=60.0)
    player.handle_scan("42")
    player.handle_scan("43")
    assert fake_spotify.played == ["spotify:album:a", "spotify:album:b"]
