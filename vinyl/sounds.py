"""Audio feedback cues, played through mpg123 without blocking the scan loop."""

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

CUES = ("startup", "accept", "error", "connect_device", "shuffle_on", "shuffle_off")


class Sounds:
    def __init__(self, sounds_dir: Path):
        self._dir = sounds_dir
        self._enabled = shutil.which("mpg123") is not None
        if not self._enabled:
            log.warning("mpg123 not found, sound cues disabled")

    def play(self, cue: str) -> None:
        path = self._dir / f"{cue}.mp3"
        if not self._enabled or not path.exists():
            return
        subprocess.Popen(
            ["mpg123", "-q", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
