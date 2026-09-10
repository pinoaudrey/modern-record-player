"""Add-to-home-screen: the web app manifest and the stdlib-drawn icon."""

import json
import struct
import zlib

from fastapi.testclient import TestClient

from vinyl.icon import render_icon
from vinyl.player import Player
from vinyl.reader import FakeReader
from vinyl.web import create_app

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def make_client(db, fake_sounds, fake_spotify):
    reader = FakeReader()
    player = Player(db, fake_spotify, reader, fake_sounds, scan_cooldown=0.0)
    return TestClient(create_app(db, fake_spotify, player, reader=reader))


def test_manifest(db, fake_sounds, fake_spotify):
    tc = make_client(db, fake_sounds, fake_spotify)
    r = tc.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    m = json.loads(r.text)
    assert m["name"] == "Record Player" and m["short_name"] == "Records"
    assert m["display"] == "standalone" and m["start_url"] == "/"
    assert m["theme_color"] == "#1db954" and m["background_color"] == "#1db954"
    assert m["icons"] == [{"src": "/icon.png", "sizes": "192x192", "type": "image/png"}]


def test_icon_is_a_192_png(db, fake_sounds, fake_spotify):
    tc = make_client(db, fake_sounds, fake_spotify)
    r = tc.get("/icon.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    data = r.content
    assert data[:8] == PNG_SIGNATURE
    length, kind = struct.unpack(">I4s", data[8:16])
    assert kind == b"IHDR" and length == 13
    width, height, depth, colour = struct.unpack(">IIBB", data[16:26])
    assert (width, height, depth, colour) == (192, 192, 8, 2)
    assert data.endswith(b"IEND" + struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF))
    assert r.content is not None and render_icon(192) == data   # cached, same bytes


def test_icon_pixels_are_a_dark_disc_on_green():
    data = render_icon(64)
    # decode the single IDAT chunk
    pos = 8
    idat = b""
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        if kind == b"IDAT":
            idat += data[pos + 8:pos + 8 + length]
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = 1 + 64 * 3

    def px(x, y):
        o = y * stride + 1 + x * 3
        return tuple(raw[o:o + 3])

    assert px(1, 1) == (0x1D, 0xB9, 0x54)        # corner: green tile
    assert px(32, 10) == (0x19, 0x14, 0x14)      # on the disc: dark
    assert px(32, 26) == (0x1D, 0xB9, 0x54)      # centre label: green


def test_base_template_links_manifest_and_icon(db, fake_sounds, fake_spotify):
    tc = make_client(db, fake_sounds, fake_spotify)
    html = tc.get("/").text
    assert '<link rel="manifest" href="/manifest.webmanifest">' in html
    assert '<link rel="apple-touch-icon" href="/icon.png">' in html
    assert '<meta name="apple-mobile-web-app-capable" content="yes">' in html
    assert '<meta name="theme-color" content="#1db954">' in html
