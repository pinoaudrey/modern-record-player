# Sound cues

Sound files live here (gitignored). For each cue the player uses
`<cue>.mp3` (played with mpg123) if it exists, else `<cue>.wav` (played with
`aplay -q`). A cue with neither file is generated as a wav when the player
starts, so nothing needs to be copied in:

- `startup` - service started (rising three-note chime)
- `accept` - card recognized (short double beep)
- `error` - unknown card, failed write, or playback error (low buzz)
- `written` - a URI was written to a card (quick ascending arpeggio)
- `connect_device` - no Spotify Connect device available (two slow beeps)
- `shuffle_on` / `shuffle_off` (upward / downward glide)

`python -m vinyl sounds` regenerates all the wav files (16-bit mono,
22050 Hz, from `vinyl/tones.py`). Drop in an mp3 of the same name to
override any of them. If neither mpg123 nor aplay is installed the player
stays silent.
