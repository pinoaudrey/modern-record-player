"""Spotify Web API wrapper: PKCE auth, ref resolution, playback control."""

import logging
from dataclasses import dataclass

import spotipy
from spotipy.oauth2 import SpotifyPKCE

from .config import Config
from .links import SpotifyRef, is_editorial_playlist, parse_ref

log = logging.getLogger(__name__)

SCOPES = ",".join([
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-library-read",
    "user-read-recently-played",   # play history poller
    "user-top-read",               # your top artists/tracks
])


class NotAuthorized(RuntimeError):
    pass


MAX_PLAY_URIS = 200      # start_playback accepts a list of track uris; keep it sane
PAGE = 50                # album_tracks page size (playlist_items allows 100)


@dataclass(frozen=True)
class ResolvedContent:
    uri: str
    content_type: str
    name: str
    artist: str | None
    artwork_url: str | None
    note: str | None = None  # something the admin should tell the user about this pick


@dataclass(frozen=True)
class RecentPlay:
    played_at: str
    track_uri: str
    track_name: str
    artist: str
    album_uri: str
    album_name: str
    artwork_url: str | None
    context_uri: str | None


EDITORIAL_NOTE = (
    "You're playing from a Spotify-curated playlist, which Spotify's API won't describe "
    "to this app, so the card is the current album instead."
)


@dataclass(frozen=True)
class CurrentTrack:
    """The bare playback state the scan loop needs: what is on, is it moving,
    where is it, and what it's playing from."""
    track_uri: str
    is_playing: bool
    position_ms: int
    context_uri: str | None


@dataclass(frozen=True)
class NowPlaying:
    track_name: str
    artist: str
    album_name: str
    album_uri: str
    artwork_url: str | None
    context_uri: str | None
    is_playing: bool
    device_name: str | None


def make_auth_manager(cfg: Config, open_browser: bool) -> SpotifyPKCE:
    return SpotifyPKCE(
        client_id=cfg.client_id,
        redirect_uri=cfg.redirect_uri,
        scope=SCOPES,
        cache_path=str(cfg.root / ".spotify_token_cache"),
        open_browser=open_browser,
    )


class SpotifyClient:
    def __init__(self, cfg: Config, open_browser: bool = False):
        self._cfg = cfg
        self._auth = make_auth_manager(cfg, open_browser)
        self._sp = spotipy.Spotify(auth_manager=self._auth)
        self._device_id: str | None = None

    @property
    def authorized(self) -> bool:
        """True if a usable token is cached. Refreshes it if expired."""
        try:
            return self._auth.validate_token(self._auth.cache_handler.get_cached_token()) is not None
        except Exception as e:  # network error during refresh, corrupt cache
            log.warning("Token check failed: %s", e)
            return False

    # --- authorization (PKCE, copy-paste flow that works on a headless Pi) ---

    def authorize_url(self) -> str:
        """Start a PKCE login. The same client instance must complete it."""
        return self._auth.get_authorize_url()

    def complete_authorization(self, redirect_url: str) -> None:
        """Finish the login with the URL the browser landed on (or the bare code)."""
        code = self._auth.parse_response_code(redirect_url.strip())
        if not code or code.startswith("http"):
            raise ValueError("No authorization code found in what you pasted.")
        self._auth.get_access_token(code=code, check_cache=False)
        self._device_id = None

    @property
    def sp(self) -> spotipy.Spotify:
        # Without this, spotipy would fall back to an interactive input() prompt
        # inside the scan loop or a web request and hang there forever.
        if not self.authorized:
            raise NotAuthorized("Spotify isn't connected yet. Open the admin's Connect Spotify page (/auth).")
        return self._sp

    def resolve(self, ref: SpotifyRef) -> ResolvedContent:
        if ref.type == "track":
            t = self.sp.track(ref.id)
            return ResolvedContent(
                uri=ref.uri,
                content_type="track",
                name=t["name"],
                artist=", ".join(a["name"] for a in t["artists"]),
                artwork_url=_first_image(t["album"]),
            )
        if ref.type == "album":
            a = self.sp.album(ref.id)
            return ResolvedContent(
                uri=ref.uri,
                content_type="album",
                name=a["name"],
                artist=", ".join(x["name"] for x in a["artists"]),
                artwork_url=_first_image(a),
            )
        if ref.type == "playlist":
            p = self.sp.playlist(ref.id, fields="name,images,owner.display_name")
            return ResolvedContent(
                uri=ref.uri,
                content_type="playlist",
                name=p["name"],
                artist=(p.get("owner") or {}).get("display_name"),
                artwork_url=_first_image(p),
            )
        if ref.type == "artist":
            a = self.sp.artist(ref.id)
            return ResolvedContent(
                uri=ref.uri,
                content_type="artist",
                name=a["name"],
                artist=None,
                artwork_url=_first_image(a),
            )
        raise ValueError(f"unsupported ref type: {ref.type}")

    def search(self, query: str, content_type: str = "album") -> list[ResolvedContent]:
        # Search limit is capped at 10 by the API as of Feb 2026
        results = self.sp.search(q=query, type=content_type, limit=10)
        items = results.get(f"{content_type}s", {}).get("items", [])
        out = []
        for item in items:
            if item is None:
                continue
            artist = None
            artwork = _first_image(item)
            if content_type == "track":
                artist = ", ".join(a["name"] for a in item["artists"])
                artwork = _first_image(item["album"])
            elif content_type == "album":
                artist = ", ".join(a["name"] for a in item["artists"])
            elif content_type == "playlist":
                artist = (item.get("owner") or {}).get("display_name")
            out.append(
                ResolvedContent(
                    uri=item["uri"],
                    content_type=content_type,
                    name=item["name"],
                    artist=artist,
                    artwork_url=artwork,
                )
            )
        return out

    # --- now playing --------------------------------------------------------

    def now_playing(self) -> NowPlaying | None:
        state = self.sp.current_playback()
        item = (state or {}).get("item")
        if not item or item.get("type") != "track":  # nothing, or a podcast episode
            return None
        album = item["album"]
        return NowPlaying(
            track_name=item["name"],
            artist=", ".join(a["name"] for a in item["artists"]),
            album_name=album["name"],
            album_uri=album["uri"],
            artwork_url=_first_image(album),
            context_uri=(state.get("context") or {}).get("uri"),
            is_playing=bool(state.get("is_playing")),
            device_name=(state.get("device") or {}).get("name"),
        )

    def now_playing_content(self) -> ResolvedContent | None:
        """What a card for "this" should hold: the playlist/album/artist being
        played from, or the current track's album when there's no usable
        context (Liked Songs, a queue, a radio)."""
        np = self.now_playing()
        if np is None:
            return None
        album = ResolvedContent(
            uri=np.album_uri,
            content_type="album",
            name=np.album_name,
            artist=np.artist,
            artwork_url=np.artwork_url,
        )
        ref = parse_ref(np.context_uri) if np.context_uri else None
        if ref is None or ref.type == "track" or ref.uri == np.album_uri:
            return album
        if is_editorial_playlist(ref):
            return ResolvedContent(**{**album.__dict__, "note": EDITORIAL_NOTE})
        try:
            return self.resolve(ref)
        except Exception as e:
            log.warning("Could not resolve playback context %s: %s", ref.uri, e)
            return album

    # --- listening history --------------------------------------------------

    def recently_played(self, limit: int = 50) -> list[RecentPlay]:
        """The last plays Spotify still remembers (max 50, tracks played >30s)."""
        result = self.sp.current_user_recently_played(limit=limit)
        out = []
        for item in result.get("items", []):
            t = item.get("track") or {}
            if t.get("type") != "track" or not t.get("album"):
                continue
            out.append(RecentPlay(
                played_at=item["played_at"],
                track_uri=t["uri"],
                track_name=t["name"],
                artist=", ".join(a["name"] for a in t["artists"]),
                album_uri=t["album"]["uri"],
                album_name=t["album"]["name"],
                artwork_url=_first_image(t["album"]),
                context_uri=(item.get("context") or {}).get("uri"),
            ))
        return out

    def top_artists(self, time_range: str = "medium_term", limit: int = 10) -> list[ResolvedContent]:
        result = self.sp.current_user_top_artists(limit=limit, time_range=time_range)
        return [
            ResolvedContent(uri=a["uri"], content_type="artist", name=a["name"],
                            artist=None, artwork_url=_first_image(a))
            for a in result.get("items", [])
        ]

    def top_albums(self, time_range: str = "medium_term", limit: int = 10) -> list[tuple[ResolvedContent, int]]:
        """Spotify ranks tracks, not albums; fold the top 50 tracks into their
        albums and return (album, number of top tracks on it), most first."""
        result = self.sp.current_user_top_tracks(limit=50, time_range=time_range)
        counts: dict[str, list] = {}
        for t in result.get("items", []):
            album = t.get("album")
            if not album:
                continue
            entry = counts.setdefault(album["uri"], [
                ResolvedContent(
                    uri=album["uri"], content_type="album", name=album["name"],
                    artist=", ".join(a["name"] for a in album["artists"]),
                    artwork_url=_first_image(album),
                ), 0,
            ])
            entry[1] += 1
        ranked = sorted(counts.values(), key=lambda e: -e[1])
        return [(c, n) for c, n in ranked[:limit]]

    # --- playback -----------------------------------------------------------

    def device_id(self, refresh: bool = False) -> str | None:
        if self._device_id is None or refresh:
            devices = self.sp.devices().get("devices", [])
            wanted = self._cfg.device_name.lower()
            for d in devices:
                if d["name"].lower() == wanted:
                    self._device_id = d["id"]
                    break
            else:
                self._device_id = devices[0]["id"] if devices else None
                if self._device_id:
                    log.warning(
                        "Device %r not found, falling back to %s",
                        self._cfg.device_name,
                        devices[0]["name"],
                    )
        return self._device_id

    def play(
        self, uri: str, position_ms: int | None = None, track_uri: str | None = None,
        uris: list[str] | None = None,
    ) -> None:
        """Start `uri` on the player's device. With `position_ms` (and, for
        albums/playlists, the `track_uri` to start at) playback picks up
        where a card left off. Artist contexts can't take an offset.

        With `uris` (a pressing: the card's frozen track list) those tracks
        are played instead of the live context, at most MAX_PLAY_URIS of
        them; `track_uri`/`position_ms` still pick the starting point."""
        device = self.device_id()
        if device is None:
            raise RuntimeError("No Spotify Connect devices available")
        kwargs: dict = {}
        if position_ms:
            kwargs["position_ms"] = int(position_ms)
        if uris:
            uris = list(uris)[:MAX_PLAY_URIS]
            if track_uri and track_uri in uris:
                kwargs["offset"] = {"uri": track_uri}
            elif "position_ms" in kwargs:
                del kwargs["position_ms"]
            self.sp.start_playback(device_id=device, uris=uris, **kwargs)
            return
        if uri.startswith("spotify:track:"):
            self.sp.start_playback(device_id=device, uris=[uri], **kwargs)
            return
        if track_uri and not uri.startswith("spotify:artist:"):
            kwargs["offset"] = {"uri": track_uri}
        elif "position_ms" in kwargs and not track_uri:
            del kwargs["position_ms"]  # a position without a track is meaningless
        self.sp.start_playback(device_id=device, context_uri=uri, **kwargs)

    def queue(self, uri: str) -> None:
        """Add one track to the end of the queue on the player's device."""
        device = self.device_id()
        if device is None:
            raise RuntimeError("No Spotify Connect devices available")
        self.sp.add_to_queue(uri, device_id=device)

    def content_tracks(self, uri: str, limit: int = 50) -> list[str]:
        """The track uris a card holds, in order: the track itself, an album's
        tracks, or a playlist's current items, at most `limit` of them.
        Artists have no fixed track list. Local files and episodes are skipped."""
        ref = parse_ref(uri)
        if ref is None:
            raise ValueError(f"not a Spotify uri: {uri}")
        if ref.type == "track":
            return [ref.uri]
        if ref.type == "artist":
            raise ValueError("artist cards have no track list")
        out: list[str] = []
        offset = 0
        while len(out) < limit:
            want = min(limit - len(out), PAGE if ref.type == "album" else 100)
            if ref.type == "album":
                page = self.sp.album_tracks(ref.id, limit=want, offset=offset)
                items = [t for t in page.get("items", [])]
            else:
                page = self.sp.playlist_items(
                    ref.id, fields="items(track(uri,type)),next", limit=want, offset=offset,
                )
                items = [(it or {}).get("track") for it in page.get("items", [])]
            for t in items:
                u = (t or {}).get("uri")
                if u and u.startswith("spotify:track:"):
                    out.append(u)
            got = len(page.get("items", []))
            offset += got
            if got == 0 or not page.get("next"):
                break
        return out[:limit]

    def current_track(self) -> CurrentTrack | None:
        """What's on right now, or None when nothing is loaded on any device."""
        state = self.sp.current_playback()
        item = (state or {}).get("item")
        if not item or not item.get("uri"):
            return None
        return CurrentTrack(
            track_uri=item["uri"],
            is_playing=bool(state.get("is_playing")),
            position_ms=int(state.get("progress_ms") or 0),
            context_uri=(state.get("context") or {}).get("uri"),
        )

    def is_playing(self) -> bool:
        state = self.sp.current_playback()
        return bool(state and state.get("is_playing"))

    def pause(self) -> None:
        self.sp.pause_playback(device_id=self.device_id())

    def resume(self) -> None:
        """Continue whatever is paused on the player's device."""
        self.sp.start_playback(device_id=self.device_id())

    def set_shuffle(self, state: bool) -> None:
        self.sp.shuffle(bool(state), device_id=self.device_id())

    def play_pause(self) -> None:
        state = self.sp.current_playback()
        if state and state.get("is_playing"):
            self.sp.pause_playback(device_id=self.device_id())
        else:
            self.sp.start_playback(device_id=self.device_id())

    def next_track(self) -> None:
        self.sp.next_track(device_id=self.device_id())

    def prev_track(self) -> None:
        self.sp.previous_track(device_id=self.device_id())

    def toggle_shuffle(self) -> bool:
        state = self.sp.current_playback()
        new_state = not (state and state.get("shuffle_state"))
        self.sp.shuffle(new_state, device_id=self.device_id())
        return new_state

    def switch_device(self) -> str | None:
        devices = self.sp.devices().get("devices", [])
        if not devices:
            return None
        ids = [d["id"] for d in devices]
        current = self.device_id()
        idx = (ids.index(current) + 1) % len(ids) if current in ids else 0
        self._device_id = ids[idx]
        self.sp.transfer_playback(device_id=self._device_id, force_play=True)
        return devices[idx]["name"]

    def list_devices(self) -> list[dict]:
        return self.sp.devices().get("devices", [])

    def me(self) -> dict:
        """The connected account's profile (display_name, id, ...), for the status page."""
        return self.sp.me() or {}


def _first_image(obj: dict) -> str | None:
    images = obj.get("images") or []
    return images[0]["url"] if images else None
