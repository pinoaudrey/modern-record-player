"""Device picker: the visible fallback banner, /devices, and the config rewrite."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vinyl.config import Config, save_device_name
from vinyl.history import HistoryPoller
from vinyl.player import Player
from vinyl.reader import FakeReader
from vinyl.spotify import SpotifyClient
from vinyl.status import Health
from vinyl.web import create_app

EXAMPLE = Path(__file__).parent.parent / "config.example.toml"


@pytest.fixture
def client(db, fake_sounds, fake_spotify, tmp_path, monkeypatch):
    monkeypatch.setattr("vinyl.status.shutil.which", lambda name: None)
    config = tmp_path / "config.toml"
    config.write_text(EXAMPLE.read_text())
    reader = FakeReader()
    player = Player(db, fake_spotify, reader, fake_sounds, scan_cooldown=0.0)
    poller = HistoryPoller(db, fake_spotify)
    health = Health(db, fake_spotify, reader, poller, repo=tmp_path, device_name=fake_spotify.device_name,
                    check_updates=lambda repo: (0, None))
    app = create_app(db, fake_spotify, player, reader=reader, poller=poller, health=health,
                     config_path=config)
    return TestClient(app), fake_spotify, health, config


# --- the banner ---------------------------------------------------------------

def test_index_has_no_banner_when_configured_device_is_used(client):
    tc, *_ = client
    r = tc.get("/")
    assert "wasn&#39;t found" not in r.text and "wasn't found" not in r.text
    assert 'href="/devices"' not in r.text


def test_index_banner_appears_on_fallback_and_disappears_after_pick(client):
    tc, spotify, health, _ = client
    spotify.last_fallback = "Kitchen Speaker"
    spotify.devices.append({"id": "k", "name": "Kitchen Speaker", "is_active": True, "type": "Speaker"})
    r = tc.get("/")
    assert "Playing on <strong>Kitchen Speaker</strong>" in r.text
    assert "<strong>Record Player</strong> wasn" in r.text
    assert 'href="/devices"' in r.text

    r = tc.post("/devices/select", data={"name": "Kitchen Speaker"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert spotify.device_name == "Kitchen Speaker"
    assert spotify.last_fallback is None
    assert health.device_name == "Kitchen Speaker"
    r = tc.get("/")
    assert "Playing on <strong>" not in r.text


# --- the picker page ------------------------------------------------------------

def test_devices_page_marks_configured_and_active(client):
    tc, spotify, *_ = client
    spotify.devices = [
        {"id": "r", "name": "Record Player", "is_active": False, "type": "Speaker"},
        {"id": "k", "name": "Kitchen Speaker", "is_active": True, "type": "Speaker"},
    ]
    r = tc.get("/devices")
    assert r.status_code == 200
    assert "Kitchen Speaker" in r.text and "Record Player" in r.text
    assert r.text.count("Use this") == 1              # not offered for the configured one
    assert ">configured<" in r.text and ">active<" in r.text


def test_devices_page_when_not_connected_or_failing(client):
    tc, spotify, *_ = client
    spotify.authorized = False
    assert 'href="/auth"' in tc.get("/devices").text
    spotify.authorized = True
    spotify.devices_error = RuntimeError("503 from Spotify")
    r = tc.get("/devices")
    assert r.status_code == 200 and "503 from Spotify" in r.text
    spotify.devices_error = None
    spotify.devices = []
    assert "No Spotify Connect devices" in tc.get("/devices").text


def test_status_device_row_links_to_picker(client):
    tc, spotify, *_ = client
    r = tc.get("/status")
    assert 'href="/devices"' in r.text
    spotify.devices = [{"id": "k", "name": "Kitchen", "is_active": True}]
    r = tc.get("/status")
    assert "pick a device" in r.text


# --- selecting ----------------------------------------------------------------

def test_select_rewrites_only_the_device_name_line(client):
    tc, spotify, _, config = client
    spotify.devices.append({"id": "k", "name": "Kitchen Speaker", "is_active": False})
    before = config.read_bytes()
    r = tc.post("/devices/select", data={"name": "kitchen speaker", "next": "/devices"},
                follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/devices"
    assert spotify.device_name == "Kitchen Speaker"        # the name as Spotify spells it
    after = config.read_bytes()
    assert after != before
    diff = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    assert diff == [(b'device_name = "raspotify"', b'device_name = "Kitchen Speaker"')]
    assert before.count(b"\n") == after.count(b"\n")


def test_select_unknown_device_is_rejected(client):
    tc, spotify, _, config = client
    before = config.read_bytes()
    r = tc.post("/devices/select", data={"name": "Garage"}, follow_redirects=False)
    assert r.status_code == 400
    assert spotify.device_name == "Record Player"
    assert config.read_bytes() == before

    spotify.authorized = False
    assert tc.post("/devices/select", data={"name": "Record Player"}).status_code == 400
    spotify.authorized = True
    spotify.devices_error = RuntimeError("down")
    assert tc.post("/devices/select", data={"name": "Record Player"}).status_code == 502


def test_select_redirect_stays_on_site(client):
    tc, spotify, *_ = client
    r = tc.post("/devices/select", data={"name": "Record Player", "next": "https://evil.example/"},
                follow_redirects=False)
    assert r.headers["location"] == "/"


def test_select_without_config_path_is_runtime_only(db, fake_sounds, fake_spotify):
    reader = FakeReader()
    player = Player(db, fake_spotify, reader, fake_sounds, scan_cooldown=0.0)
    app = create_app(db, fake_spotify, player, reader=reader)
    tc = TestClient(app)
    assert tc.post("/devices/select", data={"name": "Record Player"}, follow_redirects=False).status_code == 303


# --- save_device_name on its own ---------------------------------------------

def test_save_device_name_preserves_everything_else(tmp_path):
    path = tmp_path / "config.toml"
    original = EXAMPLE.read_text()
    path.write_text(original)
    save_device_name(path, 'Kitchen "Speaker"')
    new = path.read_text()
    assert new.replace('device_name = "Kitchen \\"Speaker\\""', 'device_name = "raspotify"') == original
    import tomllib
    assert tomllib.loads(new)["spotify"]["device_name"] == 'Kitchen "Speaker"'
    assert not (tmp_path / "config.toml.tmp").exists()


def test_save_device_name_adds_the_line_under_spotify_when_missing(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('# top\n[spotify]\nclient_id = "abc"\n\n[web]\nport = 1\n')
    save_device_name(path, "Den")
    assert path.read_text() == '# top\n[spotify]\ndevice_name = "Den"\nclient_id = "abc"\n\n[web]\nport = 1\n'


def test_save_device_name_adds_spotify_table_when_missing(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[web]\nport = 1")
    save_device_name(path, "Den")
    assert path.read_text() == '[web]\nport = 1\n\n[spotify]\ndevice_name = "Den"\n'


def test_save_device_name_ignores_device_name_in_other_tables(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[other]\ndevice_name = "x"\n[spotify]\ndevice_name = "y"   \n')
    save_device_name(path, "Den")
    assert path.read_text() == '[other]\ndevice_name = "x"\n[spotify]\ndevice_name = "Den"\n'


def test_save_device_name_keeps_crlf(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes(b'[spotify]\r\ndevice_name = "a"\r\n[web]\r\n')
    save_device_name(path, "b")
    assert path.read_bytes() == b'[spotify]\r\ndevice_name = "b"\r\n[web]\r\n'


# --- SpotifyClient.device_id records the fallback ----------------------------

class _Sp:
    def __init__(self, devices):
        self._devices = devices

    def devices(self):
        return {"devices": self._devices}


@pytest.fixture
def real_client(tmp_path, monkeypatch):
    cfg = Config(
        client_id="x", redirect_uri="http://127.0.0.1:8080/callback", device_name="Record Player",
        reader_driver="fake", reader_rst_pin=22, scan_cooldown=2.0, lift_to_pause=True,
        lift_timeout=1.5, resume_window=900, history_interval=300, web_host="0.0.0.0",
        web_port=8090, db_path=tmp_path / "r.db", sounds_dir=tmp_path / "s", root=tmp_path,
    )
    monkeypatch.setattr(SpotifyClient, "authorized", property(lambda self: True))
    return SpotifyClient(cfg)


def test_device_id_falls_back_and_records_it(real_client):
    real_client._sp = _Sp([{"id": "k", "name": "Kitchen"}])
    assert real_client.device_id() == "k"
    assert real_client.last_fallback == "Kitchen"

    real_client._sp = _Sp([{"id": "k", "name": "Kitchen"}, {"id": "r", "name": "record player"}])
    assert real_client.device_id() == "k"              # cached
    assert real_client.device_id(refresh=True) == "r"  # found again, case-insensitively
    assert real_client.last_fallback is None

    real_client._sp = _Sp([])
    assert real_client.device_id(refresh=True) is None
    assert real_client.last_fallback is None


def test_set_device_name_resets_cache_and_fallback(real_client):
    real_client._sp = _Sp([{"id": "k", "name": "Kitchen"}])
    real_client.device_id()
    assert real_client.last_fallback == "Kitchen"
    real_client.set_device_name("  Kitchen ")
    assert real_client.device_name == "Kitchen"
    assert real_client._device_id is None and real_client.last_fallback is None
    assert real_client.device_id() == "k"
    assert real_client.last_fallback is None


def test_config_loads_pin(tmp_path):
    from vinyl.config import load_config
    (tmp_path / "config.toml").write_text(EXAMPLE.read_text())
    assert load_config(tmp_path).web_pin == ""
    (tmp_path / "config.toml").write_text(EXAMPLE.read_text().replace('pin = ""', 'pin = "2468"'))
    assert load_config(tmp_path).web_pin == "2468"
    (tmp_path / "config.toml").write_text(EXAMPLE.read_text().replace('pin = ""', 'pin = 2468'))
    assert load_config(tmp_path).web_pin == "2468"
