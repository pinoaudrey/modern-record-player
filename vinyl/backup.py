"""Database backups.

A copy of records.db is taken with SQLite's online backup API (safe while the
player is running and writing) into backups/records-YYYYMMDD-HHMM.db, once a
day from a thread in `python -m vinyl run`, or on demand:

  python -m vinyl backup

Restore: stop the service, copy a backup over records.db, start it again
(README, "Backups and restore").
"""

import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_KEEP = 14
DAY = 24 * 60 * 60
PREFIX = "records-"


def backup(db_path: Path, backups_dir: Path, keep: int = DEFAULT_KEEP) -> Path:
    """Copy the database into backups_dir and prune old copies beyond `keep`."""
    db_path, backups_dir = Path(db_path), Path(backups_dir)
    backups_dir.mkdir(parents=True, exist_ok=True)
    dest = backups_dir / f"{PREFIX}{time.strftime('%Y%m%d-%H%M')}.db"
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    log.info("Backed up %s to %s", db_path.name, dest)
    for old in list_backups(backups_dir)[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
        log.info("Pruned old backup %s", old.name)
    return dest


def list_backups(backups_dir: Path) -> list[Path]:
    """Backups oldest first (the names sort chronologically)."""
    return sorted(Path(backups_dir).glob(f"{PREFIX}*.db"))


def last_backup(backups_dir: Path) -> Path | None:
    found = list_backups(backups_dir)
    return found[-1] if found else None


def has_backup_today(backups_dir: Path) -> bool:
    today = time.strftime("%Y%m%d")
    return any(p.name.startswith(f"{PREFIX}{today}-") for p in list_backups(backups_dir))


def run_forever(
    db_path: Path, backups_dir: Path, keep: int = DEFAULT_KEEP,
    interval: float = DAY, stop: threading.Event | None = None,
) -> None:
    """Back up now unless there's already one from today, then every `interval` seconds."""
    stop = stop or threading.Event()

    def attempt() -> None:
        try:
            backup(db_path, backups_dir, keep)
        except Exception as e:
            log.warning("Backup failed: %s", e)

    if not has_backup_today(backups_dir):
        attempt()
    while not stop.wait(interval):
        attempt()
