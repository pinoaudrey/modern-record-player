import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    client_id: str
    redirect_uri: str
    device_name: str
    reader_driver: str
    reader_rst_pin: int
    scan_cooldown: float
    lift_to_pause: bool
    lift_timeout: float
    resume_window: float
    history_interval: float
    web_host: str
    web_port: int
    db_path: Path
    sounds_dir: Path
    root: Path
    updates_auto: bool = True  # [updates] auto: nightly self-update timer applies updates
    web_pin: str = ""          # [web] pin: admin login PIN; empty means no login


def load_config(root: Path | None = None) -> Config:
    root = root or Path(__file__).resolve().parent.parent
    path = root / "config.toml"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy config.example.toml to config.toml and edit it."
        )
    raw = tomllib.loads(path.read_text())
    player = raw.get("player", {})
    return Config(
        client_id=raw["spotify"]["client_id"],
        redirect_uri=raw["spotify"].get("redirect_uri", "http://127.0.0.1:8080/callback"),
        device_name=raw["spotify"].get("device_name", "raspotify"),
        reader_driver=raw["reader"].get("driver", "fake"),
        reader_rst_pin=int(raw["reader"].get("rst_pin", 22)),
        scan_cooldown=float(raw["reader"].get("scan_cooldown", 2.0)),
        lift_to_pause=bool(player.get("lift_to_pause", True)),
        lift_timeout=float(player.get("lift_timeout", 1.5)),
        resume_window=float(player.get("resume_window", 900)),
        history_interval=float(raw.get("history", {}).get("poll_interval", 300)),
        web_host=raw["web"].get("host", "0.0.0.0"),
        web_port=int(raw["web"].get("port", 8090)),
        db_path=root / raw["paths"].get("db", "records.db"),
        sounds_dir=root / raw["paths"].get("sounds", "sounds"),
        root=root,
        updates_auto=bool(raw.get("updates", {}).get("auto", True)),
        web_pin=str(raw["web"].get("pin", "") or "").strip(),
    )


_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]")
_DEVICE_NAME_RE = re.compile(r"^\s*device_name\s*=")


def save_device_name(path: Path, name: str) -> None:
    """Rewrite only the `device_name = "..."` line of config.toml, keeping
    every other byte as it was (comments, ordering, line endings). Adds the
    line under [spotify] when there isn't one, and a [spotify] table when
    even that is missing."""
    path = Path(path)
    with path.open("r", encoding="utf-8", newline="") as f:
        lines = f.readlines()
    newline = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    # TOML basic strings escape the same way JSON does for anything a
    # device name can hold (quotes, backslashes, unicode as itself).
    new_line = f"device_name = {json.dumps(name, ensure_ascii=False)}"

    section = None
    spotify_header = None
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1).strip()
            if section == "spotify" and spotify_header is None:
                spotify_header = i
            continue
        if section == "spotify" and _DEVICE_NAME_RE.match(line):
            ending = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
            lines[i] = new_line + ending
            break
    else:
        if spotify_header is None:
            if lines and not lines[-1].endswith(("\n", "\r\n")):
                lines[-1] += newline
            lines += [newline, "[spotify]" + newline, new_line + newline]
        else:
            lines.insert(spotify_header + 1, new_line + newline)

    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        f.writelines(lines)
    os.replace(tmp, path)
