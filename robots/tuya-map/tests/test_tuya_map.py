import os
import random
import struct
import sys
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import build  # noqa: E402
from tuya_map import MapError, classify, lz4block, parse_layout, parse_path, render_png  # noqa: E402


class Lz4(unittest.TestCase):
    def test_known_vector_literals_only(self):
        self.assertEqual(lz4block.decompress(bytes([0x50]) + b"hello"), b"hello")

    def test_known_vector_with_overlapping_match(self):
        # literal 'a', then a match of length 8 at offset 1 -> 'aaaaaaaaa'
        src = bytes([0x14]) + b"a" + struct.pack("<H", 1) + bytes([0x10]) + b"b"
        self.assertEqual(lz4block.decompress(src), b"a" + b"a" * 8 + b"b")

    def test_roundtrip_many_shapes(self):
        rnd = random.Random(7)
        cases = [b"x", b"abc", bytes(1000), b"abcd" * 500, bytes(rnd.randrange(256) for _ in range(3000)), (b"0123456789" * 40 + bytes(300)) * 5]
        for c in cases:
            self.assertEqual(lz4block.decompress(build.lz4_compress(c)), c)

    def test_hostile_inputs_raise_maperror_only(self):
        for bad in (b"", bytes([0x10]), bytes([0x20, 0x61]), bytes([0x14, 0x61, 0, 0]), bytes([0x14, 0x61, 5, 0, 0]), bytes([0xF0]) + b"\xff" * 10, bytes([0x1F, 0x61, 1, 0])):
            with self.assertRaises(MapError):
                lz4block.decompress(bad)

    def test_decompression_bomb_is_refused(self):
        bomb = bytes([0x1F, 0x61, 1, 0]) + b"\xff" * 5000 + b"\x00"
        with self.assertRaises(MapError):
            lz4block.decompress(bomb, max_size=100000)


class Layout(unittest.TestCase):
    def test_v1_roundtrip_with_rooms(self):
        data, px = build.layout_v1(rooms=(("5", "Kitchen"), ("6", "Хол")))
        lay = parse_layout(data)
        self.assertEqual((lay.version, lay.map_id, lay.width, lay.height), (1, 7, 40, 30))
        self.assertEqual((lay.origin_x, lay.origin_y, lay.pile_x, lay.pile_y), (20.0, 15.0, 20.2, 15.0))
        self.assertEqual(lay.pixels, px)
        self.assertEqual([(r.room_id, r.name) for r in lay.rooms], [(5, "Kitchen"), (6, "Хол")])

    def test_v152_and_v0(self):
        data, px = build.layout_v152()
        lay = parse_layout(data)
        self.assertEqual((lay.version, lay.pixels, lay.rooms), (152, px, []))
        data, expect = build.layout_v0()
        lay = parse_layout(data)
        self.assertEqual((lay.version, lay.pixels), (0, expect))

    def test_pixel_classes(self):
        self.assertEqual(classify(0), ("floor", 0))
        self.assertEqual(classify(8), ("floor", 2))
        self.assertEqual(classify(1)[0], "wall")
        self.assertEqual(classify(3)[0], "wall")
        self.assertEqual(classify(2)[0], "unknown")
        self.assertEqual(classify(255)[0], "bg")
        self.assertEqual(classify(240)[0], "zone")
        self.assertEqual(classify(250)[0], "unknown")

    def test_every_truncation_is_a_clean_error_or_a_valid_prefix_never_a_crash(self):
        for make in (build.layout_v1, build.layout_v152, build.layout_v0):
            data = make()[0]
            for cut in range(len(data)):
                try:
                    parse_layout(data[:cut])
                except MapError:
                    pass                      # the only allowed failure

    def test_header_validation(self):
        data, _ = build.layout_v1()
        for patch in ((0, 2), (0, 99), (4, 0), (6, 0)):
            bad = bytearray(data)
            if patch[0] == 0:
                bad[0] = patch[1]
            else:
                bad[patch[0]:patch[0] + 2] = b"\x00\x00"
            with self.assertRaises(MapError):
                parse_layout(bytes(bad))
        huge = bytearray(data)
        huge[4:8] = struct.pack(">HH", 60000, 60000)
        with self.assertRaises(MapError):
            parse_layout(bytes(huge))
        with self.assertRaises(MapError):
            parse_layout(b"")

    def test_random_corruption_never_raises_anything_but_maperror(self):
        rnd = random.Random(1234)
        base = [build.layout_v1()[0], build.layout_v152()[0], build.layout_v0()[0]]
        for _ in range(600):
            data = bytearray(rnd.choice(base))
            for _ in range(rnd.randrange(1, 6)):
                data[rnd.randrange(len(data))] = rnd.randrange(256)
            try:
                parse_layout(bytes(data))
            except MapError:
                pass


class PathFiles(unittest.TestCase):
    PTS = [(0.0, 0.0), (1.5, -2.5), (10.0, 4.2), (-3.3, 7.7)]

    def test_standard_compressed_and_raw(self):
        for comp in (True, False):
            p = parse_path(build.path_standard(self.PTS, compress=comp))
            self.assertEqual(p.points, self.PTS)

    def test_v12(self):
        p = parse_path(build.path_v12(self.PTS))
        self.assertEqual((p.version, p.points), (12, self.PTS))

    def test_truncation_and_corruption(self):
        for make in (lambda: build.path_standard(self.PTS), lambda: build.path_v12(self.PTS)):
            data = make()
            for cut in range(len(data)):
                try:
                    parse_path(data[:cut])
                except MapError:
                    pass
        rnd = random.Random(99)
        data = build.path_standard(self.PTS * 20)
        for _ in range(300):
            bad = bytearray(data)
            bad[rnd.randrange(len(bad))] = rnd.randrange(256)
            try:
                parse_path(bytes(bad))
            except MapError:
                pass
        with self.assertRaises(MapError):
            parse_path(build.path_v12(self.PTS)[:-1])


class Render(unittest.TestCase):
    @staticmethod
    def decode(png):
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        pos, idat, hdr = 8, b"", None
        while pos < len(png):
            ln = struct.unpack(">I", png[pos:pos + 4])[0]
            tag = png[pos + 4:pos + 8]
            body = png[pos + 8:pos + 8 + ln]
            assert struct.unpack(">I", png[pos + 8 + ln:pos + 12 + ln])[0] == zlib.crc32(tag + body) & 0xFFFFFFFF
            if tag == b"IHDR":
                hdr = struct.unpack(">IIBBBBB", body)
            elif tag == b"IDAT":
                idat += body
            pos += 12 + ln
        w, h = hdr[0], hdr[1]
        raw = zlib.decompress(idat)
        rows = [raw[y * (w * 4 + 1) + 1:(y + 1) * (w * 4 + 1)] for y in range(h)]
        return w, h, rows

    def test_png_structure_and_colours(self):
        lay = parse_layout(build.layout_v1()[0])
        pth = parse_path(build.path_standard([(0.0, 0.0), (5.0, 0.0), (5.0, 5.0)]))
        w, h, rows = self.decode(render_png(lay, pth, scale=4))
        self.assertEqual((w, h), (160, 120))
        px = lambda x, y: tuple(rows[y][x * 4:x * 4 + 4])
        self.assertEqual(px(0, 0), (0, 110, 230, 255))                      # background
        self.assertEqual(px(8, 8)[3], 255)                                   # wall frame is opaque
        self.assertEqual(px(30, 30)[:3], (249, 66, 79))                      # floor of room 0
        self.assertEqual(px(120, 100)[:3], (253, 208, 43))                   # floor of room 1
        self.assertEqual(px(20 * 4, 15 * 4)[:3], (40, 190, 80))              # dock at the layout origin
        self.assertEqual(px(25 * 4, 20 * 4)[:3], (30, 90, 255))              # robot at the last path point

    def test_transparent_background(self):
        lay = parse_layout(build.layout_v152()[0])
        w, h, rows = self.decode(render_png(lay, None, scale=2, transparent=True))
        self.assertEqual(tuple(rows[0][0:4]), (0, 0, 0, 0))

    def test_output_size_is_bounded(self):
        lay = parse_layout(build.layout_v152()[0])
        with self.assertRaises(MapError):
            render_png(lay, None, scale=0)
        with self.assertRaises(MapError):
            render_png(lay, None, scale=100000)

    def test_path_off_the_map_does_not_crash(self):
        lay = parse_layout(build.layout_v152()[0])
        pth = parse_path(build.path_standard([(-500.0, -500.0), (900.0, 900.0)]))
        render_png(lay, pth, scale=1)


if __name__ == "__main__":
    unittest.main()
