"""Parse Spotify share links and URIs so nobody ever hand-strips an ID again."""

import re
from dataclasses import dataclass

CONTENT_TYPES = {"track", "album", "playlist", "artist"}

_URL_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z]{2}(?:-[A-Za-z]{2})?/)?"
    r"(track|album|playlist|artist)/([A-Za-z0-9]{22})"
)
_URI_RE = re.compile(r"spotify:(track|album|playlist|artist):([A-Za-z0-9]{22})")


@dataclass(frozen=True)
class SpotifyRef:
    type: str
    id: str

    @property
    def uri(self) -> str:
        return f"spotify:{self.type}:{self.id}"


def is_editorial_playlist(ref: SpotifyRef) -> bool:
    """Spotify-curated playlists (Today's Top Hits, Discover Weekly, ...) share
    an id prefix and can't be described by the API from a dev-mode app."""
    return ref.type == "playlist" and ref.id.startswith("37i9dQZ")


def parse_ref(text: str) -> SpotifyRef | None:
    """Accept a share URL, a bare spotify: URI, or pasted text containing either."""
    text = text.strip()
    m = _URL_RE.search(text) or _URI_RE.search(text)
    if not m:
        return None
    return SpotifyRef(type=m.group(1), id=m.group(2))
