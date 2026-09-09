"""The scan loop: card -> action.

Unknown cards become pending registrations, unless the tag itself carries a
Spotify URI, in which case the card registers itself and plays. When a write
is armed (from the admin or the CLI), the next card tapped gets the URI
written into its tag memory and is registered in the same step.
"""

import logging
import threading
import time
from dataclasses import dataclass

from .db import Card, Database
from .links import SpotifyRef, parse_ref
from .reader import Reader, Scan
from .sounds import Sounds
from .spotify import NotAuthorized, ResolvedContent, SpotifyClient

log = logging.getLogger(__name__)


@dataclass
class PendingScan:
    uid: str
    seen_at: float


@dataclass
class WriteRequest:
    content: ResolvedContent
    armed_at: float


@dataclass
class WriteResult:
    uid: str | None
    content: ResolvedContent
    ok: bool
    error: str | None
    at: float


class Player:
    def __init__(
        self,
        db: Database,
        spotify: SpotifyClient,
        reader: Reader,
        sounds: Sounds,
        scan_cooldown: float = 2.0,
        poll_timeout: float = 0.5,
        write_timeout: float = 3.0,
    ):
        self._db = db
        self._spotify = spotify
        self._reader = reader
        self._sounds = sounds
        self._cooldown = scan_cooldown
        self._poll_timeout = poll_timeout
        self._write_timeout = write_timeout
        self._last_uid: str | None = None
        self._last_scan_at = 0.0
        self._lock = threading.Lock()
        self._pending: PendingScan | None = None
        self._write_request: WriteRequest | None = None
        self._last_write: WriteResult | None = None
        self._generation = 0  # bumped whenever the scan loop changes the card database
        self._stop = threading.Event()

    # --- state shared with the web admin ------------------------------------

    @property
    def pending_scan(self) -> PendingScan | None:
        with self._lock:
            return self._pending

    def clear_pending(self, uid: str | None = None) -> None:
        with self._lock:
            if uid is None or (self._pending and self._pending.uid == uid):
                self._pending = None

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def _bump(self) -> None:
        with self._lock:
            self._generation += 1

    @property
    def pending_write(self) -> WriteRequest | None:
        with self._lock:
            return self._write_request

    def arm_write(self, content: ResolvedContent) -> None:
        """The next card tapped gets `content.uri` written to it and is registered."""
        with self._lock:
            self._write_request = WriteRequest(content=content, armed_at=time.time())
            self._last_write = None

    def cancel_write(self) -> None:
        with self._lock:
            self._write_request = None

    @property
    def last_write(self) -> WriteResult | None:
        with self._lock:
            return self._last_write

    def clear_last_write(self) -> None:
        with self._lock:
            self._last_write = None

    # --- loop ----------------------------------------------------------------

    def run_forever(self) -> None:
        self._sounds.play("startup")
        while not self._stop.is_set():
            try:
                scan = self._reader.poll(self._poll_timeout)
            except Exception:
                log.exception("Reader error, retrying")
                time.sleep(1)
                continue
            if scan is None:
                continue
            try:
                self.handle_scan(scan)
            except NotAuthorized as e:
                log.warning("Card %s: %s", scan.uid, e)
                self._sounds.play("error")
            except Exception:
                log.exception("Error handling scan of %s", scan.uid)
                self._sounds.play("error")

    def stop(self) -> None:
        self._stop.set()

    def handle_scan(self, scan: Scan | str) -> None:
        if isinstance(scan, str):
            scan = Scan(uid=scan)
        uid = scan.uid
        now = time.monotonic()

        request = self.pending_write
        if request is not None:
            self._write_card(request)
            # Don't let the freshly written card, still on the reader, start playing.
            self._last_uid, self._last_scan_at = uid, now
            return

        if uid == self._last_uid and now - self._last_scan_at < self._cooldown:
            # Same card is resting on the reader: keep it quiet until it's
            # lifted for a full cooldown, like a record left on the platter.
            self._last_scan_at = now
            return
        self._last_uid, self._last_scan_at = uid, now

        card = self._db.get_card(uid)
        if card is None:
            ref = parse_ref(scan.text) if scan.text else None
            if ref is None:
                log.info("Unknown card %s, flagging for registration", uid)
                with self._lock:
                    self._pending = PendingScan(uid=uid, seen_at=time.time())
                self._sounds.play("error")
                return
            card = self._register_from_tag(uid, ref)

        if card.kind == "control":
            log.info("Control card: %s", card.action)
            self._sounds.play("accept")
            self._handle_control(card.action)
            return

        log.info("Playing %s: %s", card.content_type, card.name)
        self._sounds.play("accept")
        self._spotify.play(card.uri)
        self._db.record_play(uid)

    # --- self-describing cards ----------------------------------------------

    def _register_from_tag(self, uid: str, ref: SpotifyRef) -> Card:
        log.info("Card %s carries %s, registering it from the tag", uid, ref.uri)
        try:
            content = self._spotify.resolve(ref)
        except Exception as e:
            # Offline or API hiccup: still register it so it plays; the name
            # can be fixed later from the admin.
            log.warning("Could not look up %s (%s), saving without metadata", ref.uri, e)
            content = ResolvedContent(
                uri=ref.uri, content_type=ref.type, name=f"{ref.type} {ref.id}",
                artist=None, artwork_url=None,
            )
        self._db.save_content_card(
            uid=uid, uri=content.uri, content_type=content.content_type, name=content.name,
            artist=content.artist, artwork_url=content.artwork_url, on_tag=True,
        )
        self.clear_pending(uid)
        self._bump()
        return self._db.get_card(uid)

    def _write_card(self, request: WriteRequest) -> None:
        content = request.content
        uid: str | None = None
        error: str | None = None
        try:
            written = self._reader.write(content.uri, timeout=self._write_timeout)
            if written is None:
                error = "No card was held on the reader long enough to write."
            else:
                uid = written.uid
        except Exception as e:
            error = str(e)

        if error is None:
            log.info("Wrote %s to card %s", content.uri, uid)
            self._db.save_content_card(
                uid=uid, uri=content.uri, content_type=content.content_type, name=content.name,
                artist=content.artist, artwork_url=content.artwork_url, on_tag=True,
            )
            self.clear_pending(uid)
            self._bump()
            self._sounds.play("written")
        else:
            log.warning("Write of %s failed: %s", content.uri, error)
            self._sounds.play("error")

        with self._lock:
            self._write_request = None
            self._last_write = WriteResult(
                uid=uid, content=content, ok=error is None, error=error, at=time.time()
            )

    # --- control cards ------------------------------------------------------

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
