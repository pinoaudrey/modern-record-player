"""Listening history.

Spotify only remembers your last 50 plays, so the player polls that endpoint
every few minutes and keeps everything. The report turns that into "make these
records": what you play a lot but haven't got a card for.
"""

import logging
import threading
import time
from dataclasses import dataclass

from .db import Database, Play, PlayCount, since_days
from .links import is_editorial_playlist, parse_ref
from .spotify import ResolvedContent, SpotifyClient

log = logging.getLogger(__name__)

LAST_POLL_KEY = "history_last_poll"

# window key -> (label, days back or None for all, Spotify top time_range)
WINDOWS = {
    "7d": ("Last 7 days", 7, "short_term"),
    "30d": ("Last 30 days", 30, "medium_term"),
    "all": ("All time", None, "long_term"),
}
DEFAULT_WINDOW = "30d"


class HistoryPoller:
    def __init__(self, db: Database, spotify: SpotifyClient, interval: float = 300.0):
        self._db = db
        self._spotify = spotify
        self._interval = interval
        self._stop = threading.Event()
        self.last_error: str | None = None
        self.last_new = 0

    @property
    def last_poll_at(self) -> str | None:
        return self._db.get_meta(LAST_POLL_KEY)

    def poll_once(self) -> int:
        """Fetch recent plays and store the ones we haven't seen. Returns how many were new."""
        recent = self._spotify.recently_played()
        plays = [Play(**r.__dict__) for r in recent]
        new = self._db.add_plays(plays)
        self._db.set_meta(LAST_POLL_KEY, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self.last_new = new
        self.last_error = None
        if new:
            log.info("History: %d new plays", new)
        return new

    def run_forever(self) -> None:
        while not self._stop.is_set():
            if getattr(self._spotify, "authorized", True):
                try:
                    self.poll_once()
                except Exception as e:
                    self.last_error = str(e)
                    log.warning("History poll failed: %s", e)
            self._stop.wait(self._interval)

    def stop(self) -> None:
        self._stop.set()


@dataclass(frozen=True)
class Suggestion:
    content: ResolvedContent
    plays: int                  # plays in the window, or top-track count for Spotify's picks
    last_played_at: str | None
    has_card: bool


@dataclass
class Report:
    window: str
    label: str
    plays_in_window: int
    albums: list[Suggestion]        # from local history
    contexts: list[Suggestion]      # playlists / artists played from, local history
    top_albums: list[Suggestion]    # Spotify's own ranking
    top_artists: list[Suggestion]
    spotify_error: str | None = None


def describe_context(db: Database, spotify: SpotifyClient, pc: PlayCount) -> ResolvedContent:
    """Name and artwork for a playlist/artist uri seen in history, looked up
    once and cached in the library table. Editorial playlists can't be looked
    up, so they get a generic name."""
    if pc.name:
        return ResolvedContent(pc.uri, pc.content_type, pc.name, pc.artist, pc.artwork_url)
    item = db.library_get(pc.uri)
    if item is None:
        ref = parse_ref(pc.uri)
        if ref is None or is_editorial_playlist(ref):
            db.library_put(pc.uri, pc.content_type, None, None, None, available=False)
        else:
            try:
                r = spotify.resolve(ref)
                db.library_put(r.uri, r.content_type, r.name, r.artist, r.artwork_url)
            except Exception as e:
                log.warning("Could not look up %s: %s", pc.uri, e)
                db.library_put(pc.uri, pc.content_type, None, None, None, available=False)
        item = db.library_get(pc.uri)
    if item.name:
        return ResolvedContent(item.uri, item.content_type, item.name, item.artist, item.artwork_url)
    ref = parse_ref(pc.uri)
    if ref is not None and is_editorial_playlist(ref):
        return ResolvedContent(
            pc.uri, "playlist", "Spotify curated playlist", "Spotify", None,
            note="Spotify won't share this playlist's name with the app, but a card of it still plays.",
        )
    return ResolvedContent(pc.uri, pc.content_type, f"{pc.content_type} {pc.uri.split(':')[-1]}", None, None)


def build_report(db: Database, spotify: SpotifyClient, window: str = DEFAULT_WINDOW) -> Report:
    if window not in WINDOWS:
        window = DEFAULT_WINDOW
    label, days, time_range = WINDOWS[window]
    since = since_days(days) if days else None
    have = db.card_uris()

    albums = [
        Suggestion(ResolvedContent(a.uri, "album", a.name, a.artist, a.artwork_url),
                   a.plays, a.last_played_at, a.uri in have)
        for a in db.top_albums(since)
    ]
    contexts = [
        Suggestion(describe_context(db, spotify, c), c.plays, c.last_played_at, c.uri in have)
        for c in db.top_contexts(since)
    ]

    top_albums: list[Suggestion] = []
    top_artists: list[Suggestion] = []
    spotify_error = None
    try:
        top_albums = [Suggestion(c, n, None, c.uri in have) for c, n in spotify.top_albums(time_range)]
        top_artists = [Suggestion(c, 0, None, c.uri in have) for c in spotify.top_artists(time_range)]
    except Exception as e:
        spotify_error = str(e)
        log.warning("Spotify top lookup failed: %s", e)

    return Report(
        window=window, label=label, plays_in_window=db.play_count(since),
        albums=albums, contexts=contexts, top_albums=top_albums, top_artists=top_artists,
        spotify_error=spotify_error,
    )
