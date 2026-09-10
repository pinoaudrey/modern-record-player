"""The scan loop: card -> action.

Unknown cards become pending registrations, unless the tag itself carries a
Spotify URI, in which case the card registers itself and plays. When a write
is armed (from the admin or the CLI), the next card tapped gets the URI
written into its tag memory and is registered in the same step.

The loop also behaves a little like a turntable. The card that started
playback is "on the platter": while it rests on the reader it stays quiet,
lifting it pauses Spotify, and putting it back within the resume window
continues where it left off. Cards can also ask to stop after one play
(single) or to remember where they were interrupted (resume).
"""

import logging
import threading
import time
from dataclasses import dataclass

from .db import Card, CardOptions, Database
from .links import SpotifyRef, parse_ref
from .reader import Reader, Scan
from .sounds import Sounds
from .spotify import NotAuthorized, ResolvedContent, SpotifyClient

log = logging.getLogger(__name__)

SINGLE_CHECK_INTERVAL = 5.0   # seconds between current_playback() calls while watching a single
SINGLE_START_GRACE = 30.0     # give Spotify this long to report the single as playing


@dataclass
class PendingScan:
    uid: str
    seen_at: float


@dataclass
class WriteRequest:
    content: ResolvedContent
    armed_at: float
    options: CardOptions | None = None


@dataclass
class WriteResult:
    uid: str | None
    content: ResolvedContent
    ok: bool
    error: str | None
    at: float


@dataclass
class CurrentCard:
    """The card whose scan started what is playing now."""
    uid: str
    card: Card
    seen_at: float           # monotonic time the reader last reported it
    lifted: bool = False     # not seen for longer than lift_timeout
    paused_by_lift: bool = False
    paused_at: float = 0.0   # monotonic time we paused it


@dataclass
class SingleWatch:
    """Watching a single-mode card so playback stops when it ends."""
    target_uri: str          # the track (or the album/playlist context) to stay on
    is_track: bool
    started_at: float
    next_check: float
    seen: bool = False       # Spotify has confirmed it's playing our target


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
        lift_to_pause: bool = True,
        lift_timeout: float = 1.5,
        resume_window: float = 900.0,
    ):
        self._db = db
        self._spotify = spotify
        self._reader = reader
        self._sounds = sounds
        self._cooldown = scan_cooldown
        self._poll_timeout = poll_timeout
        self._write_timeout = write_timeout
        self._lift_to_pause = lift_to_pause
        self._lift_timeout = lift_timeout
        self._resume_window = resume_window
        self._last_uid: str | None = None
        self._last_scan_at = 0.0
        self._lock = threading.Lock()
        self._pending: PendingScan | None = None
        self._write_request: WriteRequest | None = None
        self._last_write: WriteResult | None = None
        self._generation = 0  # bumped whenever the scan loop changes the card database
        self._stop = threading.Event()
        self._current: CurrentCard | None = None
        self._single: SingleWatch | None = None

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

    def arm_write(self, content: ResolvedContent, options: CardOptions | None = None) -> None:
        """The next card tapped gets `content.uri` written to it and is registered."""
        with self._lock:
            self._write_request = WriteRequest(content=content, armed_at=time.time(), options=options)
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

    @property
    def current_uid(self) -> str | None:
        """UID of the card that started what's playing, if any."""
        current = self._current
        return current.uid if current else None

    @property
    def lifted(self) -> bool:
        """True while the current card is off the reader."""
        current = self._current
        return bool(current and current.lifted)

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
            if scan is not None:
                try:
                    self.handle_scan(scan)
                except NotAuthorized as e:
                    log.warning("Card %s: %s", scan.uid, e)
                    self._sounds.play("error")
                except Exception:
                    log.exception("Error handling scan of %s", scan.uid)
                    self._sounds.play("error")
            try:
                self.tick()
            except Exception:
                log.exception("Error in playback check")

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> None:
        """Time-based checks, run after every poll whether or not a card was
        seen: has the playing card been lifted, has a single finished."""
        now = time.monotonic()
        self._check_lift(now)
        self._check_single(now)

    def handle_scan(self, scan: Scan | str) -> None:
        if isinstance(scan, str):
            scan = Scan(uid=scan)
        uid = scan.uid
        now = time.monotonic()

        current = self._current
        if current is not None and current.uid == uid:
            current.seen_at = now

        request = self.pending_write
        if request is not None:
            self._write_card(request)
            # Don't let the freshly written card, still on the reader, start playing.
            self._last_uid, self._last_scan_at = uid, now
            return

        from_top = False
        resting = uid == self._last_uid and now - self._last_scan_at < self._cooldown
        if current is not None and current.uid == uid and current.lifted:
            # The playing card is back after a lift. If we paused it, this is
            # not a resting repeat: resume (soon enough) or start over (much
            # later). If we didn't (Spotify was already paused, or the feature
            # is off), the plain cooldown rule decides, as it always has.
            current.lifted = False
            if current.paused_by_lift:
                current.paused_by_lift = False
                if now - current.paused_at <= self._resume_window:
                    self._resume(current, now)
                    return
                from_top = True
            elif resting:
                self._last_scan_at = now
                return
        elif resting:
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

        self._sounds.play("accept")
        self.play_card(card, from_top=from_top, on_reader=True)

    # --- playing ------------------------------------------------------------

    def play_card(self, card: Card, from_top: bool = False, on_reader: bool = False) -> None:
        """Start a content card, honouring its options. `on_reader` means a
        scan started it (so lifting it later can pause); the admin's Play
        button passes False. `from_top` ignores and clears a saved position."""
        now = time.monotonic()
        previous = self._current
        if previous is not None and previous.uid != card.uid:
            # Another card takes over: remember where this one was.
            self._save_position(previous)
        self._current = None
        self._single = None

        opts = card.options
        if opts.shuffle is not None:
            try:
                self._spotify.set_shuffle(opts.shuffle)
            except NotAuthorized:
                raise
            except Exception as e:
                log.warning("Could not set shuffle %s: %s", opts.shuffle, e)

        position = None
        if from_top:
            self._db.clear_position(card.uid)
        elif opts.resume:
            position = self._db.get_position(card.uid)

        log.info("Playing %s: %s%s", card.content_type, card.name,
                 f" (resuming at {position.position_ms // 1000}s)" if position else "")
        if position is not None:
            try:
                self._spotify.play(card.uri, position_ms=position.position_ms,
                                   track_uri=position.track_uri)
            except NotAuthorized:
                raise
            except Exception as e:
                # The track may have left the playlist, or the API refused the
                # offset: fall back to the top rather than play nothing.
                log.warning("Could not resume %s at its saved position (%s), starting over", card.name, e)
                self._spotify.play(card.uri)
            self._db.clear_position(card.uid)  # consumed; the next interruption saves a new one
        else:
            self._spotify.play(card.uri)
        self._db.record_play(card.uid)

        if on_reader:
            self._current = CurrentCard(uid=card.uid, card=card, seen_at=now)
        if opts.single:
            self._single = SingleWatch(
                target_uri=card.uri, is_track=card.content_type == "track",
                started_at=now, next_check=now + SINGLE_CHECK_INTERVAL,
            )

    def _resume(self, current: CurrentCard, now: float) -> None:
        log.info("Card %s is back, resuming %s", current.uid, current.card.name)
        self._sounds.play("accept")
        self._spotify.resume()
        self._last_uid, self._last_scan_at = current.uid, now
        if self._single is not None:
            self._single.next_check = now + SINGLE_CHECK_INTERVAL

    def _check_lift(self, now: float) -> None:
        current = self._current
        if current is None or current.lifted:
            return
        if now - current.seen_at <= self._lift_timeout:
            return
        current.lifted = True
        if not self._lift_to_pause:
            return
        log.info("Card %s lifted", current.uid)
        self._save_position(current)
        try:
            playing = self._spotify.is_playing()
        except Exception as e:
            log.warning("Could not check playback after lift: %s", e)
            return
        if not playing:
            return  # paused from the phone already; don't fight it
        try:
            self._spotify.pause()
        except Exception as e:
            log.warning("Could not pause after lift: %s", e)
            return
        current.paused_by_lift = True
        current.paused_at = now

    def _save_position(self, current: CurrentCard) -> None:
        """For a resume card, note where playback is, if it's still on this card."""
        card = current.card
        if not card.options.resume:
            return
        try:
            state = self._spotify.current_track()
        except Exception as e:
            log.warning("Could not read playback position for %s: %s", card.name, e)
            return
        if state is None:
            return
        if card.content_type == "track":
            on_card = state.track_uri == card.uri
        else:
            on_card = state.context_uri == card.uri
        if not on_card:
            return  # the phone has moved on to something else; keep what we had
        self._db.save_position(current.uid, state.track_uri, state.position_ms)
        log.info("Saved position for %s: %s at %dms", card.name, state.track_uri, state.position_ms)

    def _check_single(self, now: float) -> None:
        watch = self._single
        if watch is None:
            return
        current = self._current
        if current is not None and current.paused_by_lift:
            return  # we paused it ourselves; pick the watch back up on resume
        if now < watch.next_check:
            return
        watch.next_check = now + SINGLE_CHECK_INTERVAL
        try:
            state = self._spotify.current_track()
        except Exception as e:
            log.warning("Could not check playback for single mode: %s", e)
            return
        on_target = state is not None and (
            state.track_uri == watch.target_uri if watch.is_track
            else state.context_uri == watch.target_uri
        )
        if on_target and state.is_playing:
            watch.seen = True
            return
        if not watch.seen and now - watch.started_at < SINGLE_START_GRACE:
            return  # Spotify hasn't caught up with the start yet
        self._single = None
        if state is not None and state.is_playing:
            log.info("Single finished, stopping playback")
            try:
                self._spotify.pause()
            except Exception as e:
                log.warning("Could not stop after single: %s", e)

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
                options=request.options,
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
