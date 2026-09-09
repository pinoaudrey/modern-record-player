import pytest
from fastapi.testclient import TestClient

from vinyl.player import Player
from vinyl.reader import FakeReader
from vinyl.spotify import ResolvedContent
from vinyl.web import create_app

NOW = ResolvedContent(uri="spotify:playlist:1FIFVq4IwEPDm6sqXItXVc", content_type="playlist",
                      name="Late Night Drive", artist="audrey", artwork_url="http://art")


@pytest.fixture
def client(db, fake_sounds, fake_spotify):
    reader = FakeReader()
    player = Player(db, fake_spotify, reader, fake_sounds, scan_cooldown=0.0, write_timeout=0.05)
    app = create_app(db, fake_spotify, player, reader=reader)
    return TestClient(app), db, player, fake_spotify, reader


def test_index_empty(client):
    tc, *_ = client
    r = tc.get("/")
    assert r.status_code == 200
    assert "No cards registered" in r.text
    assert "Nothing playing" in r.text


def test_register_flow(client):
    tc, db, player, _, _ = client
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
    assert card.on_tag == 0
    assert player.pending_scan is None
    assert player.pending_write is None


def test_register_save_and_write_arms_a_write(client):
    tc, db, player, _, reader = client
    r = tc.post(
        "/register/save",
        data={"uid": "555", "uri": NOW.uri, "content_type": "playlist", "name": NOW.name, "write": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db.get_card("555").on_tag == 0
    assert player.pending_write.content.uri == NOW.uri

    reader.inject("555")
    player.handle_scan(reader.poll(0.1))
    assert db.get_card("555").on_tag == 1
    assert reader.tag_text("555") == NOW.uri


def test_register_write_without_uid_only_arms(client):
    tc, db, player, _, _ = client
    r = tc.post(
        "/register/save",
        data={"uid": "", "uri": NOW.uri, "content_type": "playlist", "name": NOW.name, "write": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db.list_cards() == []
    assert player.pending_write.content.name == NOW.name


def test_register_save_without_uid_or_write_is_an_error(client):
    tc, *_ = client
    r = tc.post("/register/save", data={"uid": "", "uri": NOW.uri, "content_type": "playlist", "name": "x"})
    assert r.status_code == 400


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
    tc, db, _, spotify, _ = client
    db.save_content_card("7", "spotify:album:a", "album", "Album", None, None)
    r = tc.post("/cards/7/play", follow_redirects=False)
    assert r.status_code == 303
    assert spotify.played == ["spotify:album:a"]


# --- now playing -> make a record --------------------------------------------

def test_index_shows_now_playing(client):
    tc, _, _, spotify, _ = client
    spotify.now = NOW
    r = tc.get("/")
    assert "Late Night Drive" in r.text
    assert "Make a record of this" in r.text


def test_index_when_not_authorized_links_to_auth(client):
    tc, _, _, spotify, _ = client
    spotify.authorized = False
    r = tc.get("/")
    assert 'href="/auth"' in r.text


def test_auth_page_and_completion(client):
    tc, _, _, spotify, _ = client
    spotify.authorized = False
    r = tc.get("/auth")
    assert "accounts.spotify.com/authorize" in r.text
    assert "won&#39;t load" in r.text or "won't load" in r.text

    r = tc.post("/auth", data={"redirect_url": "garbage"})
    assert r.status_code == 200 and "didn&#39;t work" in r.text
    assert spotify.authorized is False

    r = tc.post("/auth", data={"redirect_url": "http://127.0.0.1:8080/callback?code=AQabc"},
                follow_redirects=False)
    assert r.status_code == 303
    assert spotify.authorized is True


def test_make_record_of_now_playing(client):
    tc, db, player, spotify, reader = client
    spotify.now = NOW
    r = tc.post(
        "/write/arm",
        data={"uri": NOW.uri, "content_type": NOW.content_type, "name": NOW.name,
              "artist": NOW.artist, "artwork_url": NOW.artwork_url},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert player.pending_write.content == NOW

    r = tc.get("/")
    assert "Hold a card on the reader" in r.text

    reader.inject("321")
    player.handle_scan(reader.poll(0.1))
    card = db.get_card("321")
    assert card.uri == NOW.uri and card.on_tag == 1

    r = tc.get("/")
    assert "Wrote <strong>Late Night Drive</strong>" in r.text
    assert "on tag" in r.text

    r = tc.post("/write/dismiss", follow_redirects=False)
    assert player.last_write is None


def test_register_now_uses_current_playback(client):
    tc, _, _, spotify, _ = client
    spotify.now = NOW
    r = tc.post("/register/now", data={"uid": "12"})
    assert "Late Night Drive" in r.text
    assert "Save card" in r.text

    spotify.now = None
    r = tc.post("/register/now", data={"uid": "12"})
    assert "Nothing is playing" in r.text


def test_cancel_write(client):
    tc, _, player, _, _ = client
    player.arm_write(NOW)
    tc.post("/write/cancel", follow_redirects=False)
    assert player.pending_write is None


def test_write_existing_card(client):
    tc, db, player, _, _ = client
    db.save_content_card("7", "spotify:album:a", "album", "Album", "Artist", None)
    r = tc.post("/cards/7/write", follow_redirects=False)
    assert r.status_code == 303
    assert player.pending_write.content.uri == "spotify:album:a"
    assert tc.post("/cards/nope/write").status_code == 404


def test_status_stamp_changes_with_state(client):
    tc, _, player, _, _ = client
    before = tc.get("/api/status").json()
    assert before["pending_uid"] is None and before["write"] is None
    player.handle_scan("1")
    after = tc.get("/api/status").json()
    assert after["pending_uid"] == "1"
    assert after["stamp"] != before["stamp"]


def test_status_stamp_changes_when_tag_card_self_registers(client):
    tc, _, player, _, reader = client
    before = tc.get("/api/status").json()["stamp"]
    reader.inject("2", NOW.uri)
    player.handle_scan(reader.poll(0.1))
    assert tc.get("/api/status").json()["stamp"] != before


def test_dev_scan_endpoint_accepts_text(client):
    tc, _, _, _, reader = client
    r = tc.post("/dev/scan", data={"uid": "123", "text": NOW.uri})
    assert r.json() == {"injected": "123", "text": NOW.uri}
    assert reader.poll(0.1).text == NOW.uri
