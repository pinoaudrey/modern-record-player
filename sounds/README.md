# Sound cues

Drop mp3 files here (gitignored). The player looks for:

- `startup.mp3` - service started
- `accept.mp3` - card recognized
- `error.mp3` - unknown card, failed write, or playback error
- `written.mp3` - a URI was written to a card
- `connect_device.mp3` - no Spotify Connect device available
- `shuffle_on.mp3` / `shuffle_off.mp3`

Missing files are skipped silently. Copy the mp3s from the original build.
