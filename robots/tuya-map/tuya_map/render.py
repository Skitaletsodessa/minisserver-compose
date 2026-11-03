"""Render a Layout (and optionally a Path) to a PNG using only the standard library (zlib + struct)."""

import struct
import zlib

from .errors import MapError
from .layout import classify

ROOM_COLOURS = [(249, 66, 79), (253, 208, 43), (70, 168, 144), (91, 140, 235), (200, 120, 220), (240, 150, 80), (120, 200, 90), (90, 200, 220),
                (230, 110, 150), (160, 160, 90), (110, 110, 200), (200, 90, 90), (90, 160, 120), (220, 190, 120), (150, 120, 190), (120, 190, 190)]
WALL = (50, 50, 50, 255)
ZONE = (200, 200, 200, 255)
UNKNOWN = (20, 20, 20, 255)
BACKGROUND = (0, 110, 230, 255)
PATH_COLOUR = (255, 255, 255, 255)
DOCK_COLOUR = (40, 190, 80, 255)
ROBOT_COLOUR = (30, 90, 255, 255)
MAX_OUTPUT_PIXELS = 16 * 1024 * 1024


def _palette(transparent):
    bg = (0, 0, 0, 0) if transparent else BACKGROUND
    table = []
    for v in range(256):
        kind, k = classify(v)
        if kind == "floor":
            r, g, b = ROOM_COLOURS[k % len(ROOM_COLOURS)]
            table.append((r, g, b, 255))
        elif kind == "wall":
            table.append(WALL)
        elif kind == "zone":
            table.append(ZONE)
        elif kind == "bg":
            table.append(bg)
        else:
            table.append(UNKNOWN)
    return table


def _put(buf, w, h, x, y, rgba):
    if 0 <= x < w and 0 <= y < h:
        i = (y * w + x) * 4
        buf[i:i + 4] = bytes(rgba)


def _disc(buf, w, h, cx, cy, r, rgba):
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy <= r * r:
                _put(buf, w, h, cx + dx, cy + dy, rgba)


def _line(buf, w, h, x0, y0, x1, y1, thick, rgba):
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    while True:
        _disc(buf, w, h, x0, y0, thick, rgba)
        if x0 == x1 and y0 == y1:
            return
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def _png(width, height, rgba):
    def chunk(tag, payload):
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    row = width * 4
    raw = b"".join(b"\x00" + bytes(rgba[y * row:(y + 1) * row]) for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def render_png(layout, path=None, scale=4, transparent=False):
    """Return PNG bytes. `scale` = output pixels per map cell. The dock is drawn at the layout origin, the robot at the last path point."""
    scale = int(scale)
    if scale < 1 or layout.width * scale * layout.height * scale > MAX_OUTPUT_PIXELS:
        raise MapError("output size out of range")
    w, h = layout.width * scale, layout.height * scale
    pal = _palette(transparent)
    buf = bytearray(w * h * 4)
    px = layout.pixels
    for y in range(layout.height):
        row = bytearray()
        for x in range(layout.width):
            row += bytes(pal[px[y * layout.width + x]]) * scale
        base = y * scale
        for k in range(scale):
            buf[(base + k) * w * 4:(base + k + 1) * w * 4] = row
    thick = max(1, scale // 2)

    def to_px(mx, my):
        return int(round((mx + layout.origin_x) * scale)), int(round((my + layout.origin_y) * scale))

    if path is not None and path.points:
        pts = [to_px(x, y) for x, y in path.points]
        for a, b in zip(pts, pts[1:]):
            _line(buf, w, h, a[0], a[1], b[0], b[1], thick, PATH_COLOUR)
        _disc(buf, w, h, pts[-1][0], pts[-1][1], thick * 3, ROBOT_COLOUR)
    _disc(buf, w, h, int(round(layout.origin_x * scale)), int(round(layout.origin_y * scale)), thick * 3, DOCK_COLOUR)
    return _png(w, h, buf)
