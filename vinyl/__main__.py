"""Entry points: run the player + web admin, or one-off setup commands.

  python -m vinyl run       start the reader loop and web admin
  python -m vinyl auth      run the one-time Spotify PKCE authorization
  python -m vinyl devices   list Spotify Connect devices (find your raspotify)
  python -m vinyl resolve <link>   debug: parse + look up a share link
"""

import logging
import sys
import threading

import uvicorn

from .config import load_config
from .db import Database
from .links import parse_ref
from .player import Player
from .reader import make_reader
from .sounds import Sounds
from .spotify import SpotifyClient, make_auth_manager
from .web import create_app

log = logging.getLogger(__name__)


def cmd_run() -> None:
    cfg = load_config()
    db = Database(cfg.db_path)
    spotify = SpotifyClient(cfg)
    reader = make_reader(cfg.reader_driver)
    sounds = Sounds(cfg.sounds_dir)
    player = Player(db, spotify, reader, sounds, scan_cooldown=cfg.scan_cooldown)

    threading.Thread(target=player.run_forever, daemon=True, name="scan-loop").start()

    app = create_app(db, spotify, player, reader=reader)
    log.info("Web admin on http://%s:%s (reader: %s)", cfg.web_host, cfg.web_port, cfg.reader_driver)
    uvicorn.run(app, host=cfg.web_host, port=cfg.web_port, log_level="warning")


def cmd_auth() -> None:
    cfg = load_config()
    auth = make_auth_manager(cfg, open_browser=True)
    token = auth.get_access_token()
    if token:
        print("Authorized. Token cached at .spotify_token_cache")


def cmd_devices() -> None:
    cfg = load_config()
    spotify = SpotifyClient(cfg)
    devices = spotify.list_devices()
    if not devices:
        print("No Spotify Connect devices found. Is raspotify running and visible in the Spotify app?")
        return
    for d in devices:
        marker = " (active)" if d.get("is_active") else ""
        print(f"{d['name']:30} {d['id']}{marker}")


def cmd_resolve(link: str) -> None:
    cfg = load_config()
    ref = parse_ref(link)
    if ref is None:
        print("No Spotify content found in that text")
        sys.exit(1)
    resolved = SpotifyClient(cfg).resolve(ref)
    print(f"{resolved.content_type}: {resolved.name}"
          + (f" - {resolved.artist}" if resolved.artist else ""))
    print(resolved.uri)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = sys.argv[1:]
    cmd = args[0] if args else "run"
    if cmd == "run":
        cmd_run()
    elif cmd == "auth":
        cmd_auth()
    elif cmd == "devices":
        cmd_devices()
    elif cmd == "resolve" and len(args) > 1:
        cmd_resolve(args[1])
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
