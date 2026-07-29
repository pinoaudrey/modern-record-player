import pytest
from fastapi.testclient import TestClient

from vinyl.player import Player
from vinyl.reader import FakeReader
from vinyl.spotify import ResolvedContent
from vinyl.web import create_app


class ResolvingFakeSpotify:
    def __init__(self):
        self.played = []

    def resolve(self, ref):
        return ResolvedContent(
            uri=ref.uri, content_type=ref.type, name="Dreamland",
            artist="Glass Animals", artwork_url="http://img",
        )

    def play(self, uri):
        self.played.append(uri)


@pytest.fixture
def client(db, fake_sounds):
    spotify = ResolvingFakeSpotify()
    reader = FakeReader()
    player = Player(db, spotify, reader, fake_sounds, scan_cooldown=0.0)
    app = create_app(db, spotify, player, reader=reader)
    return TestClient(app), db, player, spotify


def test_index_empty(client):
    tc, *_ = client
    r = tc.get("/")
    assert r.status_code == 200
    assert "No cards registered" in r.text


def test_register_flow(client):
    tc, db, player, _ = client
    player.handle_scan("555")
    assert player.pending_scan.uid == "555"

    r = tc.post(
        "/register/resolve",
        data={"uid": "555", "link": "https://open.spotify.com/album/5bfpRtBW7RNRdsm3tRyl3R?si=x"},
    )
    assert "Dreamland" in r.text

    r = tc.post(
        "/register/save",
        data={
            "uid": "555",
            "uri": "spotify:album:5bfpRtBW7RNRdsm3tRyl3R",
            "content_type": "album",
            "name": "Dreamland",
            "artist": "Glass Animals",
            "artwork_url": "http://img",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    card = db.get_card("555")
    assert card.name == "Dreamland"
    assert player.pending_scan is None


def test_resolve_bad_link_shows_error(client):
    tc, *_ = client
    r = tc.post("/register/resolve", data={"uid": "555", "link": "https://youtube.com/watch?v=x"})
    assert "Could not find" in r.text


def test_register_control_card(client):
    tc, db, *_ = client
    r = tc.post("/register/control", data={"uid": "9", "action": "play_pause"}, follow_redirects=False)
    assert r.status_code == 303
    assert db.get_card("9").action == "play_pause"


def test_play_endpoint(client):
    tc, db, _, spotify = client
    db.save_content_card("7", "spotify:album:a", "album", "Album", None, None)
    r = tc.post("/cards/7/play", follow_redirects=False)
    assert r.status_code == 303
    assert spotify.played == ["spotify:album:a"]


def test_dev_scan_endpoint_present_with_fake_reader(client):
    tc, _, player, _ = client
    r = tc.post("/dev/scan", data={"uid": "123"})
    assert r.json() == {"injected": "123"}
