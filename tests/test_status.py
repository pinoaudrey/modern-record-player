import threading

import pytest
from fastapi.testclient import TestClient

from vinyl import updates
from vinyl.history import HistoryPoller
from vinyl.player import Player
from vinyl.reader import FakeReader
from vinyl.status import Health, fmt_bytes, fmt_duration
from vinyl.updates import UpdateResult
from vinyl.web import create_app


def items(data, section):
    s = next(s for s in data["sections"] if s["name"] == section)
    return {i["label"]: i for i in s["items"]}


@pytest.fixture
def health(db, fake_spotify, tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "version", lambda repo: "abc1234 (2026-09-09)")
    monkeypatch.setattr("vinyl.status.shutil.which", lambda name: None)   # no systemctl on the host
    temp = tmp_path / "temp"
    temp.write_text("51234\n")
    reader = FakeReader()
    poller = HistoryPoller(db, fake_spotify)
    return Health(
        db, fake_spotify, reader, poller, repo=tmp_path, device_name="Record Player",
        backups_dir=tmp_path / "backups", check_updates=lambda repo: (0, None), cpu_temp_path=temp,
    )


@pytest.fixture
def client(db, fake_sounds, fake_spotify, health):
    reader = FakeReader()
    player = Player(db, fake_spotify, reader, fake_sounds, scan_cooldown=0.0)
    app = create_app(db, fake_spotify, player, reader=reader, poller=health._poller, health=health)
    return TestClient(app), fake_spotify, health


# --- the data -----------------------------------------------------------------

def test_collect_all_ok(health, fake_spotify):
    data = health.collect()
    assert [s["name"] for s in data["sections"]] == ["Player", "Spotify", "raspotify", "History", "System"]
    player = items(data, "Player")
    assert player["Version"]["value"] == "abc1234 (2026-09-09)"
    assert player["Reader"]["value"] == "fake"
    spotify = items(data, "Spotify")
    assert spotify["Connected"]["ok"] and spotify["Account"]["value"] == "Audrey"
    assert spotify["Device"]["ok"] and "Record Player" in spotify["Device"]["value"]
    assert spotify["Active device"]["value"] == "Record Player"
    system = items(data, "System")
    assert system["CPU temperature"] == {"label": "CPU temperature", "value": "51.2 C", "ok": True}
    assert system["Update"]["value"] == "up to date" and system["Update"]["ok"]
    assert system["Last backup"]["ok"] is False          # none yet is a warning
    assert data["update"]["behind"] == 0


def test_spotify_failures_are_warnings(health, fake_spotify):
    fake_spotify.me_error = RuntimeError("token expired")
    fake_spotify.devices = [{"id": "x", "name": "Kitchen", "is_active": False}]
    sp = items(health.collect(), "Spotify")
    assert sp["Account"]["ok"] is False and "token expired" in sp["Account"]["value"]
    assert sp["Device"]["ok"] is False and "Kitchen" in sp["Device"]["value"]
    assert sp["Active device"]["value"] == "none"

    fake_spotify.devices_error = RuntimeError("503")
    sp = items(health.collect(), "Spotify")
    assert sp["Device"]["ok"] is False and "Active device" not in sp

    fake_spotify.authorized = False
    sp = items(health.collect(), "Spotify")
    assert sp["Connected"]["ok"] is False and len(sp) == 1


def test_history_section(health, db):
    hist = items(health.collect(), "History")
    assert hist["Plays collected"]["value"] == "0"
    assert hist["Last poll"]["value"] == "never yet" and hist["Last poll"]["ok"] is False
    health._poller.poll_once()
    health._poller.last_error = "boom"
    hist = items(health.collect(), "History")
    assert hist["Last poll"]["ok"] is True
    assert hist["Last error"] == {"label": "Last error", "value": "boom", "ok": False}


def test_update_available_is_a_warning_and_cached(health):
    calls = []

    def check(repo):
        calls.append(repo)
        return (3, "Add label sheets")
    health._check_updates = check
    health.invalidate_updates()
    sysitems = items(health.collect(), "System")
    assert sysitems["Update"] == {"label": "Update", "value": "3 commits behind: Add label sheets", "ok": False}
    health.collect()
    assert len(calls) == 1                      # cached between page loads
    health.update_status(refresh=True)
    assert len(calls) == 2

    def broken(repo):
        raise updates.UpdateError("no upstream")
    health._check_updates = broken
    health.invalidate_updates()
    assert "no upstream" in items(health.collect(), "System")["Update"]["value"]
    assert health.collect()["ok"] is False


def test_reader_chip_warning_shows_up(health):
    class Reader:
        def status(self):
            return {"driver": "rc522", "chip": "not responding (0x00)", "ok": False}
    health._reader = Reader()
    player = items(health.collect(), "Player")
    assert player["Reader"]["value"] == "rc522"
    assert player["RC522 chip"]["ok"] is False


def test_raspotify_without_systemctl(health, monkeypatch):
    monkeypatch.setattr("vinyl.status.shutil.which", lambda name: None)
    assert items(health.collect(), "raspotify")["Service"]["value"].startswith("n/a")


def test_raspotify_with_systemctl(health, monkeypatch):
    import subprocess
    monkeypatch.setattr("vinyl.status.shutil.which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(
        "vinyl.status.subprocess.run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 3, stdout="inactive\n", stderr=""),
    )
    assert items(health.collect(), "raspotify")["Service"] == {"label": "Service", "value": "inactive", "ok": False}


def test_formatting_helpers():
    assert fmt_duration(59) == "0m"
    assert fmt_duration(3 * 3600 + 120) == "3h 2m"
    assert fmt_duration(2 * 86400 + 60) == "2d 0h 1m"
    assert fmt_bytes(512) == "512 B"
    assert fmt_bytes(12.5 * 1024 ** 3) == "12.5 GB"


# --- the routes ---------------------------------------------------------------

def test_status_page_and_health_json(client):
    tc, _, health = client
    health.backups_dir.mkdir()
    (health.backups_dir / "records-20260909-0400.db").write_bytes(b"")
    health._poller.poll_once()
    r = tc.get("/status")
    assert r.status_code == 200
    assert "Everything looks fine" in r.text
    assert "abc1234 (2026-09-09)" in r.text and "Record Player" in r.text
    assert "Update now" in r.text and 'href="/status"' in r.text
    assert "records-20260909-0400.db" in r.text
    j = tc.get("/api/health").json()
    assert j["version"] == "abc1234 (2026-09-09)" and j["ok"] is True
    assert items(j, "Spotify")["Account"]["value"] == "Audrey"
    assert "Status" in tc.get("/").text          # nav link


def test_status_page_warns_when_spotify_is_down(client):
    tc, spotify, _ = client
    spotify.me_error = RuntimeError("offline")
    r = tc.get("/status")
    assert r.status_code == 200
    assert "Something needs attention" in r.text and "offline" in r.text


def test_update_route_up_to_date(client, monkeypatch):
    tc, _, health = client
    monkeypatch.setattr(updates, "apply", lambda repo, restart=True, user=None: UpdateResult("aaa", "aaa", False))
    r = tc.post("/update")
    assert r.status_code == 200 and "Already up to date" in r.text


def test_update_route_schedules_restart_when_sudo_allows(client, monkeypatch):
    tc, _, health = client
    timers = []

    class FakeTimer:
        def __init__(self, delay, fn, args=(), kwargs=None):
            timers.append((delay, fn))

        def start(self):
            pass

    monkeypatch.setattr(updates, "apply", lambda repo, restart=True, user=None: UpdateResult("aaa", "bbb", True))
    monkeypatch.setattr(updates, "can_sudo", lambda argv: True)
    monkeypatch.setattr("vinyl.web.threading.Timer", FakeTimer)
    r = tc.post("/update")
    assert r.status_code == 200
    assert "Updated aaa -&gt; bbb" in r.text or "Updated aaa -> bbb" in r.text
    assert "Restarting the service" in r.text
    assert timers and timers[0][1] is updates.restart_service


def test_update_route_warns_when_restart_is_not_allowed(client, monkeypatch):
    tc, _, _ = client
    monkeypatch.setattr(updates, "apply", lambda repo, restart=True, user=None: UpdateResult("aaa", "bbb", True))
    monkeypatch.setattr(updates, "can_sudo", lambda argv: False)
    r = tc.post("/update")
    assert r.status_code == 200
    assert "restart" in r.text.lower() and "install-pi.sh" in r.text
    assert "Restarting the service" not in r.text


def test_update_route_shows_pull_failure(client, monkeypatch):
    tc, _, _ = client

    def fail(repo, restart=True, user=None):
        raise updates.UpdateError("fatal: could not read from remote")
    monkeypatch.setattr(updates, "apply", fail)
    r = tc.post("/update")
    assert r.status_code == 200 and "Update failed: fatal" in r.text


def test_reboot_route_refuses_without_sudo(client, monkeypatch):
    tc, _, _ = client
    monkeypatch.setattr(updates, "can_sudo", lambda argv: False)
    r = tc.post("/reboot")
    assert r.status_code == 200 and "may not reboot" in r.text
