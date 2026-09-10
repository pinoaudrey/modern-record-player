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


# --- lift the needle ---------------------------------------------------------

@pytest.fixture
def clock(monkeypatch):
    """A fake monotonic clock; advance it with clock[0] += seconds."""
    c = [1000.0]
    monkeypatch.setattr("vinyl.player.time.monotonic", lambda: c[0])
    return c


def lift_player(db, fake_spotify, fake_sounds, **kw):
    kw.setdefault("lift_to_pause", True)
    kw.setdefault("lift_timeout", 1.5)
    kw.setdefault("resume_window", 900.0)
    return Player(db, fake_spotify, FakeReader(), fake_sounds, scan_cooldown=2.0,
                  write_timeout=0.05, **kw)


def rest(player, clock, uid, seconds):
    """The card sits on the reader: polled every 0.5s, tick after each poll."""
    for _ in range(int(seconds / 0.5)):
        clock[0] += 0.5
        player.handle_scan(uid)
        player.tick()


def lift(player, clock, seconds):
    """Nothing on the reader: the loop's poll times out, tick still runs."""
    for _ in range(int(seconds / 0.5)):
        clock[0] += 0.5
        player.tick()


def test_lift_pauses(db, fake_spotify, fake_sounds, clock):
    db.save_content_card("42", "spotify:album:a", "album", "Album", None, None)
    player = lift_player(db, fake_spotify, fake_sounds)
    player.handle_scan("42")
    player.tick()
    assert fake_spotify.playing
    rest(player, clock, "42", 3.0)
    assert "pause" not in fake_spotify.calls and not player.lifted

    lift(player, clock, 1.0)                    # not yet: 1.0 < lift_timeout
    assert "pause" not in fake_spotify.calls
    lift(player, clock, 1.0)
    assert fake_spotify.calls == ["pause"]
    assert player.lifted and not fake_spotify.playing
    assert fake_spotify.played == ["spotify:album:a"]


def test_return_within_window_resumes_instead_of_replaying(db, fake_spotify, fake_sounds, clock):
    db.save_content_card("42", "spotify:album:a", "album", "Album", None, None)
    player = lift_player(db, fake_spotify, fake_sounds)
    player.handle_scan("42")
    lift(player, clock, 2.0)
    assert fake_spotify.calls == ["pause"]

    clock[0] += 60                              # back after a minute
    player.handle_scan("42")
    player.tick()
    assert fake_spotify.calls == ["pause", "resume"]
    assert fake_spotify.played == ["spotify:album:a"]   # not replayed
    assert fake_spotify.playing and not player.lifted
    assert db.get_card("42").play_count == 1

    rest(player, clock, "42", 3.0)              # resting again stays quiet
    assert fake_spotify.played == ["spotify:album:a"]
    lift(player, clock, 2.0)                    # and lifts again
    assert fake_spotify.calls == ["pause", "resume", "pause"]


def test_return_within_cooldown_after_lift_still_resumes(db, fake_spotify, fake_sounds, clock):
    # lift_timeout (1.5) is shorter than scan_cooldown (2.0): a card away for
    # 1.7s has been paused, so putting it back must resume, not stay quiet.
    db.save_content_card("42", "spotify:album:a", "album", "Album", None, None)
    player = lift_player(db, fake_spotify, fake_sounds)
    player.handle_scan("42")
    clock[0] += 1.7
    player.tick()
    assert fake_spotify.calls == ["pause"]
    player.handle_scan("42")
    assert fake_spotify.calls == ["pause", "resume"]
    assert fake_spotify.played == ["spotify:album:a"]


def test_return_after_window_replays_from_the_top(db, fake_spotify, fake_sounds, clock):
    db.save_content_card("42", "spotify:album:a", "album", "Album", None, None)
    player = lift_player(db, fake_spotify, fake_sounds, resume_window=900.0)
    player.handle_scan("42")
    lift(player, clock, 2.0)
    assert fake_spotify.calls == ["pause"]

    clock[0] += 901
    player.handle_scan("42")
    assert fake_spotify.calls == ["pause"]                      # no resume
    assert fake_spotify.played == ["spotify:album:a"] * 2       # played again
    assert db.get_card("42").play_count == 2


def test_different_card_while_lifted_plays_and_takes_over(db, fake_spotify, fake_sounds, clock):
    db.save_content_card("42", "spotify:album:a", "album", "A", None, None)
    db.save_content_card("43", "spotify:album:b", "album", "B", None, None)
    player = lift_player(db, fake_spotify, fake_sounds)
    player.handle_scan("42")
    lift(player, clock, 2.0)
    assert fake_spotify.calls == ["pause"]

    player.handle_scan("43")
    player.tick()
    assert fake_spotify.played == ["spotify:album:a", "spotify:album:b"]
    assert player.current_uid == "43"

    clock[0] += 10                              # the first card coming back is just a new play
    player.handle_scan("42")
    assert fake_spotify.calls == ["pause"]
    assert fake_spotify.played[-1] == "spotify:album:a"
    assert player.current_uid == "42"

    lift(player, clock, 2.0)                    # and it can be lifted like any other
    assert fake_spotify.calls == ["pause", "pause"]


def test_lift_while_spotify_already_paused_does_nothing(db, fake_spotify, fake_sounds, clock):
    db.save_content_card("42", "spotify:album:a", "album", "Album", None, None)
    player = lift_player(db, fake_spotify, fake_sounds)
    player.handle_scan("42")
    fake_spotify.playing = False                # paused from the phone
    lift(player, clock, 2.0)
    assert player.lifted
    assert fake_spotify.calls == []             # we don't fight the phone

    clock[0] += 1                               # back: a plain re-tap, cooldown rules
    player.handle_scan("42")
    assert fake_spotify.calls == []
    assert fake_spotify.played == ["spotify:album:a"] * 2


def test_lift_feature_off_does_nothing(db, fake_spotify, fake_sounds, clock):
    db.save_content_card("42", "spotify:album:a", "album", "Album", None, None)
    player = lift_player(db, fake_spotify, fake_sounds, lift_to_pause=False)
    player.handle_scan("42")
    lift(player, clock, 5.0)
    assert fake_spotify.calls == []
    assert fake_spotify.playing

    clock[0] += 0.5                             # old behaviour: lifted past cooldown, put back = start over
    player.handle_scan("42")
    assert fake_spotify.calls == []
    assert fake_spotify.played == ["spotify:album:a"] * 2


def test_lift_survives_spotify_errors(db, fake_spotify, fake_sounds, clock):
    db.save_content_card("42", "spotify:album:a", "album", "Album", None, None)
    player = lift_player(db, fake_spotify, fake_sounds)
    player.handle_scan("42")
    fake_spotify.playback_error = RuntimeError("offline")
    lift(player, clock, 2.0)                    # no exception out of tick
    assert player.lifted
    fake_spotify.playback_error = None
    clock[0] += 1
    player.handle_scan("42")                    # we never paused it: plain re-tap rules
    assert "resume" not in fake_spotify.calls


def test_control_and_unknown_cards_do_not_become_current(db, fake_spotify, fake_sounds, clock):
    db.save_content_card("42", "spotify:album:a", "album", "Album", None, None)
    db.save_control_card("1", "next")
    player = lift_player(db, fake_spotify, fake_sounds)
    player.handle_scan("42")
    player.handle_scan("1")
    player.handle_scan("999")
    assert player.current_uid == "42"
    lift(player, clock, 2.0)
    assert fake_spotify.calls == ["next", "pause"]


def test_web_play_is_not_on_the_reader(db, fake_spotify, fake_sounds, clock):
    db.save_content_card("42", "spotify:album:a", "album", "Album", None, None)
    player = lift_player(db, fake_spotify, fake_sounds)
    player.play_card(db.get_card("42"))         # the admin's Play button
    assert player.current_uid is None
    lift(player, clock, 5.0)
    assert fake_spotify.calls == []


# --- per-card options: shuffle, single, resume --------------------------------

def test_shuffle_option_is_set_before_playing(db, fake_spotify, fake_sounds):
    from vinyl.db import CardOptions
    db.save_content_card("42", "spotify:playlist:p", "playlist", "P", None, None)
    player = make_player(db, fake_spotify, fake_sounds)
    player.handle_scan("42")
    assert fake_spotify.shuffle_calls == []

    db.set_card_options("42", CardOptions(shuffle=True))
    player.handle_scan("42")
    assert fake_spotify.shuffle_calls == [True]
    db.set_card_options("42", CardOptions(shuffle=False))
    player.handle_scan("42")
    assert fake_spotify.shuffle_calls == [True, False]
    assert fake_spotify.played == ["spotify:playlist:p"] * 3


def test_single_track_stops_when_spotify_moves_on(db, fake_spotify, fake_sounds, clock):
    from vinyl.db import CardOptions
    db.save_content_card("42", "spotify:track:one", "track", "Song", None, None,
                         options=CardOptions(single=True))
    player = lift_player(db, fake_spotify, fake_sounds)
    player.handle_scan("42")
    assert fake_spotify.track_uri == "spotify:track:one"

    rest(player, clock, "42", 30.0)             # our track plays on; nothing happens
    assert fake_spotify.calls == []

    fake_spotify.track_uri = "spotify:track:autoplay-junk"   # Spotify starts something else
    rest(player, clock, "42", 4.5)              # checked at most every 5s
    assert fake_spotify.calls == []
    rest(player, clock, "42", 1.0)
    assert fake_spotify.calls == ["pause"]
    assert not fake_spotify.playing

    fake_spotify.playing = True                 # watch is over: a later play from the phone is left alone
    rest(player, clock, "42", 30.0)
    assert fake_spotify.calls == ["pause"]


def test_single_stops_watching_when_playback_ends(db, fake_spotify, fake_sounds, clock):
    from vinyl.db import CardOptions
    db.save_content_card("42", "spotify:track:one", "track", "Song", None, None,
                         options=CardOptions(single=True))
    player = lift_player(db, fake_spotify, fake_sounds)
    player.handle_scan("42")
    rest(player, clock, "42", 10.0)
    fake_spotify.playing = False                # track ended with autoplay off
    rest(player, clock, "42", 5.0)
    assert fake_spotify.calls == []             # nothing to pause
    fake_spotify.playing = True
    fake_spotify.track_uri = "spotify:track:other"
    rest(player, clock, "42", 10.0)
    assert fake_spotify.calls == []             # not watching any more


def test_single_waits_for_spotify_to_catch_up(db, fake_spotify, fake_sounds, clock):
    from vinyl.db import CardOptions
    db.save_content_card("42", "spotify:track:one", "track", "Song", None, None,
                         options=CardOptions(single=True))
    player = lift_player(db, fake_spotify, fake_sounds)
    player.handle_scan("42")
    fake_spotify.track_uri = "spotify:track:previous"   # API still reports the old track
    rest(player, clock, "42", 10.0)
    assert fake_spotify.calls == []
    fake_spotify.track_uri = "spotify:track:one"
    rest(player, clock, "42", 10.0)
    fake_spotify.track_uri = "spotify:track:next"
    rest(player, clock, "42", 6.0)
    assert fake_spotify.calls == ["pause"]


def test_single_album_stops_when_context_changes(db, fake_spotify, fake_sounds, clock):
    from vinyl.db import CardOptions
    db.save_content_card("42", "spotify:album:a", "album", "Album", None, None,
                         options=CardOptions(single=True))
    player = lift_player(db, fake_spotify, fake_sounds)
    player.handle_scan("42")
    rest(player, clock, "42", 10.0)
    fake_spotify.track_uri = "spotify:track:song-3"      # tracks change within the album
    rest(player, clock, "42", 10.0)
    assert fake_spotify.calls == []
    fake_spotify.context_uri = None                       # album over, autoplay radio
    rest(player, clock, "42", 6.0)
    assert fake_spotify.calls == ["pause"]


def test_single_survives_a_lift_and_resume(db, fake_spotify, fake_sounds, clock):
    from vinyl.db import CardOptions
    db.save_content_card("42", "spotify:track:one", "track", "Song", None, None,
                         options=CardOptions(single=True))
    player = lift_player(db, fake_spotify, fake_sounds)
    player.handle_scan("42")
    rest(player, clock, "42", 10.0)
    lift(player, clock, 10.0)                   # paused by us, watch suspended
    assert fake_spotify.calls == ["pause"]
    player.handle_scan("42")
    assert fake_spotify.calls == ["pause", "resume"]
    rest(player, clock, "42", 10.0)
    fake_spotify.track_uri = "spotify:track:next"
    rest(player, clock, "42", 6.0)
    assert fake_spotify.calls == ["pause", "resume", "pause"]


def test_resume_card_saves_position_on_lift_and_continues_later(db, fake_spotify, fake_sounds, clock):
    from vinyl.db import CardOptions
    db.save_content_card("42", "spotify:playlist:p", "playlist", "P", None, None,
                         options=CardOptions(resume=True))
    db.save_content_card("43", "spotify:album:b", "album", "B", None, None)
    player = lift_player(db, fake_spotify, fake_sounds)
    player.handle_scan("42")
    fake_spotify.track_uri, fake_spotify.position_ms = "spotify:track:seven", 83000
    lift(player, clock, 2.0)
    pos = db.get_position("42")
    assert (pos.track_uri, pos.position_ms) == ("spotify:track:seven", 83000)

    player.handle_scan("43")                    # something else, for an hour
    clock[0] += 3600
    player.handle_scan("42")
    assert fake_spotify.play_kwargs[-1] == ("spotify:playlist:p", 83000, "spotify:track:seven")
    assert db.get_position("42") is None        # consumed


def test_resume_card_saves_position_when_replaced(db, fake_spotify, fake_sounds, clock):
    from vinyl.db import CardOptions
    db.save_content_card("42", "spotify:track:one", "track", "Song", None, None,
                         options=CardOptions(resume=True))
    db.save_content_card("43", "spotify:album:b", "album", "B", None, None)
    player = lift_player(db, fake_spotify, fake_sounds)
    player.handle_scan("42")
    fake_spotify.position_ms = 45000
    player.handle_scan("43")                    # interrupted by another card
    pos = db.get_position("42")
    assert (pos.track_uri, pos.position_ms) == ("spotify:track:one", 45000)
    player.handle_scan("42")
    assert fake_spotify.play_kwargs[-1] == ("spotify:track:one", 45000, "spotify:track:one")


def test_resume_position_not_saved_when_phone_moved_on(db, fake_spotify, fake_sounds, clock):
    from vinyl.db import CardOptions
    db.save_content_card("42", "spotify:album:a", "album", "A", None, None,
                         options=CardOptions(resume=True))
    player = lift_player(db, fake_spotify, fake_sounds)
    player.handle_scan("42")
    fake_spotify.context_uri = "spotify:playlist:something-else"
    lift(player, clock, 2.0)
    assert db.get_position("42") is None


def test_resume_position_cleared_by_from_the_top_after_window(db, fake_spotify, fake_sounds, clock):
    from vinyl.db import CardOptions
    db.save_content_card("42", "spotify:album:a", "album", "A", None, None,
                         options=CardOptions(resume=True))
    player = lift_player(db, fake_spotify, fake_sounds)
    player.handle_scan("42")
    fake_spotify.position_ms = 20000
    lift(player, clock, 2.0)
    assert db.get_position("42") is not None
    clock[0] += 1000
    player.handle_scan("42")
    assert fake_spotify.play_kwargs[-1] == ("spotify:album:a", None, None)
    assert db.get_position("42") is None


def test_resume_falls_back_to_the_top_when_spotify_refuses(db, fake_spotify, fake_sounds):
    from vinyl.db import CardOptions
    db.save_content_card("42", "spotify:playlist:p", "playlist", "P", None, None,
                         options=CardOptions(resume=True))
    db.save_position("42", "spotify:track:gone", 5000)
    player = make_player(db, fake_spotify, fake_sounds)

    real_play = fake_spotify.play

    def picky_play(uri, position_ms=None, track_uri=None):
        if track_uri:
            raise RuntimeError("404 track not in context")
        real_play(uri)

    fake_spotify.play = picky_play
    player.handle_scan("42")
    assert fake_spotify.played == ["spotify:playlist:p"]
    assert db.get_position("42") is None


def test_no_position_saved_without_resume_option(db, fake_spotify, fake_sounds, clock):
    db.save_content_card("42", "spotify:album:a", "album", "A", None, None)
    player = lift_player(db, fake_spotify, fake_sounds)
    player.handle_scan("42")
    lift(player, clock, 2.0)
    assert db.get_position("42") is None


def test_write_request_carries_options(db, fake_spotify, fake_sounds):
    from vinyl.db import CardOptions
    reader = FakeReader()
    player = make_player(db, fake_spotify, fake_sounds, reader=reader)
    player.arm_write(CONTENT, options=CardOptions(single=True, shuffle=False))
    reader.inject("55")
    player.handle_scan(reader.poll(0.1))
    assert db.get_card("55").options == CardOptions(single=True, shuffle=False)
