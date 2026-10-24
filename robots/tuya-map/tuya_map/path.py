"""Parser for the Tuya cloud *path* file (where the robot has driven).

FORMAT STATUS: reconstructed from the open-source `tuya-vacuum` library (MIT) and Tuya's laser-robot documentation (path coordinates are INT16 x 10).
Not verified on the ETA Tiger. Big-endian.

    standard header 13 bytes: version, id (u16), force-update, type, total count (u32), theta (u16), compressed length (u16)
    version 12 header 8 bytes (points start at byte 8, never compressed)
    body: points of 4 bytes = x (i16), y (i16); the value 32769..65535 is read as negative; both divided by 10
    coordinates are offsets from the layout origin (the charging dock); y is mirrored for every version except 12
"""

from dataclasses import dataclass, field

from . import lz4block
from .errors import MapError

HEADER = 13
HEADER_V12 = 8
MAX_POINTS = 4 * 1024 * 1024


@dataclass
class Path:
    version: int
    map_id: int
    points: list = field(default_factory=list)      # [(x, y)] floats, map cells relative to the layout origin


def _u16(b, i):
    return (b[i] << 8) | b[i + 1]


def _signed(v):
    return v - 65536 if v > 32768 else v


def parse_path(data):
    """Parse a path file (bytes-like). Raises MapError for anything truncated or implausible."""
    data = bytes(data)
    if len(data) < HEADER_V12:
        raise MapError("path too short for a header")
    version = data[0]
    map_id = _u16(data, 1)
    if version == 12:
        body, mirror = data[HEADER_V12:], False
    else:
        if len(data) < HEADER:
            raise MapError("path too short for a header")
        clen = _u16(data, 11)
        raw = data[HEADER:]
        if clen:
            if len(raw) < clen:
                raise MapError("path body is shorter than its declared compressed length")
            raw = lz4block.decompress(raw[:clen])
        body, mirror = raw, True
    if len(body) % 4:
        raise MapError("path body is not a whole number of points")
    if len(body) // 4 > MAX_POINTS:
        raise MapError("implausible number of path points")
    points = []
    for i in range(0, len(body), 4):
        x = _signed(_u16(body, i)) / 10.0
        y = _signed(_u16(body, i + 2)) / 10.0
        y = -y if mirror else y
        points.append((round(x, 1) + 0.0, round(y, 1) + 0.0))
    return Path(version=version, map_id=map_id, points=points)
