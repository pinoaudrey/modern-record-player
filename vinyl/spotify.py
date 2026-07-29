"""Spotify Web API wrapper: PKCE auth, ref resolution, playback control."""

import logging
from dataclasses import dataclass

import spotipy
from spotipy.oauth2 import SpotifyPKCE

from .config import Config
from .links import SpotifyRef

log = logging.getLogger(__name__)

SCOPES = "user-read-playback-state,user-modify-playback-state,user-library-read"


@dataclass(frozen=True)
class ResolvedContent:
    uri: str
    content_type: str
    name: str
    artist: str | None
    artwork_url: str | None


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
        self._sp = spotipy.Spotify(auth_manager=make_auth_manager(cfg, open_browser))
        self._device_id: str | None = None

    def resolve(self, ref: SpotifyRef) -> ResolvedContent:
        if ref.type == "track":
            t = self._sp.track(ref.id)
            return ResolvedContent(
                uri=ref.uri,
                content_type="track",
                name=t["name"],
                artist=", ".join(a["name"] for a in t["artists"]),
                artwork_url=_first_image(t["album"]),
            )
        if ref.type == "album":
            a = self._sp.album(ref.id)
            return ResolvedContent(
                uri=ref.uri,
                content_type="album",
                name=a["name"],
                artist=", ".join(x["name"] for x in a["artists"]),
                artwork_url=_first_image(a),
            )
        if ref.type == "playlist":
            p = self._sp.playlist(ref.id, fields="name,images,owner.display_name")
            return ResolvedContent(
                uri=ref.uri,
                content_type="playlist",
                name=p["name"],
                artist=(p.get("owner") or {}).get("display_name"),
                artwork_url=_first_image(p),
            )
        if ref.type == "artist":
            a = self._sp.artist(ref.id)
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
        results = self._sp.search(q=query, type=content_type, limit=10)
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

    # --- playback -----------------------------------------------------------

    def device_id(self, refresh: bool = False) -> str | None:
        if self._device_id is None or refresh:
            devices = self._sp.devices().get("devices", [])
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
            self._sp.start_playback(device_id=device, uris=[uri])
        else:
            self._sp.start_playback(device_id=device, context_uri=uri)

    def play_pause(self) -> None:
        state = self._sp.current_playback()
        if state and state.get("is_playing"):
            self._sp.pause_playback(device_id=self.device_id())
        else:
            self._sp.start_playback(device_id=self.device_id())

    def next_track(self) -> None:
        self._sp.next_track(device_id=self.device_id())

    def prev_track(self) -> None:
        self._sp.previous_track(device_id=self.device_id())

    def toggle_shuffle(self) -> bool:
        state = self._sp.current_playback()
        new_state = not (state and state.get("shuffle_state"))
        self._sp.shuffle(new_state, device_id=self.device_id())
        return new_state

    def switch_device(self) -> str | None:
        devices = self._sp.devices().get("devices", [])
        if not devices:
            return None
        ids = [d["id"] for d in devices]
        current = self.device_id()
        idx = (ids.index(current) + 1) % len(ids) if current in ids else 0
        self._device_id = ids[idx]
        self._sp.transfer_playback(device_id=self._device_id, force_play=True)
        return devices[idx]["name"]

    def list_devices(self) -> list[dict]:
        return self._sp.devices().get("devices", [])


def _first_image(obj: dict) -> str | None:
    images = obj.get("images") or []
    return images[0]["url"] if images else None
