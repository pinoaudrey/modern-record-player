"""Health data for the admin's /status page and GET /api/health.

Everything here is best-effort: a Spotify or git failure becomes a warning
row, never an exception out of a request.
"""

import logging
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import updates
from .backup import last_backup

log = logging.getLogger(__name__)

UPDATE_CHECK_TTL = 15 * 60          # seconds between `git fetch`es for the status page
LOW_DISK_BYTES = 500 * 1024 * 1024
HOT_CPU_C = 80.0
CPU_TEMP_PATH = Path("/sys/class/thermal/thermal_zone0/temp")


def item(label: str, value, ok: bool = True) -> dict:
    return {"label": label, "value": str(value), "ok": bool(ok)}


def section(name: str, items: list[dict]) -> dict:
    return {"name": name, "items": items, "ok": all(i["ok"] for i in items)}


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def local(iso: str | None, fmt: str = "%b %d, %H:%M") -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime(fmt)


class Health:
    def __init__(
        self, db, spotify, reader, poller, repo: Path, device_name: str,
        updates_auto: bool = True, backups_dir: Path | None = None,
        check_updates=updates.check, cpu_temp_path: Path = CPU_TEMP_PATH,
    ):
        self._db = db
        self._spotify = spotify
        self._reader = reader
        self._poller = poller
        self.repo = Path(repo)
        self.device_name = device_name
        self.updates_auto = updates_auto
        self.backups_dir = Path(backups_dir) if backups_dir else self.repo / "backups"
        self._check_updates = check_updates
        self._cpu_temp_path = cpu_temp_path
        self.version = updates.version(self.repo)   # read once; a restart follows any update
        self._started = time.monotonic()
        self._update_lock = threading.Lock()
        self._update_cache: dict | None = None
        self._update_checked_at = 0.0

    # --- updates (cached: a git fetch per page load would be slow) ------------

    def update_status(self, refresh: bool = False) -> dict:
        with self._update_lock:
            stale = time.monotonic() - self._update_checked_at > UPDATE_CHECK_TTL
            if refresh or stale or self._update_cache is None:
                try:
                    behind, latest = self._check_updates(self.repo)
                    self._update_cache = {"behind": behind, "latest": latest, "error": None}
                except Exception as e:
                    log.warning("Update check failed: %s", e)
                    self._update_cache = {"behind": 0, "latest": None, "error": str(e)}
                self._update_cache["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self._update_checked_at = time.monotonic()
            return dict(self._update_cache)

    def invalidate_updates(self) -> None:
        with self._update_lock:
            self._update_cache = None
            self._update_checked_at = 0.0

    # --- the sections ---------------------------------------------------------

    def collect(self, refresh_updates: bool = False) -> dict:
        update = self.update_status(refresh=refresh_updates)
        sections = [
            self._player(),
            self._spotify_section(),
            self._raspotify(),
            self._history(),
            self._system(update),
        ]
        return {
            "ok": all(s["ok"] for s in sections),
            "version": self.version,
            "uptime_seconds": int(time.monotonic() - self._started),
            "update": update,
            "sections": sections,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def _player(self) -> dict:
        items = [
            item("Version", self.version, self.version != "unknown"),
            item("Uptime", fmt_duration(time.monotonic() - self._started)),
        ]
        status = getattr(self._reader, "status", None)
        if status is None:
            items.append(item("Reader", "none", False))
        else:
            try:
                st = status()
                items.append(item("Reader", st.get("driver", "?")))
                if "chip" in st:
                    items.append(item("RC522 chip", st["chip"], st.get("ok", True)))
            except Exception as e:
                items.append(item("Reader", f"error: {e}", False))
        return section("Player", items)

    def _spotify_section(self) -> dict:
        sp = self._spotify
        try:
            connected = bool(getattr(sp, "authorized", True))
        except Exception:
            connected = False
        items = [item("Connected", "yes" if connected else "no, open the Spotify page", connected)]
        if not connected:
            return section("Spotify", items)
        try:
            me = sp.me() or {}
            items.append(item("Account", me.get("display_name") or me.get("id") or "?"))
        except Exception as e:
            items.append(item("Account", f"couldn't ask Spotify: {e}", False))
        try:
            devices = sp.list_devices()
            names = [d.get("name", "?") for d in devices]
            present = any(n.lower() == self.device_name.lower() for n in names)
            if present:
                items.append(item("Device", f"{self.device_name} (in the Connect list)"))
            else:
                seen = ", ".join(names) if names else "no devices at all"
                items.append(item("Device", f"{self.device_name} not in the Connect list ({seen})", False))
            active = next((d.get("name") for d in devices if d.get("is_active")), None)
            items.append(item("Active device", active or "none"))
        except Exception as e:
            items.append(item("Device", f"couldn't list devices: {e}", False))
        return section("Spotify", items)

    def _raspotify(self) -> dict:
        if shutil.which("systemctl") is None:
            return section("raspotify", [item("Service", "n/a (no systemctl)")])
        try:
            r = subprocess.run(
                ["systemctl", "is-active", "raspotify"], capture_output=True, text=True, timeout=5,
            )
            state = (r.stdout or r.stderr).strip() or "unknown"
        except Exception as e:
            state = f"unknown ({e})"
        return section("raspotify", [item("Service", state, state == "active")])

    def _history(self) -> dict:
        items = [item("Plays collected", self._db.play_count())]
        poller = self._poller
        if poller is None:
            items.append(item("Poller", "not running", False))
            return section("History", items)
        last = poller.last_poll_at
        items.append(item("Last poll", local(last) if last else "never yet", last is not None))
        err = poller.last_error
        items.append(item("Last error", err or "none", not err))
        return section("History", items)

    def _system(self, update: dict) -> dict:
        items = []
        temp = self.cpu_temp()
        if temp is not None:
            items.append(item("CPU temperature", f"{temp:.1f} C", temp < HOT_CPU_C))
        try:
            free = shutil.disk_usage(self.repo).free
            items.append(item("Free disk", fmt_bytes(free), free > LOW_DISK_BYTES))
        except OSError as e:
            items.append(item("Free disk", f"unknown ({e})", False))
        if update.get("error"):
            items.append(item("Update", f"couldn't check: {update['error']}", False))
        elif update.get("behind"):
            n = update["behind"]
            items.append(item("Update", f"{n} commit{'s' if n != 1 else ''} behind: {update.get('latest') or ''}", False))
        else:
            items.append(item("Update", "up to date"))
        items.append(item("Nightly self-update", "on" if self.updates_auto else "off"))
        last = last_backup(self.backups_dir)
        items.append(item("Last backup", last.name if last else "none yet", last is not None))
        return section("System", items)

    def cpu_temp(self) -> float | None:
        try:
            return int(self._cpu_temp_path.read_text().strip()) / 1000
        except (OSError, ValueError):
            return None
