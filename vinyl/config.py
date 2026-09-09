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
    history_interval: float
    web_host: str
    web_port: int
    db_path: Path
    sounds_dir: Path
    root: Path


def load_config(root: Path | None = None) -> Config:
    root = root or Path(__file__).resolve().parent.parent
    path = root / "config.toml"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy config.example.toml to config.toml and edit it."
        )
    raw = tomllib.loads(path.read_text())
    return Config(
        client_id=raw["spotify"]["client_id"],
        redirect_uri=raw["spotify"].get("redirect_uri", "http://127.0.0.1:8080/callback"),
        device_name=raw["spotify"].get("device_name", "raspotify"),
        reader_driver=raw["reader"].get("driver", "fake"),
        reader_rst_pin=int(raw["reader"].get("rst_pin", 22)),
        scan_cooldown=float(raw["reader"].get("scan_cooldown", 2.0)),
        history_interval=float(raw.get("history", {}).get("poll_interval", 300)),
        web_host=raw["web"].get("host", "0.0.0.0"),
        web_port=int(raw["web"].get("port", 8090)),
        db_path=root / raw["paths"].get("db", "records.db"),
        sounds_dir=root / raw["paths"].get("sounds", "sounds"),
        root=root,
    )
