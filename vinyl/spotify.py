"""Spotify Web API wrapper: PKCE auth, ref resolution, playback control."""

import logging
from dataclasses import dataclass

import spotipy
from spotipy.oauth2 import SpotifyPKCE

from .config import Config
from .links import SpotifyRef, parse_ref

log = logging.getLogger(__name__)

SCOPES = "user-read-playback-state,user-modify-playback-state,user-library-read"


class NotAuthorized(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedContent:
    uri: str
    content_type: str
    name: str
    artist: str | None
    artwork_url: str | None


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
            raise NotAuthorized("Spotify isn't authorized yet. Run: python -m vinyl auth")
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
        try:
            return self.resolve(ref)
        except Exception as e:
            log.warning("Could not resolve playback context %s: %s", ref.uri, e)
            return album

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

    def play(self, uri: str) -> None:
        device = self.device_id()
        if device is None:
            raise RuntimeError("No Spotify Connect devices available")
        if uri.startswith("spotify:track:"):
            self.sp.start_playback(device_id=device, uris=[uri])
        else:
            self.sp.start_playback(device_id=device, context_uri=uri)

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


def _first_image(obj: dict) -> str | None:
    images = obj.get("images") or []
    return images[0]["url"] if images else None
