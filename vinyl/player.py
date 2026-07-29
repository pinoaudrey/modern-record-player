"""The scan loop: card -> action. Unknown cards become pending registrations."""

import logging
import threading
import time
from dataclasses import dataclass

from .db import Database
from .reader import Reader
from .sounds import Sounds
from .spotify import SpotifyClient

log = logging.getLogger(__name__)


@dataclass
class PendingScan:
    uid: str
    seen_at: float


class Player:
    def __init__(
        self,
        db: Database,
        spotify: SpotifyClient,
        reader: Reader,
        sounds: Sounds,
        scan_cooldown: float = 2.0,
    ):
        self._db = db
        self._spotify = spotify
        self._reader = reader
        self._sounds = sounds
        self._cooldown = scan_cooldown
        self._last_uid: str | None = None
        self._last_scan_at = 0.0
        self._pending_lock = threading.Lock()
        self._pending: PendingScan | None = None
        self._stop = threading.Event()

    # --- pending registration handoff to the web admin ----------------------

    @property
    def pending_scan(self) -> PendingScan | None:
        with self._pending_lock:
            return self._pending

    def clear_pending(self, uid: str | None = None) -> None:
        with self._pending_lock:
            if uid is None or (self._pending and self._pending.uid == uid):
                self._pending = None

    # --- loop ----------------------------------------------------------------

    def run_forever(self) -> None:
        self._sounds.play("startup")
        while not self._stop.is_set():
            try:
                uid = self._reader.read()
            except Exception:
                log.exception("Reader error, retrying")
                time.sleep(1)
                continue
            try:
                self.handle_scan(uid)
            except Exception:
                log.exception("Error handling scan of %s", uid)
                self._sounds.play("error")

    def stop(self) -> None:
        self._stop.set()

    def handle_scan(self, uid: str) -> None:
        uid = str(uid)
        now = time.monotonic()
        if uid == self._last_uid and now - self._last_scan_at < self._cooldown:
            return
        self._last_uid, self._last_scan_at = uid, now

        card = self._db.get_card(uid)
        if card is None:
            log.info("Unknown card %s, flagging for registration", uid)
            with self._pending_lock:
                self._pending = PendingScan(uid=uid, seen_at=time.time())
            self._sounds.play("error")
            return

        if card.kind == "control":
            log.info("Control card: %s", card.action)
            self._sounds.play("accept")
            self._handle_control(card.action)
            return

        log.info("Playing %s: %s", card.content_type, card.name)
        self._sounds.play("accept")
        self._spotify.play(card.uri)
        self._db.record_play(uid)

    def _handle_control(self, action: str) -> None:
        if action == "play_pause":
            self._spotify.play_pause()
        elif action == "next":
            self._spotify.next_track()
        elif action == "prev":
            self._spotify.prev_track()
        elif action == "shuffle":
            state = self._spotify.toggle_shuffle()
            self._sounds.play("shuffle_on" if state else "shuffle_off")
        elif action == "switch_device":
            name = self._spotify.switch_device()
            if name is None:
                self._sounds.play("connect_device")
            else:
                log.info("Now playing on %s", name)
