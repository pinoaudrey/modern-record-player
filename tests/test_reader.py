import sys
import types

import pytest

from vinyl.reader import FakeReader, RC522Reader, Scan, WriteError, check_tag_text

URI = "spotify:playlist:66r7S3FuK7h9a930TOQSYH"


# --- fake reader -------------------------------------------------------------

def test_fake_poll_returns_none_on_timeout():
    assert FakeReader().poll(0.01) is None


def test_fake_inject_with_text_is_remembered():
    r = FakeReader()
    r.inject("1", URI)
    assert r.poll(0.1) == Scan(uid="1", text=URI)
    r.inject("1")  # second tap, no text given: card still holds it
    assert r.poll(0.1).text == URI


def test_fake_write_lands_on_card_just_scanned():
    r = FakeReader()
    r.inject("7")
    r.poll(0.1)
    assert r.write(URI, timeout=0.1) == Scan(uid="7", text=URI)
    assert r.tag_text("7") == URI


def test_fake_write_waits_for_a_card_when_none_present():
    r = FakeReader()
    assert r.write(URI, timeout=0.01) is None
    r.inject("8")
    assert r.write(URI, timeout=0.1).uid == "8"


def test_tag_text_limits():
    assert check_tag_text(URI) == URI
    with pytest.raises(ValueError):
        check_tag_text("x" * 49)
    with pytest.raises(ValueError):
        check_tag_text("spotify:album:café")


# --- RC522 driver against a stub of the mfrc522 library ---------------------

class StubSimpleMFRC522:
    """Mimics SimpleMFRC522: 48-char space-padded text, uid even on auth failure."""

    card = None          # (uid, text) or None
    accept_writes = True

    def read_no_block(self):
        if self.card is None:
            return None, None
        uid, text = self.card
        return uid, text.ljust(48)

    def write_no_block(self, text):
        if self.card is None:
            return None, None
        uid, _ = self.card
        if self.accept_writes:
            type(self).card = (uid, text)
        return uid, text[:48]


class StubMFRC522:
    """The low-level driver; records the reset pin it was built with."""

    instances: list = []
    version = 0x92       # what Read_MFRC522(0x37) returns; None makes the read raise

    def __init__(self, bus=0, device=0, spd=1000000, pin_mode=10, pin_rst=-1, debugLevel="WARNING"):
        self.pin_rst = pin_rst
        StubMFRC522.instances.append(self)

    def Read_MFRC522(self, addr):
        if type(self).version is None:
            raise OSError("SPI not available")
        return type(self).version if addr == 0x37 else 0


def _fake_mfrc522_module(monkeypatch):
    StubSimpleMFRC522.card = None
    StubSimpleMFRC522.accept_writes = True
    StubMFRC522.instances = []
    StubMFRC522.version = 0x92
    monkeypatch.setitem(
        sys.modules, "mfrc522",
        types.SimpleNamespace(SimpleMFRC522=StubSimpleMFRC522, MFRC522=StubMFRC522),
    )
    monkeypatch.setattr("vinyl.reader._POLL_INTERVAL", 0.001)


@pytest.fixture
def rc522(monkeypatch):
    _fake_mfrc522_module(monkeypatch)
    return RC522Reader()


def test_rc522_silences_library_auth_errors(monkeypatch):
    import logging
    _fake_mfrc522_module(monkeypatch)
    RC522Reader()
    lib_log = logging.getLogger("mfrc522Logger")
    assert lib_log.propagate is False and lib_log.level == logging.CRITICAL


def test_rc522_reset_pin_is_configurable(monkeypatch):
    _fake_mfrc522_module(monkeypatch)
    RC522Reader()
    RC522Reader(rst_pin=16)
    assert [r.pin_rst for r in StubMFRC522.instances] == [22, 16]


def test_rc522_reads_chip_version_once_and_reports_it(monkeypatch):
    _fake_mfrc522_module(monkeypatch)
    r = RC522Reader()
    assert r.chip_version == 0x92
    assert r.status() == {"driver": "rc522", "chip_version": 0x92, "chip": "MFRC522 v2.0 (0x92)", "ok": True}

    StubMFRC522.version = 0x00       # nothing answering on the bus
    st = RC522Reader().status()
    assert st["ok"] is False and "not responding" in st["chip"]

    StubMFRC522.version = 0xB2       # a clone
    st = RC522Reader().status()
    assert st["ok"] is True and "clone" in st["chip"]

    StubMFRC522.version = None       # register read blows up: still constructs
    st = RC522Reader().status()
    assert st["chip_version"] is None and st["ok"] is False


def test_fake_reader_status():
    assert FakeReader().status() == {"driver": "fake", "ok": True}


def test_rc522_poll_strips_padding_and_nulls(rc522):
    assert rc522.poll(0.01) is None
    StubSimpleMFRC522.card = (123456789, "\x00" * 48)
    assert rc522.poll(0.01) == Scan(uid="123456789", text="")
    StubSimpleMFRC522.card = (123456789, URI)
    assert rc522.poll(0.01).text == URI


def test_rc522_write_verifies_by_reading_back(rc522):
    StubSimpleMFRC522.card = (42, "")
    assert rc522.write(URI, timeout=0.01) == Scan(uid="42", text=URI)


def test_rc522_write_detects_rejected_write(rc522):
    StubSimpleMFRC522.card = (42, "")
    StubSimpleMFRC522.accept_writes = False
    with pytest.raises(WriteError):
        rc522.write(URI, timeout=0.01)


def test_rc522_write_times_out_without_card(rc522):
    assert rc522.write(URI, timeout=0.01) is None


def test_fake_hold_returns_card_on_every_poll_until_released():
    r = FakeReader()
    r.hold("9", URI)
    assert r.held == "9"
    assert r.poll(0.5) == Scan(uid="9", text=URI)
    assert r.poll(0.5) == Scan(uid="9", text=URI)
    r.release()
    assert r.held is None
    assert r.poll(0.01) is None


def test_fake_tap_wins_over_held_card():
    r = FakeReader()
    r.hold("9")
    r.inject("10")
    assert r.poll(0.1).uid == "10"
    assert r.poll(0.1).uid == "9"


def test_fake_write_lands_on_held_card():
    r = FakeReader()
    r.hold("9")
    r.poll(0.1)
    assert r.write(URI, timeout=0.1) == Scan(uid="9", text=URI)
    assert r.poll(0.1).text == URI
