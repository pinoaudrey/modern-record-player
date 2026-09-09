from vinyl.db import Play, since_days
from vinyl.history import HistoryPoller, build_report, describe_context
from vinyl.spotify import RecentPlay, ResolvedContent

ALBUM_A = "spotify:album:" + "a" * 22
ALBUM_B = "spotify:album:" + "b" * 22
PLAYLIST = "spotify:playlist:" + "c" * 22
EDITORIAL = "spotify:playlist:37i9dQZEVXbkfo4lPyVdsA"
ARTIST = "spotify:artist:" + "d" * 22


def play(i, album=ALBUM_A, context=None, when="2026-09-08T12:00:0"):
    return Play(
        played_at=f"{when}{i}.000Z", track_uri=f"spotify:track:{i:022d}", track_name=f"Track {i}",
        artist="Artist", album_uri=album, album_name="Album " + album[-1], artwork_url="http://art",
        context_uri=context,
    )


def test_add_plays_dedupes(db):
    assert db.add_plays([play(1), play(2)]) == 2
    assert db.add_plays([play(2), play(3)]) == 1
    assert db.play_count() == 3
    assert db.recent_plays(1)[0].track_name == "Track 3"


def test_top_albums_and_contexts(db):
    db.add_plays([play(1), play(2), play(3, album=ALBUM_B, context=PLAYLIST), play(4, context=PLAYLIST)])
    albums = db.top_albums()
    assert [(a.uri, a.plays) for a in albums] == [(ALBUM_A, 3), (ALBUM_B, 1)]
    contexts = db.top_contexts()
    assert [(c.uri, c.plays, c.name) for c in contexts] == [(PLAYLIST, 2, None)]


def test_since_filters_old_plays(db):
    db.add_plays([play(1, when="2020-01-01T00:00:0"), play(2)])
    assert db.play_count(since_days(30)) == 1
    assert db.play_count(since_days(30 * 12 * 20)) == 2


def test_poller_stores_recent_plays(db, fake_spotify):
    fake_spotify.recent = [RecentPlay(
        played_at="2026-09-08T12:00:00.000Z", track_uri="spotify:track:x", track_name="X",
        artist="A", album_uri=ALBUM_A, album_name="Album", artwork_url=None, context_uri=None,
    )]
    poller = HistoryPoller(db, fake_spotify)
    assert poller.poll_once() == 1
    assert poller.poll_once() == 0
    assert poller.last_poll_at is not None
    assert db.recent_plays()[0].track_name == "X"


def test_describe_context_caches_lookup(db, fake_spotify):
    db.add_plays([play(1, context=PLAYLIST)])
    pc = db.top_contexts()[0]
    first = describe_context(db, fake_spotify, pc)
    assert first.name == "Dreamland"
    describe_context(db, fake_spotify, pc)
    assert fake_spotify.resolve_calls == 1
    assert db.top_contexts()[0].name == "Dreamland"  # now served straight from the library


def test_describe_editorial_playlist_without_lookup(db, fake_spotify):
    db.add_plays([play(1, context=EDITORIAL)])
    c = describe_context(db, fake_spotify, db.top_contexts()[0])
    assert fake_spotify.resolve_calls == 0
    assert c.name == "Spotify curated playlist"
    assert c.uri == EDITORIAL and c.note


def test_describe_context_lookup_failure_falls_back(db, fake_spotify):
    fake_spotify.resolve_error = RuntimeError("offline")
    db.add_plays([play(1, context=ARTIST)])
    c = describe_context(db, fake_spotify, db.top_contexts()[0])
    assert c.content_type == "artist" and c.name.startswith("artist ")
    assert db.library_get(ARTIST).available == 0


def test_report_marks_cards_on_shelf(db, fake_spotify):
    db.add_plays([play(1), play(2), play(3, album=ALBUM_B)])
    db.save_content_card("1", ALBUM_A, "album", "Album a", None, None)
    fake_spotify.top = [(ResolvedContent(ALBUM_B, "album", "Album b", "Artist", None), 4)]
    fake_spotify.artists = [ResolvedContent(ARTIST, "artist", "Artist", None, None)]
    report = build_report(db, fake_spotify, "30d")
    assert report.label == "Last 30 days"
    assert report.plays_in_window == 3
    assert [(s.content.uri, s.has_card) for s in report.albums] == [(ALBUM_A, True), (ALBUM_B, False)]
    assert report.top_albums[0].plays == 4 and not report.top_albums[0].has_card
    assert report.top_artists[0].content.name == "Artist"
    assert report.spotify_error is None


def test_report_survives_spotify_failure(db, fake_spotify):
    fake_spotify.top_error = RuntimeError("no scope")
    report = build_report(db, fake_spotify, "nonsense")
    assert report.window == "30d"
    assert report.spotify_error == "no scope"
    assert report.top_albums == []
