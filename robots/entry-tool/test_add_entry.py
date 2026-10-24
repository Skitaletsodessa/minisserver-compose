import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import add_tuya_local_entry as T  # noqa: E402

DOC = {"version": 1, "minor_version": 5, "key": "core.config_entries", "data": {"entries": [
    {"domain": "tuya_local", "entry_id": "A" * 26, "title": "Plug", "unique_id": "dev1", "created_at": "x", "modified_at": "x", "version": 13, "minor_version": 24,
     "data": {"device_id": "dev1", "host": "1.2.3.4", "local_key": "k1", "poll_only": False, "protocol_version": 3.3, "type": "smartplugv2_energy"},
     "options": {}, "source": "user", "disabled_by": None, "discovery_keys": {}, "subentries": [], "pref_disable_new_entities": False, "pref_disable_polling": False},
    {"domain": "xiaomi_miot", "entry_id": "B" * 26, "title": "other", "unique_id": "u", "data": {}}]}}
CFG = {"DEVICE_ID": "robot9", "LOCAL_KEY": "sekret", "HOST": "192.168.31.77", "VERSION": "3.4"}


class T1(unittest.TestCase):
    def test_adds_one_entry_and_changes_nothing_else(self):
        before = copy.deepcopy(DOC)
        out = T.add_entry(DOC, CFG, "eta_tiger_vacuum", "ETA Tiger", now_iso="NOW", entry_id="C" * 26)
        self.assertEqual(DOC, before)                                   # input untouched
        self.assertEqual(out["data"]["entries"][:2], before["data"]["entries"])
        e = out["data"]["entries"][2]
        self.assertEqual((e["domain"], e["title"], e["unique_id"], e["entry_id"], e["version"], e["minor_version"]), ("tuya_local", "ETA Tiger", "robot9", "C" * 26, 13, 24))
        self.assertEqual(e["data"], {"device_id": "robot9", "host": "192.168.31.77", "local_key": "sekret", "poll_only": False, "protocol_version": 3.4, "type": "eta_tiger_vacuum"})
        json.dumps(out)

    def test_refuses_duplicates_and_bad_shapes(self):
        once = T.add_entry(DOC, CFG, "t", "x")
        with self.assertRaises(SystemExit):
            T.add_entry(once, CFG, "t", "x")
        with self.assertRaises(SystemExit):
            T.add_entry({"key": "other", "data": {}}, CFG, "t", "x")
        with self.assertRaises(SystemExit):
            T.add_entry({"key": "core.config_entries", "data": {"entries": []}}, CFG, "t", "x")

    def test_entry_ids(self):
        ids = {T.new_entry_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)
        self.assertTrue(all(len(i) == 26 and set(i) <= set(T.CROCKFORD) for i in ids))

    def test_env_reader(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("# c\nDEVICE_ID=a\nLOCAL_KEY=b\nHOST=c\nVERSION=3.3\n")
        self.assertEqual(T.read_env(f.name)["HOST"], "c")
        os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
