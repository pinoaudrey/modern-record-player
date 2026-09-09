from dataclasses import dataclass, field

import pytest

from vinyl.db import Database
from vinyl.spotify import RecentPlay, ResolvedContent


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

    def play(self, uri):
        self.played.append(uri)

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
