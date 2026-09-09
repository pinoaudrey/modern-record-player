# Modern Record Player

RFID "vinyl" record player for Spotify on a Raspberry Pi 5. Scan a card, the
album plays. Registering a card is a web form, not a code edit: scan any blank
card, open the admin page, paste a Spotify share link, done.

Cards can also carry their own Spotify URI in the tag's memory. Play
something on your phone, click "Make a record of this" in the admin, hold a
blank card on the reader, done: the card is registered on this player and
plays on any other player without registering it again.

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
| RST   | GPIO25 (physical pin 22), or any free GPIO, see below |
| 3.3V  | 3V3    |

RST can go to any free GPIO as long as `rst_pin` in `config.toml` matches, in
physical pin numbering: pin 22 is GPIO25 (the library default), pin 16 is
GPIO23 (what the 2021 build used). If cards are never detected, this
mismatch is the first thing to check.

Cards: MIFARE Classic 1K, the white cards and blue fobs that ship with RC522
kits (and the ones from the 2021 build). They hold 48 characters of text,
enough for any Spotify URI. NFC stickers of the NTAG type read fine for their
UID but cannot be written by this library, so they only work as
database-registered cards.

## Pi setup

Flash Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager, setting the
hostname, your user, Wi-Fi and SSH in its customisation screen. Then, on the
Pi:

```bash
curl -sL https://raw.githubusercontent.com/pinoaudrey/modern-record-player/main/deploy/install-pi.sh | bash
sudo reboot
```

The script enables SPI, installs raspotify (named "Record Player" so it's easy
to find in the Spotify app), clones this repo into `~/Documents`, builds the
venv, writes `config.toml`, and installs the systemd service so the player
starts at boot. Safe to re-run to update.

Pi 5 note: the GPIO stack comes from apt (`python3-rpi-lgpio`, a drop-in
`RPi.GPIO` replacement for the Pi 5's RP1 chip) and the venv sees it via
`--system-site-packages`. The `mfrc522` package declares a dependency on the
real `RPi.GPIO`, so it is installed with `--no-deps`. Never install the real
`RPi.GPIO` alongside the shim.

## Spotify setup (one time)

The app uses Authorization Code with PKCE: no client secret is stored on the
Pi. Requirements on the Spotify dashboard app:

- Redirect URI `http://127.0.0.1:8080/callback` (`localhost` is no longer
  allowed by Spotify)
- Any account that will use the player must be added under User Management
  (dev mode allows 5 users) and needs Premium for playback

Then open `http://recordplayer.local:8090/auth` (your Pi's hostname) from
any phone or laptop and follow the three steps: open the Spotify login, copy
the address of the page it lands on (it starts with
`http://127.0.0.1:8080/callback?code=` and won't load, which is expected),
paste it back. No terminal needed. `python -m vinyl auth` over ssh does the
same thing in text form.

Open Spotify on your phone, play anything, pick the raspotify device once so
it shows up, then confirm the player can see it:

```bash
.venv/bin/python -m vinyl devices
```

The install script names the raspotify device "Record Player" and sets
`device_name` in `config.toml` to match.

## Run

```bash
.venv/bin/python -m vinyl run
```

Web admin: `http://<pi-hostname>.local:8090`

- Scan an unregistered card: the player chirps and the admin shows a
  "register it" banner. Paste any Spotify share link, save, scan again to play.
- **Make a record of what's playing**: the admin's "Now playing" panel shows
  the playlist/album/artist you're playing from (or the track's album if
  you're in Liked Songs or a queue). Click "Make a record of this", hold a
  blank card on the reader, and the URI is written into the card and the
  card is registered in one step.
- **Self-describing cards**: any card whose memory holds a Spotify URI plays
  on any player, even one that has never seen it. The player registers it on
  first scan (name and artwork are looked up, or filled in later if offline).
  Cards written this way show an "on tag" badge. "Write to card" on any
  existing card writes its URI onto a card, so you can make copies for a
  friend's player.
- A card left resting on the reader plays once. Lift it for a couple of
  seconds and put it back to start it over, like a record on the platter.
- Control cards (play/pause, next, prev, shuffle, switch device) are assigned
  the same way from the register page.
- Sound cues go in `sounds/` (see `sounds/README.md`).

CLI equivalents, handy over ssh (stop the service first if using `write`,
the RC522 can't be shared between processes):

```bash
.venv/bin/python -m vinyl now                     # what's playing, and what a card of it would hold
.venv/bin/python -m vinyl write <spotify link>    # write that URI to the next card tapped
```

### Service

`install-pi.sh` installs and enables `record-player@<user>`. Useful commands:

```bash
sudo systemctl status record-player@$USER
journalctl -u record-player@$USER -f
sudo systemctl restart record-player@$USER
```

## Development (no Pi needed)

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp config.example.toml config.toml      # reader driver "fake"
.venv/bin/pytest
.venv/bin/python -m vinyl run
```

With the fake reader, simulate a card scan, or a scan of a card that already
carries a URI in its memory:

```bash
curl -X POST http://127.0.0.1:8090/dev/scan -d uid=12345
curl -X POST http://127.0.0.1:8090/dev/scan -d uid=777 -d text=spotify:album:22py1IeIi51c0GBYEHQTsI
```

The fake reader remembers what was written to each fake card, and treats the
last scanned card as still resting on the reader for a few seconds, so
"arm a write, then scan" works the same way it does on the hardware.

## Roadmap

- ~~Slice 2: write URIs onto the tags themselves (self-describing cards) and a
  "tag what's playing" flow~~ done
- Slice 3: play-history poller, most-played reports, "make these records" list
- Slice 4: print-ready label sheets with high-res artwork
