"""Entry points: run the player + web admin, or one-off setup commands.

  python -m vinyl run              start the reader loop and web admin
  python -m vinyl auth             Spotify login from the terminal (or use the /auth web page)
  python -m vinyl devices          list Spotify Connect devices (find your raspotify)
  python -m vinyl now              show what's playing and what a card of it would hold
  python -m vinyl history          poll play history once and list recent plays
  python -m vinyl write <link>     write a share link's URI onto the next card tapped
  python -m vinyl resolve <link>   debug: parse + look up a share link
  python -m vinyl update           pull + reinstall + restart if the repo is behind (nightly timer)
  python -m vinyl backup           copy records.db into backups/ now
  python -m vinyl sounds           regenerate the wav sound cues in sounds/

`write` talks to the reader directly, so on the Pi stop the service first
(sudo systemctl stop record-player@$USER); two processes can't share the RC522.
"""

import logging
import sys
import threading

import uvicorn

from . import backup, updates
from .config import load_config
from .db import Database
from .history import HistoryPoller
from .links import parse_ref
from .player import Player
from .reader import make_reader
from .sounds import Sounds
from .spotify import NotAuthorized, SpotifyClient, make_auth_manager
from .status import Health
from .tones import generate as generate_tones
from .web import create_app

log = logging.getLogger(__name__)


def cmd_run() -> None:
    cfg = load_config()
    db = Database(cfg.db_path)
    spotify = SpotifyClient(cfg)
    reader = make_reader(cfg.reader_driver, cfg.reader_rst_pin)
    sounds = Sounds(cfg.sounds_dir)
    sounds.ensure_cues()
    player = Player(
        db, spotify, reader, sounds, scan_cooldown=cfg.scan_cooldown,
        lift_to_pause=cfg.lift_to_pause, lift_timeout=cfg.lift_timeout,
        resume_window=cfg.resume_window, tap_while_playing=cfg.tap_while_playing,
    )

    if not spotify.authorized:
        log.warning("Spotify is not connected yet; cards will not play until you use the admin's Connect Spotify page (/auth)")

    threading.Thread(target=player.run_forever, daemon=True, name="scan-loop").start()
    poller = HistoryPoller(db, spotify, interval=cfg.history_interval)
    threading.Thread(target=poller.run_forever, daemon=True, name="history").start()
    backups_dir = cfg.root / "backups"
    threading.Thread(
        target=backup.run_forever, args=(cfg.db_path, backups_dir), daemon=True, name="backup",
    ).start()

    health = Health(
        db, spotify, reader, poller, repo=cfg.root, device_name=cfg.device_name,
        updates_auto=cfg.updates_auto, backups_dir=backups_dir,
    )
    app = create_app(db, spotify, player, reader=reader, poller=poller, health=health)
    log.info("Web admin on http://%s:%s (reader: %s, version %s)",
             cfg.web_host, cfg.web_port, cfg.reader_driver, health.version)
    uvicorn.run(app, host=cfg.web_host, port=cfg.web_port, log_level="warning")


def cmd_update() -> None:
    """Nightly self-update (record-player-update@<user>.timer). Does nothing
    when [updates] auto is false; otherwise pulls only if the repo is behind."""
    cfg = load_config()
    if not cfg.updates_auto:
        return
    behind, latest = updates.check(cfg.root)
    if not behind:
        print(f"Up to date ({updates.version(cfg.root)}).")
        return
    print(f"{behind} commit(s) behind, latest: {latest}")
    backup.backup(cfg.db_path, cfg.root / "backups")
    result = updates.apply(cfg.root)
    print(result.summary)
    if result.warning:
        print(result.warning)
        sys.exit(1)


def cmd_backup() -> None:
    cfg = load_config()
    dest = backup.backup(cfg.db_path, cfg.root / "backups")
    print(f"Backed up to {dest}")


def cmd_sounds() -> None:
    cfg = load_config()
    for path in generate_tones(cfg.sounds_dir):
        print(f"wrote {path}")


def cmd_auth() -> None:
    """Terminal fallback for the web page at /auth. Prints the login URL, then
    asks for the address the browser lands on. Works over ssh."""
    cfg = load_config()
    print("Easier: open http://<this-pi>.local:8090/auth from any phone or laptop.\n")
    auth = make_auth_manager(cfg, open_browser=False)
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


def _describe(content) -> str:
    line = f"{content.content_type}: {content.name}"
    if content.artist:
        line += f" - {content.artist}"
    return line


def cmd_resolve(link: str) -> None:
    cfg = load_config()
    ref = parse_ref(link)
    if ref is None:
        print("No Spotify content found in that text")
        sys.exit(1)
    resolved = SpotifyClient(cfg).resolve(ref)
    print(_describe(resolved))
    print(resolved.uri)


def cmd_now() -> None:
    cfg = load_config()
    spotify = SpotifyClient(cfg)
    np = spotify.now_playing()
    if np is None:
        print("Nothing playing.")
        return
    state = "playing" if np.is_playing else "paused"
    print(f"{np.track_name} - {np.artist} ({np.album_name}) [{state} on {np.device_name}]")
    content = spotify.now_playing_content()
    print(f"A record of this would hold -> {_describe(content)}")
    print(content.uri)


def cmd_history() -> None:
    """Poll play history once and show what's been collected."""
    cfg = load_config()
    db = Database(cfg.db_path)
    poller = HistoryPoller(db, SpotifyClient(cfg))
    new = poller.poll_once()
    print(f"{new} new plays, {db.play_count()} total")
    for p in db.recent_plays(10):
        print(f"{p.played_at[:16]}  {p.track_name} - {p.artist}  ({p.album_name})")


def cmd_write(link: str) -> None:
    cfg = load_config()
    ref = parse_ref(link)
    if ref is None:
        print("No Spotify content found in that text")
        sys.exit(1)
    content = SpotifyClient(cfg).resolve(ref)
    reader = make_reader(cfg.reader_driver, cfg.reader_rst_pin)
    db = Database(cfg.db_path)
    print(f"Hold a card on the reader to write {_describe(content)} ...")
    scan = reader.write(content.uri, timeout=60)
    if scan is None:
        print("No card seen within 60 seconds.")
        sys.exit(1)
    db.save_content_card(
        uid=scan.uid, uri=content.uri, content_type=content.content_type, name=content.name,
        artist=content.artist, artwork_url=content.artwork_url, on_tag=True,
    )
    print(f"Wrote {content.uri} to card {scan.uid} and registered it.")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = sys.argv[1:]
    cmd = args[0] if args else "run"
    try:
        if cmd == "run":
            cmd_run()
        elif cmd == "auth":
            cmd_auth()
        elif cmd == "devices":
            cmd_devices()
        elif cmd == "now":
            cmd_now()
        elif cmd == "history":
            cmd_history()
        elif cmd == "resolve" and len(args) > 1:
            cmd_resolve(args[1])
        elif cmd == "write" and len(args) > 1:
            cmd_write(args[1])
        elif cmd == "update":
            cmd_update()
        elif cmd == "backup":
            cmd_backup()
        elif cmd == "sounds":
            cmd_sounds()
        else:
            print(__doc__)
            sys.exit(2)
    except NotAuthorized as e:
        print(e)
        sys.exit(1)
    except updates.UpdateError as e:
        print(f"Update failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
