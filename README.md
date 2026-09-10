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
- **Make these records**: the player polls Spotify for your recently played
  tracks every five minutes and keeps them (Spotify itself only remembers the
  last 50). The "Make these records" page ranks the albums, playlists and
  artists you actually play over the last week, month, or all time, alongside
  Spotify's own top-albums and top-artists picks, and marks what's already on
  the shelf. Every row has a "Make a record" button that arms a write.
- A card left resting on the reader plays once. Lift it for a couple of
  seconds and put it back to start it over, like a record on the platter.
- Control cards (play/pause, next, prev, shuffle, switch device) are assigned
  the same way from the register page.
- **Status**: the Status page shows whether everything is wired up (reader
  chip, Spotify account and device, raspotify, history poller, CPU
  temperature, disk, updates, backups), with an update button. See below.
- Sound cues go in `sounds/` (see `sounds/README.md`). Missing ones are
  generated at startup.

CLI equivalents, handy over ssh (stop the service first if using `write`,
the RC522 can't be shared between processes):

```bash
.venv/bin/python -m vinyl now                     # what's playing, and what a card of it would hold
.venv/bin/python -m vinyl write <spotify link>    # write that URI to the next card tapped
.venv/bin/python -m vinyl history                 # poll play history once, list recent plays
.venv/bin/python -m vinyl update                  # pull + reinstall + restart if behind (what the nightly timer runs)
.venv/bin/python -m vinyl backup                  # copy records.db into backups/ now
.venv/bin/python -m vinyl sounds                  # regenerate the wav sound cues
```

Spotify-curated playlists (Discover Weekly, Today's Top Hits, anything whose
id starts with `37i9dQZ`) can't be looked up by an app in Spotify's
development mode. "Make a record of this" falls back to the current album
and says so; history shows them as "Spotify curated playlist", and a card of
one still plays.

### Service

`install-pi.sh` installs and enables `record-player@<user>`. Useful commands:

```bash
sudo systemctl status record-player@$USER
journalctl -u record-player@$USER -f
sudo systemctl restart record-player@$USER
```

### Status

`http://<pi-hostname>.local:8090/status` (the "Status" link in the admin), or
`GET /api/health` for the same thing as JSON. Every row has a green dot or an
orange triangle:

- Player: version (git hash and commit date), uptime, reader driver, and for
  the RC522 the chip's version register, read once at startup. `0x91`/`0x92`
  is a genuine MFRC522, other values are clones (they work), `0x00`/`0xFF`
  means nothing is answering on SPI: check the wiring and that SPI is
  enabled.
- Spotify: connected, account name, whether the configured `device_name` is
  in the Spotify Connect device list right now, and the active device.
- raspotify: `systemctl is-active raspotify`.
- History: plays collected, last poll, last error.
- System: CPU temperature, free disk, whether an update is available, last
  backup.

A Spotify or git hiccup shows as a warning row, never an error page.

### Updates

The status page checks the git remote (at most every 15 minutes, "Check
again" forces it) and shows how many commits behind the player is. "Update
now" runs `git pull --ff-only`, reinstalls the package if `pyproject.toml`
changed, and restarts the service. The restart needs root, so
`install-pi.sh` installs `/etc/sudoers.d/record-player` (checked with
`visudo -cf`, mode 0440) that lets the player's user run exactly these two
commands without a password:

```
/usr/bin/systemctl restart record-player@<user>
/usr/sbin/reboot
```

The second one is the "Reboot" button on the same page. If the sudo rule is
missing the update still lands; the page says so and asks you to restart
manually.

Nightly self-update: `install-pi.sh` also enables
`record-player-update@<user>.timer`, which runs `python -m vinyl update` at
04:30 (plus up to 30 minutes of random delay). That command pulls only when
the repo is behind, backs up the database first, and does nothing at all
when `config.toml` has

```toml
[updates]
auto = false
```

The timer isn't persistent: if the Pi is off at 04:30 it simply tries again
the next night. `journalctl -u record-player-update@$USER` shows what it did.

### Backups and restore

`records.db` (cards, play history) is copied with SQLite's online backup
API into `backups/records-YYYYMMDD-HHMM.db`: once when the player starts
(unless there is already one from today), then every 24 hours, keeping the
newest 14. `python -m vinyl backup` takes one now. `backups/` is
gitignored; copy it somewhere else now and then if you care about the
history.

To restore, stop the service, copy the backup over the database, start it
again:

```bash
sudo systemctl stop record-player@$USER
cp backups/records-20260909-0430.db records.db
rm -f records.db-wal records.db-shm
sudo systemctl start record-player@$USER
```

### Sound cues

The player chirps on scan, error, write, and so on. It plays `sounds/<cue>.mp3`
through mpg123 when that file exists, otherwise `sounds/<cue>.wav` through
`aplay`. At startup any cue with neither file gets a generated wav (simple
sine tones), so a fresh install has sound without any files to copy.
`python -m vinyl sounds` regenerates all of them; drop in your own mp3s to
override. Cue names are in `sounds/README.md`.

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
- ~~Slice 3: play-history poller, most-played reports, "make these records"
  list~~ done
- Slice 4: print-ready label sheets with high-res artwork; export/import a
  card set for a friend's player
- Small: device picker in the admin, visible "device not found" fallback
