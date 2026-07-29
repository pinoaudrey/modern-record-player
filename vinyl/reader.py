"""RFID reader drivers. RC522 on the Pi, a queue-backed fake for development."""

import queue
from typing import Protocol


class Reader(Protocol):
    def read(self) -> str:
        """Block until a card is scanned, return its UID as a string."""
        ...


class RC522Reader:
    """MFRC522 over SPI. Requires rpi-lgpio + spidev + mfrc522 (see requirements-pi.txt)."""

    def __init__(self):
        from mfrc522 import SimpleMFRC522

        self._reader = SimpleMFRC522()

    def read(self) -> str:
        uid, _text = self._reader.read()
        return str(uid)


class FakeReader:
    """Development reader. Inject scans with .inject(uid) or via POST /dev/scan."""

    def __init__(self):
        self._queue: queue.Queue[str] = queue.Queue()

    def inject(self, uid: str) -> None:
        self._queue.put(str(uid))

    def read(self) -> str:
        return self._queue.get()


def make_reader(driver: str) -> Reader:
    if driver == "rc522":
        return RC522Reader()
    if driver == "fake":
        return FakeReader()
    raise ValueError(f"unknown reader driver: {driver}")
