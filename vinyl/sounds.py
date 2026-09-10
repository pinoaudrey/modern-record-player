"""Audio feedback cues, played without blocking the scan loop.

Each cue is <cue>.mp3 (played with mpg123) or, failing that, <cue>.wav
(played with `aplay -q`). Cues with neither file are generated as wav at
startup (see tones.py), so a fresh install still chirps.
"""

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

CUES = ("startup", "accept", "error", "written", "connect_device", "shuffle_on", "shuffle_off")


class Sounds:
    def __init__(self, sounds_dir: Path):
        self._dir = Path(sounds_dir)
        self._mpg123 = shutil.which("mpg123") is not None
        self._aplay = shutil.which("aplay") is not None
        if not (self._mpg123 or self._aplay):
            log.warning("neither mpg123 nor aplay found, sound cues disabled")

    def command(self, cue: str) -> list[str] | None:
        """The player command for a cue: mp3 through mpg123 if both exist,
        else wav through aplay, else None (stay silent)."""
        mp3 = self._dir / f"{cue}.mp3"
        wav = self._dir / f"{cue}.wav"
        if self._mpg123 and mp3.exists():
            return ["mpg123", "-q", str(mp3)]
        if self._aplay and wav.exists():
            return ["aplay", "-q", str(wav)]
        return None

    def play(self, cue: str) -> None:
        cmd = self.command(cue)
        if cmd is None:
            return
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def missing(self) -> list[str]:
        """Cues that have neither an mp3 nor a wav file."""
        return [
            cue for cue in CUES
            if not (self._dir / f"{cue}.mp3").exists() and not (self._dir / f"{cue}.wav").exists()
        ]

    def ensure_cues(self) -> list[Path]:
        """Generate wav files for cues that have no sound file at all."""
        from .tones import generate

        missing = self.missing()
        if not missing:
            return []
        log.info("No sound file for %s, generating wav cues in %s", ", ".join(missing), self._dir)
        try:
            return generate(self._dir, missing)
        except OSError as e:
            log.warning("Could not generate sound cues: %s", e)
            return []
