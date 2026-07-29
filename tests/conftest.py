from dataclasses import dataclass, field

import pytest

from vinyl.db import Database


@dataclass
class FakeSpotify:
    """Stands in for SpotifyClient in player/web tests. Records calls."""

    played: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    shuffle_state: bool = False
    device: str | None = "raspotify-device-id"

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
