from dataclasses import dataclass, field

import pytest

from vinyl.db import Database
from vinyl.spotify import CurrentTrack, RecentPlay, ResolvedContent


@dataclass
class FakeSpotify:
    """Stands in for SpotifyClient in player/web tests. Records calls."""

    played: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    shuffle_state: bool = False
    device: str | None = "raspotify-device-id"
    authorized: bool = True
    now: ResolvedContent | None = None
    resolve_error: Exception | None = None
    recent: list = field(default_factory=list)
    top: list = field(default_factory=list)          # [(ResolvedContent, n)]
    artists: list = field(default_factory=list)
    top_error: Exception | None = None
    resolve_calls: int = 0
    # playback state, for lift / single / resume tests
    playing: bool = False
    track_uri: str | None = None
    context_uri: str | None = None
    position_ms: int = 0
    nothing_loaded: bool = False                  # current_track() returns None
    play_kwargs: list = field(default_factory=list)   # (uri, position_ms, track_uri) per play()
    shuffle_calls: list = field(default_factory=list)
    playback_error: Exception | None = None       # raised by the playback-state calls

    # status page: account + Spotify Connect device list
    device_name: str = "Record Player"
    display_name: str | None = "Audrey"
    devices: list = field(default_factory=lambda: [
        {"id": "raspotify-device-id", "name": "Record Player", "is_active": True},
    ])
    me_error: Exception | None = None
    devices_error: Exception | None = None
    # device picker: the device actually used when device_name wasn't found
    last_fallback: str | None = None

    def me(self):
        if self.me_error:
            raise self.me_error
        return {"display_name": self.display_name, "id": "audrey"}

    def list_devices(self):
        if self.devices_error:
            raise self.devices_error
        return self.devices

    def set_device_name(self, name):
        self.device_name = name
        self.last_fallback = None
        self.calls.append(f"set_device_name:{name}")

    def authorize_url(self):
        return "https://accounts.spotify.com/authorize?client_id=x&code_challenge=y"

    def complete_authorization(self, redirect_url):
        if "code=" not in redirect_url:
            raise ValueError("No authorization code found in what you pasted.")
        self.authorized = True

    def recently_played(self, limit=50):
        return self.recent

    def top_albums(self, time_range="medium_term", limit=10):
        if self.top_error:
            raise self.top_error
        return self.top

    def top_artists(self, time_range="medium_term", limit=10):
        if self.top_error:
            raise self.top_error
        return self.artists

    def resolve(self, ref):
        self.resolve_calls += 1
        if self.resolve_error:
            raise self.resolve_error
        return ResolvedContent(
            uri=ref.uri, content_type=ref.type, name="Dreamland",
            artist="Glass Animals", artwork_url="http://img",
        )

    def now_playing_content(self):
        return self.now

    def play(self, uri, position_ms=None, track_uri=None):
        self.played.append(uri)
        self.play_kwargs.append((uri, position_ms, track_uri))
        self.playing = True
        self.nothing_loaded = False
        self.position_ms = position_ms or 0
        if uri.startswith("spotify:track:"):
            self.track_uri, self.context_uri = uri, None
        else:
            self.context_uri = uri
            self.track_uri = track_uri or f"spotify:track:first-of-{uri.split(':')[-1]}"

    def _check(self):
        if self.playback_error:
            raise self.playback_error

    def is_playing(self):
        self._check()
        return self.playing

    def pause(self):
        self._check()
        self.playing = False
        self.calls.append("pause")

    def resume(self):
        self._check()
        self.playing = True
        self.calls.append("resume")

    def current_track(self):
        self._check()
        if self.nothing_loaded or self.track_uri is None:
            return None
        return CurrentTrack(track_uri=self.track_uri, is_playing=self.playing,
                            position_ms=self.position_ms, context_uri=self.context_uri)

    def set_shuffle(self, state):
        self.shuffle_state = state
        self.shuffle_calls.append(state)

    def play_pause(self):
        self.calls.append("play_pause")

    def next_track(self):
        self.calls.append("next")

    def prev_track(self):
        self.calls.append("prev")

    def toggle_shuffle(self):
        self.shuffle_state = not self.shuffle_state
        self.calls.append("shuffle")
        return self.shuffle_state

    def switch_device(self):
        self.calls.append("switch_device")
        return "Kitchen Speaker"


class FakeSounds:
    def __init__(self):
        self.played = []

    def play(self, cue):
        self.played.append(cue)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    yield d
    d.close()


@pytest.fixture
def fake_spotify():
    return FakeSpotify()


@pytest.fixture
def fake_sounds():
    return FakeSounds()
