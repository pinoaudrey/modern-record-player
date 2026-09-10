"""SpotifyClient against a stub spotipy object: the bits of the real client
that shape API calls (uris playback, queueing, track-list paging)."""

from pathlib import Path

import pytest

from vinyl.config import Config
from vinyl.spotify import MAX_PLAY_URIS, SpotifyClient

TRACK = "spotify:track:" + "t" * 22
ALBUM = "spotify:album:" + "a" * 22
PLAYLIST = "spotify:playlist:" + "p" * 22
ARTIST = "spotify:artist:" + "r" * 22


class StubSp:
    """Records the spotipy calls the client makes and pages a fake catalogue."""

    def __init__(self):
        self.calls = []
        self.album = [f"spotify:track:al{i}" for i in range(7)]
        # 250 tracks, a removed one (the API reports {"track": None}) and a local file
        self.playlist = [f"spotify:track:pl{i}" for i in range(250)]
        self.playlist.insert(3, None)
        self.playlist.append("spotify:local:file")

    def devices(self):
        return {"devices": [{"id": "dev1", "name": "Record Player"}]}

    def start_playback(self, **kw):
        self.calls.append(("start_playback", kw))

    def add_to_queue(self, uri, device_id=None):
        self.calls.append(("add_to_queue", uri, device_id))

    def _page(self, items, limit, offset, wrap):
        chunk = items[offset:offset + limit]
        return {"items": [wrap(u) for u in chunk],
                "next": "more" if offset + limit < len(items) else None}

    def album_tracks(self, album_id, limit=50, offset=0):
        assert limit <= 50
        self.calls.append(("album_tracks", limit, offset))
        return self._page(self.album, limit, offset, lambda u: {"uri": u})

    def playlist_items(self, playlist_id, fields=None, limit=100, offset=0):
        assert limit <= 100
        self.calls.append(("playlist_items", limit, offset))
        return self._page(self.playlist, limit, offset,
                          lambda u: {"track": None if u is None else {"uri": u, "type": "track"}})


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = Config(
        client_id="x", redirect_uri="http://127.0.0.1:8080/callback", device_name="Record Player",
        reader_driver="fake", reader_rst_pin=22, scan_cooldown=2.0, lift_to_pause=True,
        lift_timeout=1.5, resume_window=900, history_interval=300, web_host="0.0.0.0",
        web_port=8090, db_path=tmp_path / "db", sounds_dir=tmp_path / "s", root=tmp_path,
    )
    c = SpotifyClient(cfg)
    stub = StubSp()
    monkeypatch.setattr(type(c), "authorized", property(lambda self: True))
    c._sp = stub
    return c, stub


def test_play_with_uris_list(client):
    c, sp = client
    uris = [f"spotify:track:{i}" for i in range(MAX_PLAY_URIS + 10)]
    c.play("spotify:playlist:p", uris=uris)
    name, kw = sp.calls[-1]
    assert name == "start_playback" and kw["device_id"] == "dev1"
    assert kw["uris"] == uris[:MAX_PLAY_URIS] and "context_uri" not in kw

    c.play("spotify:playlist:p", uris=uris[:3], position_ms=5000, track_uri="spotify:track:1")
    kw = sp.calls[-1][1]
    assert kw["offset"] == {"uri": "spotify:track:1"} and kw["position_ms"] == 5000

    c.play("spotify:playlist:p", uris=uris[:3], position_ms=5000, track_uri="spotify:track:gone")
    kw = sp.calls[-1][1]
    assert "offset" not in kw and "position_ms" not in kw   # unknown start track: from the top

    c.play("spotify:playlist:p")                              # no pressing: the live context
    assert sp.calls[-1][1] == {"device_id": "dev1", "context_uri": "spotify:playlist:p"}


def test_queue(client):
    c, sp = client
    c.queue("spotify:track:q")
    assert sp.calls == [("add_to_queue", "spotify:track:q", "dev1")]


def test_content_tracks_pages_and_caps(client):
    c, sp = client
    assert c.content_tracks(TRACK) == [TRACK]
    with pytest.raises(ValueError):
        c.content_tracks(ARTIST)
    with pytest.raises(ValueError):
        c.content_tracks("spotify:album:short")

    assert c.content_tracks(ALBUM, limit=5) == sp.album[:5]
    assert c.content_tracks(ALBUM, limit=50) == sp.album
    sp.calls.clear()
    tracks = c.content_tracks(PLAYLIST, limit=200)
    assert tracks == [u for u in sp.playlist if u][:200]    # the removed one is skipped, not counted
    assert [x for x in sp.calls if x[0] == "playlist_items"] == [
        ("playlist_items", 100, 0), ("playlist_items", 100, 100), ("playlist_items", 1, 200),
    ]
    everything = c.content_tracks(PLAYLIST, limit=300)
    assert len(everything) == 250 and "spotify:local:file" not in everything
