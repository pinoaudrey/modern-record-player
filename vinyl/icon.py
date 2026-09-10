"""Home-screen icon for the admin, drawn with the standard library.

A dark record on the admin's green, with a green centre label. Rendered
once per size and cached; no image library on the Pi needed.
"""

import struct
import zlib
from functools import lru_cache

GREEN = (0x1D, 0xB9, 0x54)      # the #1db954 in base.html
DARK = (0x19, 0x14, 0x14)
GROOVE = (0x2A, 0x25, 0x25)
HOLE = (0x0B, 0x0B, 0x0B)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload)) + kind + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _mix(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _edge(dist: float, radius: float) -> float:
    """1 inside the circle, 0 outside, a one-pixel soft edge between."""
    return min(1.0, max(0.0, radius + 0.5 - dist))


@lru_cache(maxsize=4)
def render_icon(size: int = 192) -> bytes:
    """A `size` x `size` RGB PNG of a record on a green tile."""
    centre = (size - 1) / 2
    disc_r = size * 0.44
    label_r = size * 0.17
    hole_r = size * 0.025
    grooves = [disc_r * f for f in (0.92, 0.82, 0.72, 0.62, 0.52)]
    groove_w = max(1.0, size / 192)

    rows = []
    for y in range(size):
        row = bytearray([0])            # filter type 0 (none) for this scanline
        for x in range(size):
            d = ((x - centre) ** 2 + (y - centre) ** 2) ** 0.5
            colour = GREEN
            colour = _mix(colour, DARK, _edge(d, disc_r))
            for g in grooves:
                if abs(d - g) < groove_w:
                    colour = _mix(colour, GROOVE, 1.0 - abs(d - g) / groove_w)
            colour = _mix(colour, GREEN, _edge(d, label_r))
            colour = _mix(colour, HOLE, _edge(d, hole_r))
            row += bytes(colour)
        rows.append(bytes(row))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)   # 8-bit RGB
    return b"".join([
        b"\x89PNG\r\n\x1a\n",
        _chunk(b"IHDR", ihdr),
        _chunk(b"IDAT", zlib.compress(b"".join(rows), 9)),
        _chunk(b"IEND", b""),
    ])
