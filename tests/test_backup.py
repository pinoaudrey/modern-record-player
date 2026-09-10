import sqlite3
import threading
import time

from vinyl import backup as backup_mod
from vinyl.backup import backup, has_backup_today, last_backup, list_backups, run_forever
from vinyl.db import Database


def test_backup_copies_a_consistent_database(db, tmp_path):
    db.save_content_card("1", "spotify:album:a", "album", "Album", "Artist", None)
    dest = backup(db._path if hasattr(db, "_path") else tmp_path / "test.db", tmp_path / "backups")
    assert dest.parent == tmp_path / "backups"
    assert dest.name.startswith("records-") and dest.suffix == ".db"
    assert len(dest.stem) == len("records-YYYYMMDD-HHMM")

    copy = Database(dest)
    try:
        assert [c.uid for c in copy.list_cards()] == ["1"]
        assert copy.schema_version() == db.schema_version()
    finally:
        copy.close()
    assert last_backup(tmp_path / "backups") == dest
    assert has_backup_today(tmp_path / "backups")


def test_backup_prunes_beyond_keep(tmp_path):
    src = tmp_path / "records.db"
    sqlite3.connect(src).close()
    bdir = tmp_path / "backups"
    bdir.mkdir()
    for day in range(1, 6):
        (bdir / f"records-202601{day:02d}-0400.db").write_bytes(b"")
    (bdir / "unrelated.db").write_bytes(b"")

    dest = backup(src, bdir, keep=3)
    names = [p.name for p in list_backups(bdir)]
    assert names == ["records-20260104-0400.db", "records-20260105-0400.db", dest.name]
    assert (bdir / "unrelated.db").exists()


def test_has_backup_today_is_false_for_old_files(tmp_path):
    bdir = tmp_path / "backups"
    bdir.mkdir()
    (bdir / "records-20200101-0400.db").write_bytes(b"")
    assert not has_backup_today(bdir)
    assert last_backup(bdir).name == "records-20200101-0400.db"
    assert last_backup(tmp_path / "nowhere") is None


def test_run_forever_backs_up_at_start_only_when_none_today(tmp_path, monkeypatch):
    src = tmp_path / "records.db"
    sqlite3.connect(src).close()
    bdir = tmp_path / "backups"
    calls = []
    monkeypatch.setattr(backup_mod, "backup", lambda *a, **k: calls.append(a))

    stop = threading.Event()
    stop.set()                                  # one pass, no waiting
    run_forever(src, bdir, stop=stop)
    assert len(calls) == 1                      # nothing from today yet -> backup now

    bdir.mkdir()
    (bdir / f"records-{time.strftime('%Y%m%d')}-0000.db").write_bytes(b"")
    calls.clear()
    stop = threading.Event()
    stop.set()
    run_forever(src, bdir, stop=stop)
    assert calls == []                          # already have today's -> wait for the interval


def test_run_forever_survives_a_failed_backup(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr(backup_mod, "backup", boom)
    stop = threading.Event()
    stop.set()
    run_forever(tmp_path / "records.db", tmp_path / "backups", stop=stop)  # logs, doesn't raise
