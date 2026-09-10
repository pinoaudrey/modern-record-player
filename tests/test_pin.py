"""Optional admin PIN: nothing changes without one; with one, a signed cookie gates the admin."""

import pytest
from fastapi.testclient import TestClient

from vinyl import web
from vinyl.player import Player
from vinyl.reader import FakeReader
from vinyl.web import PIN_COOKIE, cookie_is_valid, cookie_secret, create_app, sign_cookie


def make_client(db, fake_sounds, fake_spotify, pin=""):
    reader = FakeReader()
    player = Player(db, fake_spotify, reader, fake_sounds, scan_cooldown=0.0)
    app = create_app(db, fake_spotify, player, reader=reader, pin=pin)
    return TestClient(app)


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(web, "LOGIN_DELAY", 0.0)


def test_without_a_pin_everything_is_open(db, fake_sounds, fake_spotify):
    tc = make_client(db, fake_sounds, fake_spotify)
    for path in ("/", "/status", "/devices", "/register", "/api/status"):
        assert tc.get(path, follow_redirects=False).status_code == 200, path
    assert tc.get("/login", follow_redirects=False).headers["location"] == "/"
    assert "Log out" not in tc.get("/").text
    assert db.get_meta("web_salt") is None


def test_with_a_pin_pages_redirect_to_login(db, fake_sounds, fake_spotify):
    tc = make_client(db, fake_sounds, fake_spotify, pin="2468")
    r = tc.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2F"
    r = tc.get("/register?uid=5", follow_redirects=False)
    assert r.headers["location"] == "/login?next=%2Fregister%3Fuid%3D5"
    r = tc.post("/cards/1/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = tc.get("/login?next=%2Fregister%3Fuid%3D5")
    assert r.status_code == 200 and 'name="pin"' in r.text
    assert 'value="/register?uid=5"' in r.text


def test_health_manifest_and_dev_stay_open(db, fake_sounds, fake_spotify):
    tc = make_client(db, fake_sounds, fake_spotify, pin="2468")
    assert tc.get("/health", follow_redirects=False).status_code == 200
    assert tc.get("/api/health", follow_redirects=False).json()["ok"] in (True, False)
    assert tc.get("/manifest.webmanifest", follow_redirects=False).status_code == 200
    assert tc.get("/icon.png", follow_redirects=False).status_code == 200
    assert tc.post("/dev/scan", data={"uid": "1"}, follow_redirects=False).status_code == 200


def test_correct_pin_sets_cookie_and_grants_access(db, fake_sounds, fake_spotify):
    tc = make_client(db, fake_sounds, fake_spotify, pin="2468")
    r = tc.post("/login", data={"pin": "2468", "next": "/status"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/status"
    cookie = r.headers["set-cookie"]
    assert PIN_COOKIE in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie.replace("Lax", "lax")
    assert f"Max-Age={30 * 24 * 3600}" in cookie
    assert tc.get("/", follow_redirects=False).status_code == 200
    assert "Log out" in tc.get("/").text
    assert tc.get("/login", follow_redirects=False).headers["location"] == "/"

    r = tc.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert tc.get("/", follow_redirects=False).status_code == 303


def test_wrong_pin_fails_and_sleeps(db, fake_sounds, fake_spotify, monkeypatch):
    slept = []
    monkeypatch.setattr(web.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(web, "LOGIN_DELAY", 1.0)
    tc = make_client(db, fake_sounds, fake_spotify, pin="2468")
    r = tc.post("/login", data={"pin": "0000", "next": "/"}, follow_redirects=False)
    assert r.status_code == 401 and "isn" in r.text and "right" in r.text
    assert "set-cookie" not in r.headers
    assert slept == [1.0]
    assert tc.get("/", follow_redirects=False).status_code == 303


def test_forged_or_expired_cookie_is_refused(db, fake_sounds, fake_spotify):
    tc = make_client(db, fake_sounds, fake_spotify, pin="2468")
    tc.cookies.set(PIN_COOKIE, "9999999999.deadbeef")
    assert tc.get("/", follow_redirects=False).status_code == 303
    secret = cookie_secret(db, "2468")
    tc.cookies.set(PIN_COOKIE, sign_cookie(secret, 1))          # expired long ago
    assert tc.get("/", follow_redirects=False).status_code == 303
    tc.cookies.set(PIN_COOKIE, sign_cookie(secret, 4102444800))  # signed with the real secret
    assert tc.get("/", follow_redirects=False).status_code == 200


def test_cookie_survives_restart_but_not_a_pin_change(db, fake_sounds, fake_spotify):
    tc = make_client(db, fake_sounds, fake_spotify, pin="2468")
    r = tc.post("/login", data={"pin": "2468"}, follow_redirects=False)
    value = tc.cookies.get(PIN_COOKIE)
    assert value and cookie_is_valid(cookie_secret(db, "2468"), value)
    salt = db.get_meta("web_salt")
    assert salt and len(salt) == 32

    again = make_client(db, fake_sounds, fake_spotify, pin="2468")   # same db: same salt
    again.cookies.set(PIN_COOKIE, value)
    assert again.get("/", follow_redirects=False).status_code == 200
    assert db.get_meta("web_salt") == salt

    changed = make_client(db, fake_sounds, fake_spotify, pin="1357")
    changed.cookies.set(PIN_COOKIE, value)
    assert changed.get("/", follow_redirects=False).status_code == 303


def test_login_next_never_leaves_the_site(db, fake_sounds, fake_spotify):
    tc = make_client(db, fake_sounds, fake_spotify, pin="2468")
    r = tc.post("/login", data={"pin": "2468", "next": "https://evil.example/x"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    r = tc.post("/login", data={"pin": "2468", "next": "//evil.example/x"}, follow_redirects=False)
    assert r.headers["location"] == "/"
