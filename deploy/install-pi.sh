#!/usr/bin/env bash
# One-shot Raspberry Pi setup. Run on the Pi as the user who will run the
# player (fresh Raspberry Pi OS Lite, Bookworm or later):
#
#   curl -sL https://raw.githubusercontent.com/pinoaudrey/modern-record-player/main/deploy/install-pi.sh | bash
#
# Safe to re-run: it updates the checkout and reinstalls the service.
# Reboot once afterwards so SPI is enabled, then wire the RC522 and run
# `.venv/bin/python -m vinyl auth`.
set -euo pipefail

REPO_URL=https://github.com/pinoaudrey/modern-record-player.git
DIR="$HOME/Documents/modern-record-player"
DEVICE_NAME="Record Player"

echo "== System packages"
sudo apt-get update -q
# python3-rpi-lgpio is the Pi 5 compatible RPi.GPIO replacement; the venv is
# created with --system-site-packages so it and spidev come from apt instead
# of being compiled by pip.
sudo apt-get install -y -q git python3-venv python3-pip python3-rpi-lgpio python3-spidev mpg123 alsa-utils curl

echo "== Enable SPI (takes effect after a reboot)"
sudo raspi-config nonint do_spi 0

echo "== raspotify (Spotify Connect target)"
if ! dpkg -s raspotify >/dev/null 2>&1; then
  curl -sL https://dtcooper.github.io/raspotify/install.sh | sh
fi
if sudo grep -q '^#\?LIBRESPOT_NAME=' /etc/raspotify/conf; then
  sudo sed -i "s|^#\?LIBRESPOT_NAME=.*|LIBRESPOT_NAME=\"$DEVICE_NAME\"|" /etc/raspotify/conf
else
  echo "LIBRESPOT_NAME=\"$DEVICE_NAME\"" | sudo tee -a /etc/raspotify/conf >/dev/null
fi
sudo systemctl enable raspotify >/dev/null
sudo systemctl restart raspotify

echo "== Code"
mkdir -p "$(dirname "$DIR")"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only || echo "git pull failed (private repo with no credentials on the Pi?), using the existing checkout"
else
  git clone "$REPO_URL" "$DIR"
fi
cd "$DIR"
[ -d .venv ] || python3 -m venv --system-site-packages .venv
.venv/bin/pip install -q -e .
# mfrc522 declares a dependency on the real RPi.GPIO, which must never be
# installed next to rpi-lgpio on a Pi 5. Install it without dependencies.
.venv/bin/pip install -q --no-deps mfrc522

echo "== Config"
if [ ! -f config.toml ]; then
  sed -e 's/^driver = "fake"/driver = "rc522"/' \
      -e "s/^device_name = .*/device_name = \"$DEVICE_NAME\"/" \
      config.example.toml > config.toml
fi

echo "== Sudo rule (lets the player restart itself after an update, and reboot from the admin)"
# Exactly two commands, no password, checked with visudo before it goes live.
SUDOERS_TMP=$(mktemp)
cat > "$SUDOERS_TMP" <<EOF
# Installed by modern-record-player/deploy/install-pi.sh. The player runs as
# $USER and restarts its own service after a self-update; the admin's status
# page has a reboot button. Nothing else.
$USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart record-player@$USER, /usr/sbin/reboot
EOF
sudo visudo -cf "$SUDOERS_TMP" >/dev/null
sudo install -m 0440 -o root -g root "$SUDOERS_TMP" /etc/sudoers.d/record-player
rm -f "$SUDOERS_TMP"

echo "== Service"
sudo cp deploy/record-player.service "/etc/systemd/system/record-player@$USER.service"
sudo cp deploy/record-player-update.service "/etc/systemd/system/record-player-update@$USER.service"
sudo cp deploy/record-player-update.timer "/etc/systemd/system/record-player-update@$USER.timer"
sudo systemctl daemon-reload
sudo systemctl enable "record-player@$USER" >/dev/null
sudo systemctl restart "record-player@$USER"
# Nightly self-update at 04:30 (+ up to 30 min). `[updates] auto = false` in
# config.toml turns it into a no-op without touching systemd.
sudo systemctl enable --now "record-player-update@$USER.timer" >/dev/null

echo
echo "Done. Next steps:"
if [ -e /dev/spidev0.0 ]; then
  echo "  1. SPI is enabled."
else
  echo "  1. sudo reboot                 (enables SPI)"
fi
echo "  2. Power off, wire the RC522    (README, RST on physical pin 22)"
echo "  3. Connect Spotify from any phone or laptop: http://$(hostname).local:8090/auth"
echo "  4. Admin: http://$(hostname).local:8090"
