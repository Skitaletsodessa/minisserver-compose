"""Builders for SYNTHETIC map files (tests only): an LZ4 block encoder and writers for the layout / path formats of tuya_map.
Nothing here comes from a real robot; the maps are invented rooms in a 40x30 grid."""

import struct


def lz4_compress(data):
    """Simple greedy LZ4 block encoder (correct per the block spec, not optimal)."""
    data = bytes(data)
    n = len(data)
    out = bytearray()
    table = {}
    i = anchor = 0
    limit = n - 5                                   # the spec keeps the last 5 bytes as literals
    while i + 4 <= limit:
        key = data[i:i + 4]
        cand = table.get(key)
        table[key] = i
        if cand is not None and i - cand <= 65535:
            m = 4
            while i + m < n - 5 and data[cand + m] == data[i + m]:
                m += 1
            lit = data[anchor:i]
            _emit(out, lit, i - cand, m)
            i += m
            anchor = i
        else:
            i += 1
    _emit(out, data[anchor:], None, None)
    return bytes(out)


def _len_ext(v):
    out = bytearray()
    while v >= 255:
        out.append(255)
        v -= 255
    out.append(v)
    return bytes(out)


def _emit(out, lit, offset, mlen):
    ll = len(lit)
    token = (min(ll, 15) << 4)
    if offset is not None:
        token |= min(mlen - 4, 15)
    out.append(token)
    if ll >= 15:
        out += _len_ext(ll - 15)
    out += lit
    if offset is not None:
        out += struct.pack("<H", offset)
        if mlen - 4 >= 15:
            out += _len_ext(mlen - 4 - 15)


def synthetic_pixels(w=40, h=30):
    """Two rooms (ids 0 and 1) separated by a wall, a wall frame, background outside; one virtual-zone pixel."""
    px = bytearray([255] * (w * h))
    for y in range(2, h - 2):
        for x in range(2, w - 2):
            room = 0 if x < w // 2 else 1
            px[y * w + x] = room * 4                # floor of room k = 4k
    for x in range(2, w - 2):
        px[2 * w + x] = px[(h - 3) * w + x] = 1     # wall of room 0
    for y in range(2, h - 2):
        px[y * w + 2] = px[y * w + (w - 3)] = 1
    for y in range(2, h - 2):
        px[y * w + w // 2] = 241                    # inner wall
    px[5 * w + 5] = 240
    return bytes(px)


def room_table(rooms):
    out = bytearray([0, len(rooms)])
    for rid, name in rooms:
        rec = bytearray(47)
        struct.pack_into(">HHHH", rec, 0, rid, 65535, 1, 2)
        nm = name.encode("utf-8")
        rec[26] = len(nm)
        rec[27:27 + len(nm)] = nm
        out += rec
    return bytes(out)


def layout_v1(w=40, h=30, origin=(20.0, 15.0), rooms=(("5", "A"),)):
    px = synthetic_pixels(w, h)
    raw = px + room_table([(int(r), n) for r, n in rooms])
    comp = lz4_compress(raw)
    head = bytes([1]) + struct.pack(">H", 7) + bytes([0]) + struct.pack(">HHHHHHH", w, h, int(origin[0] * 10), int(origin[1] * 10), 0, 202, 150)
    head += struct.pack(">IH", len(raw), len(comp))
    return head + comp, px


def layout_v152(w=40, h=30, origin=(20.0, 15.0)):
    px = synthetic_pixels(w, h)
    comp = lz4_compress(px)
    head = bytes([152]) + struct.pack(">H", 9) + bytes([0]) + struct.pack(">HHHHHHH", w, h, int(origin[0] * 10), int(origin[1] * 10), 0, 100, 100)
    return head + comp, px


def layout_v0(w=40, h=30, origin=(20.0, 15.0)):
    """Packed 2-bit pixels: 0 -> 00, 1 -> F1, 2 -> F2, 3 -> FF (most significant pair first)."""
    codes = bytearray()
    expect = bytearray()
    cells = [(x * 7 + y * 3) % 4 for y in range(h) for x in range(w)]
    cells += [0] * ((-len(cells)) % 4)
    for i in range(0, len(cells), 4):
        a, b, c, d = cells[i:i + 4]
        codes.append((a << 6) | (b << 4) | (c << 2) | d)
    table = (0x00, 0xF1, 0xF2, 0xFF)
    expect = bytes(table[v] for v in cells[:w * h])
    comp = lz4_compress(bytes(codes))
    head = bytes([0]) + struct.pack(">H", 1) + bytes([0]) + struct.pack(">HHHHHHH", w, h, int(origin[0] * 10), int(origin[1] * 10), 0, 0, 0)
    head += struct.pack(">IH", len(codes), len(comp))
    return head + comp, expect


def _pt(v):
    return struct.pack(">H", int(round(v * 10)) & 0xFFFF)


def path_standard(points, compress=True):
    """Standard path file: points are (x, y) in cells; stored y is mirrored as the reader mirrors it back."""
    body = b"".join(_pt(x) + _pt(-y) for x, y in points)
    comp = lz4_compress(body) if compress else body
    head = bytes([1]) + struct.pack(">H", 7) + bytes([0, 1]) + struct.pack(">IHH", len(points), 0, len(comp) if compress else 0)
    return head + comp


def path_v12(points):
    body = b"".join(_pt(x) + _pt(y) for x, y in points)
    return bytes([12]) + struct.pack(">H", 7) + bytes(5) + body
