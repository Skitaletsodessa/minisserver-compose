import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dp_logger  # noqa: E402
import redact  # noqa: E402


class T(unittest.TestCase):
    def test_changes_only_and_full_dump(self):
        r = dp_logger.Recorder(now=lambda: "T")
        a = r.feed({"1": True, "8": 90, "5": "standby"}, "full")
        self.assertEqual([x["dp"] for x in a], ["1", "5", "8"])
        self.assertEqual(r.feed({"8": 90}), [])
        b = r.feed({"8": 89, "5": "smart"})
        self.assertEqual([(x["dp"], x["value"], x["was"]["value"]) for x in b], [("5", "smart", "standby"), ("8", 89, 90)])
        self.assertEqual(len(r.feed({"8": 89}, "full")), 1)

    def test_numeric_dp_order_and_long_values(self):
        r = dp_logger.Recorder(now=lambda: "T")
        out = r.feed({"101": 1, "9": "x", "15": "A" * 200})
        self.assertEqual([x["dp"] for x in out], ["9", "15", "101"])
        big = [x for x in out if x["dp"] == "15"][0]
        self.assertEqual((big["type"], big["len"], len(big["head"])), ("str", 200, 64))

    def test_file_is_private_and_appended(self):
        with tempfile.TemporaryDirectory() as d:
            dp_logger.append([{"t": "T", "dp": "1", "value": True}], d)
            dp_logger.append([{"t": "T2", "dp": "2", "value": False}], d)
            f = [os.path.join(d, n) for n in os.listdir(d)][0]
            self.assertEqual(oct(os.stat(f).st_mode & 0o777), "0o600")
            self.assertEqual(len(open(f).read().splitlines()), 2)

    def test_env_validation(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("DEVICE_ID=x\nLOCAL_KEY=y\n")
        with self.assertRaises(SystemExit):
            dp_logger.load_env(f.name)
        os.unlink(f.name)

    def test_redaction(self):
        rec = {"t": "T", "dp": "15", "type": "str", "len": 40, "head": "0123456789abcdef" * 4, "was": {"type": "str", "head": "z" * 40}}
        out = redact.redact(rec)
        self.assertTrue(out["head"].startswith("0123456789abcdef...(64 chars)"))
        self.assertTrue(out["was"]["head"].endswith("(40 chars)"))
        small = {"t": "T", "dp": "8", "type": "int", "value": 88}
        self.assertEqual(redact.redact(small), small)


if __name__ == "__main__":
    unittest.main()
