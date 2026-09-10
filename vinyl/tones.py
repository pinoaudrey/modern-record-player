"""Generated sound cues.

The original mp3 cues are gone, so the player can synthesise its own: plain
sine tones with a short fade in and out (no clicks), written as 16-bit mono
22050 Hz WAV files with the stdlib `wave` module. Nothing to install.

  python -m vinyl sounds        regenerate every cue as sounds/<cue>.wav
"""

import math
import sys
import wave
from array import array
from pathlib import Path

SAMPLE_RATE = 22050
FADE = 0.008          # seconds of linear fade at each end of a note
PEAK = 32767

# note frequencies used below
C5, E5, G5, A5, C6, D6 = 523.25, 659.25, 783.99, 880.0, 1046.5, 1174.66
A4 = 440.0


def _fade_gain(i: int, n: int, fade_n: int) -> float:
    if i < fade_n:
        return i / fade_n
    if i >= n - fade_n:
        return (n - 1 - i) / fade_n
    return 1.0


def tone(freq: float, seconds: float, volume: float = 0.5, end_freq: float | None = None) -> array:
    """A sine note. With `end_freq` the pitch glides there, phase-continuously."""
    n = int(SAMPLE_RATE * seconds)
    fade_n = max(1, min(int(SAMPLE_RATE * FADE), n // 2))
    out = array("h")
    phase = 0.0
    for i in range(n):
        f = freq if end_freq is None else freq + (end_freq - freq) * i / n
        phase += 2 * math.pi * f / SAMPLE_RATE
        out.append(int(PEAK * volume * _fade_gain(i, n, fade_n) * math.sin(phase)))
    return out


def buzz(freq: float, seconds: float, volume: float = 0.35) -> array:
    """A low square-wave buzz (a few odd harmonics, so it's rough but not harsh)."""
    n = int(SAMPLE_RATE * seconds)
    fade_n = max(1, min(int(SAMPLE_RATE * FADE), n // 2))
    out = array("h")
    for i in range(n):
        t = i / SAMPLE_RATE
        s = sum(math.sin(2 * math.pi * freq * k * t) / k for k in (1, 3, 5, 7))
        out.append(int(PEAK * volume * _fade_gain(i, n, fade_n) * s / 1.4))
    return out


def silence(seconds: float) -> array:
    return array("h", [0]) * int(SAMPLE_RATE * seconds)


def _join(*parts: array) -> array:
    out = array("h")
    for p in parts:
        out.extend(p)
    return out


def render(cue: str) -> array:
    """Samples for one cue name (see sounds.CUES)."""
    if cue == "startup":            # rising three-note chime
        return _join(tone(C5, 0.12), silence(0.02), tone(E5, 0.12), silence(0.02), tone(G5, 0.3))
    if cue == "accept":             # short pleasant double beep
        return _join(tone(A5, 0.07), silence(0.05), tone(D6, 0.09))
    if cue == "error":              # low buzz
        return buzz(110.0, 0.35)
    if cue == "written":            # quick ascending arpeggio
        return _join(tone(C5, 0.06), tone(E5, 0.06), tone(G5, 0.06), tone(C6, 0.14))
    if cue == "connect_device":     # two slow beeps
        return _join(tone(A4, 0.2), silence(0.25), tone(A4, 0.2))
    if cue == "shuffle_on":         # upward glide
        return tone(A4, 0.3, end_freq=A5)
    if cue == "shuffle_off":        # downward glide
        return tone(A5, 0.3, end_freq=A4)
    raise ValueError(f"unknown cue: {cue}")


def write_wav(path: Path, samples: array) -> None:
    if sys.byteorder == "big":
        samples = array("h", samples)
        samples.byteswap()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(samples.tobytes())


def generate(sounds_dir: Path, cues=None) -> list[Path]:
    """Write <cue>.wav for each cue (default: all of them). Returns the paths."""
    from .sounds import CUES

    sounds_dir = Path(sounds_dir)
    sounds_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for cue in cues or CUES:
        path = sounds_dir / f"{cue}.wav"
        write_wav(path, render(cue))
        out.append(path)
    return out
