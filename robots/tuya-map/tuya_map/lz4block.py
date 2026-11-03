"""LZ4 *block* format decoder (no frame header), pure Python, hardened against corrupt input.

Tuya's cloud map files (see layout.py) store the bitmap as an LZ4 block. This decoder follows the public LZ4 block specification
(https://github.com/lz4/lz4/blob/dev/doc/lz4_Block_format.md): a sequence of [token][literal length extension][literals][offset u16 LE][match length extension].
It never reads or writes outside its buffers and refuses output larger than `max_size`, so a hostile or truncated file raises `MapError` instead of
hanging or exhausting memory.
"""

from .errors import MapError

DEFAULT_MAX_SIZE = 64 * 1024 * 1024


def _extend(src, pos, length):
    """Read the 255-run extension of a length that started as 15."""
    while True:
        if pos >= len(src):
            raise MapError("truncated LZ4 data (length extension)")
        b = src[pos]
        pos += 1
        length += b
        if b != 255:
            return length, pos


def decompress(src, max_size=DEFAULT_MAX_SIZE):
    """Decode an LZ4 block. `src` is bytes-like; returns bytes. Raises MapError on any inconsistency."""
    src = bytes(src)
    out = bytearray()
    pos = 0
    n = len(src)
    if n == 0:
        raise MapError("empty LZ4 data")
    while pos < n:
        token = src[pos]
        pos += 1
        lit = token >> 4
        if lit == 15:
            lit, pos = _extend(src, pos, lit)
        if pos + lit > n:
            raise MapError("truncated LZ4 data (literals)")
        if len(out) + lit > max_size:
            raise MapError("LZ4 output exceeds the allowed size")
        out += src[pos:pos + lit]
        pos += lit
        if pos >= n:                      # the last sequence has literals only
            break
        if pos + 2 > n:
            raise MapError("truncated LZ4 data (offset)")
        offset = src[pos] | (src[pos + 1] << 8)
        pos += 2
        if offset == 0 or offset > len(out):
            raise MapError("invalid LZ4 match offset")
        mlen = token & 15
        if mlen == 15:
            mlen, pos = _extend(src, pos, mlen)
        mlen += 4
        if len(out) + mlen > max_size:
            raise MapError("LZ4 output exceeds the allowed size")
        start = len(out) - offset
        if offset >= mlen:
            out += out[start:start + mlen]
        else:                              # overlapping copy repeats the pattern
            for i in range(mlen):
                out.append(out[start + i])
    return bytes(out)
