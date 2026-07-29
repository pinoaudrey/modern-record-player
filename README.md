# Modern Record Player

RFID "vinyl" record player for Spotify on a Raspberry Pi 5. Scan a card, the
album plays. Registering a card is a web form, not a code edit: scan any blank
card, open the admin page, paste a Spotify share link, done.

Successor to the 2021 build. Same architecture (raspotify as a Spotify Connect
target, the Web API for control), but the hardcoded `songMap.py` is replaced by
a SQLite database plus a web admin, auth is PKCE (no client secret anywhere),
and everything works on the Pi 5's new GPIO stack.

## Hardware

- Raspberry Pi 5
- RC522 RFID module, wired as before:

| RC522 | Pi pin |
| ----- | ------ |
| SDA   | GPIO8  |
| SCK   | GPIO11 |
| MOSI  | GPIO10 |
| MISO  | GPIO9  |
| GND   | GND    |
| RST   | GPIO25 (physical pin 22) |
| 3.3V  | 3V3    |

Note: the `mfrc522` library defaults to physical pin 22 (GPIO25) for RST.
The 2021 build docs said GPIO23; if reads fail on a rewired unit, this pin
is the first thing to check.

## Pi setup

```bash
sudo raspi-config nonint do_spi 0        # enable SPI
sudo apt update && sudo apt install -y python3-venv mpg123
curl -sL https://dtcooper.github.io/raspotify/install.sh | sh   # Spotify Connect target
```

```bash
cd ~/Documents
git clone https://github.com/pinoaudrey/modern-record-player.git
cd modern-record-player
python3 -m venv .venv
.venv/bin/pip install -e . -r requirements-pi.txt
cp config.example.toml config.toml      # then edit: set reader driver to "rc522"
```

Pi 5 note: `requirements-pi.txt` installs `rpi-lgpio`, a drop-in `RPi.GPIO`
replacement for the Pi 5's RP1 chip. Never install the real `RPi.GPIO`
alongside it.

## Spotify setup (one time)

The app uses Authorization Code with PKCE: no client secret is stored on the
Pi. Requirements on the Spotify dashboard app:

- Redirect URI `http://127.0.0.1:8080/callback` (`localhost` is no longer
  allowed by Spotify)
- Any account that will use the player must be added under User Management
  (dev mode allows 5 users) and needs Premium for playback

Then, on the Pi (with a browser, or copy the printed URL to another machine
and paste the redirect back):

```bash
.venv/bin/python -m vinyl auth
```

Open Spotify on your phone, play anything, pick the raspotify device once so
it shows up, then confirm the player can see it:

```bash
.venv/bin/python -m vinyl devices
```

Set `device_name` in `config.toml` to match.

## Run

```bash
.venv/bin/python -m vinyl run
```

Web admin: `http://<pi-hostname>.local:8090`

- Scan an unregistered card: the player chirps and the admin shows a
  "register it" banner. Paste any Spotify share link, save, scan again to play.
- Control cards (play/pause, next, prev, shuffle, switch device) are assigned
  the same way from the register page.
- Sound cues go in `sounds/` (see `sounds/README.md`).

### Run at boot

```bash
sudo cp deploy/record-player.service /etc/systemd/system/record-player@$(whoami).service
sudo systemctl enable --now record-player@$(whoami)
```

## Development (no Pi needed)

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp config.example.toml config.toml      # reader driver "fake"
.venv/bin/pytest
.venv/bin/python -m vinyl run
```

With the fake reader, simulate a card scan:

```bash
curl -X POST http://127.0.0.1:8090/dev/scan -d uid=12345
```

## Roadmap

- Slice 2: write URIs onto the tags themselves (self-describing cards) and a
  "tag what's playing" flow
- Slice 3: play-history poller, most-played reports, "make these records" list
- Slice 4: print-ready label sheets with high-res artwork
