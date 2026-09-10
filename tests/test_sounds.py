import wave

import pytest

from vinyl import sounds as sounds_mod
from vinyl.sounds import CUES, Sounds
from vinyl.tones import SAMPLE_RATE, generate, render


# --- generated cues -----------------------------------------------------------

def test_generate_writes_a_valid_wav_for_every_cue(tmp_path):
    paths = generate(tmp_path / "sounds")
    assert [p.name for p in paths] == [f"{c}.wav" for c in CUES]
    for p in paths:
        with wave.open(str(p)) as w:
            assert w.getnchannels() == 1
            assert w.getsampwidth() == 2
            assert w.getframerate() == SAMPLE_RATE
            assert w.getnframes() > SAMPLE_RATE // 20        # at least 50 ms
            frames = w.readframes(w.getnframes())
        assert any(frames)                                   # not silence


def test_cues_have_distinct_shapes():
    startup, accept, error = render("startup"), render("accept"), render("error")
    assert len(startup) > len(accept)
    assert startup[0] == 0 and startup[-1] == 0             # faded in and out, no click
    assert max(abs(s) for s in error) < max(abs(s) for s in accept)   # buzz is quieter than a beep
    up, down = render("shuffle_on"), render("shuffle_off")
    assert len(up) == len(down) and list(up) != list(down)
    with pytest.raises(ValueError):
        render("nope")


# --- Sounds: mp3 first, wav second, silent otherwise --------------------------

class FakePopen:
    calls: list = []

    def __init__(self, argv, **kwargs):
        FakePopen.calls.append(list(argv))


@pytest.fixture
def popen(monkeypatch):
    FakePopen.calls = []
    monkeypatch.setattr(sounds_mod.subprocess, "Popen", FakePopen)
    return FakePopen


def _which(available):
    return lambda name: f"/usr/bin/{name}" if name in available else None


def test_play_prefers_mp3_over_wav(tmp_path, monkeypatch, popen):
    monkeypatch.setattr(sounds_mod.shutil, "which", _which({"mpg123", "aplay"}))
    (tmp_path / "accept.mp3").write_bytes(b"")
    (tmp_path / "accept.wav").write_bytes(b"")
    (tmp_path / "error.wav").write_bytes(b"")
    s = Sounds(tmp_path)
    s.play("accept")
    s.play("error")
    s.play("written")                                        # no file at all
    assert popen.calls == [
        ["mpg123", "-q", str(tmp_path / "accept.mp3")],
        ["aplay", "-q", str(tmp_path / "error.wav")],
    ]


def test_play_uses_wav_when_mpg123_is_missing(tmp_path, monkeypatch, popen):
    monkeypatch.setattr(sounds_mod.shutil, "which", _which({"aplay"}))
    (tmp_path / "accept.mp3").write_bytes(b"")
    (tmp_path / "accept.wav").write_bytes(b"")
    Sounds(tmp_path).play("accept")
    assert popen.calls == [["aplay", "-q", str(tmp_path / "accept.wav")]]


def test_play_is_silent_without_any_player(tmp_path, monkeypatch, popen):
    monkeypatch.setattr(sounds_mod.shutil, "which", _which(set()))
    (tmp_path / "accept.mp3").write_bytes(b"")
    (tmp_path / "accept.wav").write_bytes(b"")
    Sounds(tmp_path).play("accept")
    assert popen.calls == []


def test_ensure_cues_generates_only_the_missing_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(sounds_mod.shutil, "which", _which({"aplay"}))
    (tmp_path / "accept.mp3").write_bytes(b"")
    s = Sounds(tmp_path)
    assert "accept" not in s.missing() and "startup" in s.missing()
    made = s.ensure_cues()
    assert sorted(p.name for p in made) == sorted(f"{c}.wav" for c in CUES if c != "accept")
    assert not (tmp_path / "accept.wav").exists()
    assert s.missing() == []
    assert s.ensure_cues() == []
