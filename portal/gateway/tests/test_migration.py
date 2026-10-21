"""A database created before display names existed (the live one, 2026-10-20) is migrated in place and its empty names are filled, never overwritten."""
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from gw import db as dbmod, config  # noqa: E402
from gw.core import Core  # noqa: E402


class Migration(unittest.TestCase):
    def test_old_device_meta_gets_the_column_and_the_seed_fills_only_empty_names(self):
        d = tempfile.mkdtemp(prefix="gw-mig-")
        c = sqlite3.connect(os.path.join(d, "gateway.db"))
        c.execute("CREATE TABLE device_meta(device_id TEXT PRIMARY KEY, kind TEXT NOT NULL, power_sensor TEXT, household_switchable INTEGER NOT NULL DEFAULT 1)")
        c.execute("INSERT INTO device_meta VALUES ('plant_rack1','lamp','sensor.x',1)")
        c.execute("INSERT INTO device_meta VALUES ('parrot_uv','uv',NULL,1)")
        c.commit()
        c.close()
        db = dbmod.DB(d)
        self.assertIn("display_name", [r["name"] for r in db.c.execute("PRAGMA table_info(device_meta)")])
        db.x("UPDATE device_meta SET display_name='Моё имя' WHERE device_id='parrot_uv'")          # an edit made by hand
        core = Core.__new__(Core)
        core.db = db
        core.seed()
        rows = {r["device_id"]: r["display_name"] for r in db.q("SELECT device_id, display_name FROM device_meta")}
        self.assertEqual(rows["plant_rack1"], config.DEVICE_SEED["plant_rack1"]["name"])
        self.assertEqual(rows["parrot_uv"], "Моё имя")
        core.seed()                                                                                 # idempotent
        self.assertEqual({r["device_id"]: r["display_name"] for r in db.q("SELECT device_id, display_name FROM device_meta")}, rows)



    def test_a_plug_added_after_the_first_start_gets_its_meta_row_once(self):
        d = tempfile.mkdtemp(prefix="gw-mig2-")
        db = dbmod.DB(d)
        core = Core.__new__(Core)
        core.db = db
        core.seed()
        db.x("DELETE FROM device_meta WHERE device_id='plant_citrus'")                              # a database from before the citrus plug existed
        db.x("UPDATE device_meta SET household_switchable=0, display_name='Моё' WHERE device_id='plant_rack1'")   # edits made by hand
        core.seed()
        c = db.q1("SELECT * FROM device_meta WHERE device_id='plant_citrus'")
        self.assertEqual((c["kind"], c["power_sensor"], c["household_switchable"], c["switch_mode"]), ("lamp", "sensor.smartplug_citrus_2ndfloor_power", 1, "plain"))
        r = db.q1("SELECT * FROM device_meta WHERE device_id='plant_rack1'")
        self.assertEqual((r["household_switchable"], r["display_name"]), (0, "Моё"))                 # never overwritten


if __name__ == "__main__":
    unittest.main()
