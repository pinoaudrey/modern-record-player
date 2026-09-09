"""RFID reader drivers. RC522 on the Pi, a queue-backed fake for development.

Cards are MIFARE Classic 1K (the white cards / blue fobs that ship with RC522
kits). Besides the UID, the player reads and writes a short text payload
(48 ASCII chars in blocks 8-10) so a card can carry its own Spotify URI: a
card written on one player works on any other one without re-registering.
"""

import queue
import time
from dataclasses import dataclass
from typing import Protocol

TAG_TEXT_MAX = 48  # 3 blocks x 16 bytes, what SimpleMFRC522 reads/writes
_POLL_INTERVAL = 0.1


@dataclass(frozen=True)
class Scan:
    uid: str
    text: str = ""


class WriteError(Exception):
    pass


class Reader(Protocol):
    def poll(self, timeout: float) -> Scan | None:
        """Wait up to `timeout` seconds for a card. None if none was presented."""
        ...

    def write(self, text: str, timeout: float) -> Scan | None:
        """Write `text` to the card on the reader (waiting up to `timeout` for
        one), verify it, and return the scan. None if no card showed up."""
        ...


def check_tag_text(text: str) -> str:
    if len(text) > TAG_TEXT_MAX:
        raise ValueError(f"tag text is {len(text)} chars, max is {TAG_TEXT_MAX}")
    try:
        text.encode("ascii")
    except UnicodeEncodeError as e:
        raise ValueError("tag text must be ASCII") from e
    return text


def _clean(text: str | None) -> str:
    # Blank cards read as NULs, written cards are space-padded to 48.
    return (text or "").strip("\x00 \r\n\t")


class RC522Reader:
    """MFRC522 over SPI. Requires rpi-lgpio + spidev + mfrc522 (see deploy/install-pi.sh).

    Uses the library's non-blocking calls with a short sleep between attempts;
    its blocking read() busy-spins a core at 100%.
    """

    def __init__(self):
        from mfrc522 import SimpleMFRC522

        self._reader = SimpleMFRC522()

    def poll(self, timeout: float) -> Scan | None:
        deadline = time.monotonic() + timeout
        while True:
            uid, text = self._reader.read_no_block()
            if uid:
                return Scan(uid=str(uid), text=_clean(text))
            if time.monotonic() >= deadline:
                return None
            time.sleep(_POLL_INTERVAL)

    def write(self, text: str, timeout: float) -> Scan | None:
        check_tag_text(text)
        deadline = time.monotonic() + timeout
        while True:
            uid, _ = self._reader.write_no_block(text)
            if uid:
                break
            if time.monotonic() >= deadline:
                return None
            time.sleep(_POLL_INTERVAL)

        # write_no_block returns the uid even when sector auth failed, so read
        # back and compare before claiming success.
        for _ in range(10):
            read_uid, read_text = self._reader.read_no_block()
            if read_uid:
                if _clean(read_text) == text:
                    return Scan(uid=str(read_uid), text=text)
                raise WriteError(
                    f"card {read_uid} did not accept the write (read back {_clean(read_text)!r}). "
                    "Is it a MIFARE Classic card with the default key?"
                )
            time.sleep(_POLL_INTERVAL)
        raise WriteError("card was removed before the write could be verified")


class FakeReader:
    """Development reader. Inject scans with .inject(uid, text) or POST /dev/scan.

    Remembers the text written to each fake card, and treats the most recently
    scanned card as still resting on the reader for a few seconds so a write
    that follows a scan lands on that card, like on the real hardware.
    """

    PRESENCE_WINDOW = 3.0

    def __init__(self):
        self._queue: queue.Queue[str] = queue.Queue()
        self._tags: dict[str, str] = {}
        self._present: tuple[str, float] | None = None

    def inject(self, uid: str, text: str | None = None) -> None:
        uid = str(uid)
        if text is not None:
            self._tags[uid] = text
        self._queue.put(uid)

    def tag_text(self, uid: str) -> str:
        return self._tags.get(str(uid), "")

    def poll(self, timeout: float) -> Scan | None:
        try:
            uid = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        self._present = (uid, time.monotonic())
        return Scan(uid=uid, text=self._tags.get(uid, ""))

    def write(self, text: str, timeout: float) -> Scan | None:
        check_tag_text(text)
        if self._present and time.monotonic() - self._present[1] < self.PRESENCE_WINDOW:
            uid = self._present[0]
        else:
            scan = self.poll(timeout)
            if scan is None:
                return None
            uid = scan.uid
        self._tags[uid] = text
        return Scan(uid=uid, text=text)


def make_reader(driver: str) -> Reader:
    if driver == "rc522":
        return RC522Reader()
    if driver == "fake":
        return FakeReader()
    raise ValueError(f"unknown reader driver: {driver}")
