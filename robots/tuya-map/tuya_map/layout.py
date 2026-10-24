"""Parser for the Tuya cloud *layout* file (the walls/floor/rooms bitmap of a robot-vacuum map).

FORMAT STATUS: reconstructed from the open-source `tuya-vacuum` library (MIT, github.com/jaidenlabelle/tuya-vacuum) and Tuya's laser-robot documentation.
It is **not verified on the ETA Tiger**; no real file of that robot has been seen. All numbers below are big-endian.

    byte 0        version (0, 1 or 152 supported; anything else is refused)
    bytes 1-2     map id
    byte 3        type
    bytes 4-5     width          bytes 6-7   height
    bytes 8-9     origin x * 10  bytes 10-11 origin y * 10 (charging dock / path origin, in map cells)
    bytes 12-13   resolution     bytes 14-15 pile x * 10    bytes 16-17 pile y * 10
    v0 / v1:      bytes 18-21 total count (u32), bytes 22-23 compressed length (u16), LZ4 block from byte 24
    v152:         LZ4 block from byte 18

    v1   decompressed = width*height pixel bytes (row-major, row 0 on top) + a room table
    v152 decompressed = width*height pixel bytes, no rooms
    v0   decompressed = packed 2-bit pixels (four per byte, most significant pair first -> 00, F1, F2, FF); the bit order is a guess

Pixel byte meaning (v1 palette of the reference library): 4k = floor of room k, 4k+1 / 4k+3 = wall of room k, 240 = virtual zone, 241/248-253 = wall,
243/255 = background. Other values are reported as "unknown" and drawn dark.
"""

from dataclasses import dataclass, field

from . import lz4block
from .errors import MapError

MAX_PIXELS = 16 * 1024 * 1024
HEADER_V01 = 24
HEADER_V152 = 18
ROOM_RECORD = 47
ROOM_NAME_MAX = 20


@dataclass
class Room:
    room_id: int
    order: int
    sweep_count: int
    mop_count: int
    name: str


@dataclass
class Layout:
    version: int
    map_id: int
    kind: int
    width: int
    height: int
    origin_x: float
    origin_y: float
    resolution: int
    pile_x: float
    pile_y: float
    pixels: bytes = field(repr=False)
    rooms: list = field(default_factory=list)


def _u16(b, i):
    return (b[i] << 8) | b[i + 1]


def _u32(b, i):
    return (b[i] << 24) | (b[i + 1] << 16) | (b[i + 2] << 8) | b[i + 3]


def _parse_rooms(extra):
    """The room table after the bitmap: [ignored byte][count][count records of 47 bytes]."""
    if len(extra) < 2:
        return []
    count = extra[1]
    rooms = []
    pos = 2
    for _ in range(count):
        rec = extra[pos:pos + ROOM_RECORD]
        if len(rec) < ROOM_RECORD:
            raise MapError("truncated room table")
        name_len = rec[26]
        if name_len > ROOM_NAME_MAX:
            raise MapError("implausible room name length")
        name = rec[27:27 + name_len].decode("utf-8", errors="replace")
        rooms.append(Room(_u16(rec, 0), _u16(rec, 2), _u16(rec, 4), _u16(rec, 6), name))
        pos += ROOM_RECORD
    return rooms


_V0_CODES = (0x00, 0xF1, 0xF2, 0xFF)


def _unpack_v0(packed, count):
    out = bytearray()
    for b in packed:
        out.append(_V0_CODES[(b >> 6) & 3])
        out.append(_V0_CODES[(b >> 4) & 3])
        out.append(_V0_CODES[(b >> 2) & 3])
        out.append(_V0_CODES[b & 3])
    if len(out) < count:
        raise MapError("layout bitmap is shorter than width*height")
    return bytes(out[:count])


def parse_layout(data):
    """Parse a layout file (bytes-like). Raises MapError for anything that is not a plausible, complete layout."""
    data = bytes(data)
    if len(data) < HEADER_V152:
        raise MapError("layout too short for a header")
    version = data[0]
    if version not in (0, 1, 152):
        raise MapError("unsupported layout version %d" % version)
    width, height = _u16(data, 4), _u16(data, 6)
    if width == 0 or height == 0 or width * height > MAX_PIXELS:
        raise MapError("implausible layout size %dx%d" % (width, height))
    count = width * height
    head = dict(map_id=_u16(data, 1), kind=data[3], width=width, height=height, origin_x=_u16(data, 8) / 10.0, origin_y=_u16(data, 10) / 10.0,
                resolution=_u16(data, 12), pile_x=_u16(data, 14) / 10.0, pile_y=_u16(data, 16) / 10.0)
    rooms = []
    if version == 152:
        pixels = lz4block.decompress(data[HEADER_V152:])
        if len(pixels) < count:
            raise MapError("layout bitmap is shorter than width*height")
        pixels = pixels[:count]
    else:
        if len(data) < HEADER_V01:
            raise MapError("layout too short for a header")
        clen = _u16(data, 22)
        if clen == 0:
            raise MapError("uncompressed layouts are not supported")
        body = data[HEADER_V01:HEADER_V01 + clen]
        if len(body) < clen:
            raise MapError("layout body is shorter than its declared compressed length")
        raw = lz4block.decompress(body)
        if version == 0:
            pixels = _unpack_v0(raw, count)
        else:
            if len(raw) < count:
                raise MapError("layout bitmap is shorter than width*height")
            pixels, rooms = raw[:count], _parse_rooms(raw[count:])
    return Layout(version=version, pixels=pixels, rooms=rooms, **head)


def classify(value):
    """-> ('floor', room_index) | ('wall', None) | ('zone', None) | ('bg', None) | ('unknown', None) for one pixel byte."""
    if value < 240:
        k, r = divmod(value, 4)
        return ("floor", k) if r == 0 else (("wall", None) if r in (1, 3) else ("unknown", None))
    if value == 240:
        return ("zone", None)
    if value in (241, 248, 249, 251, 252, 253):
        return ("wall", None)
    if value in (243, 255):
        return ("bg", None)
    return ("unknown", None)
