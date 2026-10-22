"""Task 31: the name a parent sees on the tile (display data only: the device name, which is the AdGuard client name, never changes)."""
import json
import os
import unittest

from tests.helpers import Env
from tests.test_child import PAYLOAD


class Display(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.assertEqual(self.e.req("PUT", "/v1/child-policy/import", json=PAYLOAD).status_code, 200)

    def test_admin_sets_and_clears_it_and_nothing_else_moves(self):
        snap0 = open(os.path.join(self.e.store.out, "child-policy.json")).read()
        r = self.e.req("PATCH", "/v1/child/child_a", json={"display_name": "Влад · телефон", "icon": "phone"})
        self.assertEqual((r.status_code, r.json()["display_name"], r.json()["icon"], r.json()["name"]), (200, "Влад · телефон", "phone", "a-phone"))
        self.assertEqual(open(os.path.join(self.e.store.out, "child-policy.json")).read(), snap0, "display data never reaches the snapshot")
        self.assertEqual(self.e.req("GET", "/v1/child/child_a").json()["display_name"], "Влад · телефон")
        self.assertEqual(self.e.req("PATCH", "/v1/child/child_a", json={"display_name": ""}).json()["display_name"], "")
        rows = [a["action"] for a in self.e.req("GET", "/v1/audit?limit=5").json()["audit"]]
        self.assertEqual(rows[:2], ["child_device_display", "child_device_display"])
        self.assertTrue(self.e.store.verify_audit()[0])

    def test_refusals(self):
        for who, body, code in (("household", {"display_name": "X1"}, 403), ("readonly", {"display_name": "X1"}, 403), ("admin", {}, 400), ("admin", {"name": "x"}, 400),
                                ("admin", {"display_name": 5}, 400), ("admin", {"display_name": "---"}, 422), ("admin", {"display_name": "a" * 41}, 422), ("admin", {"icon": "fridge"}, 422)):
            self.assertEqual(self.e.req("PATCH", "/v1/child/child_a", role=who, json=body).status_code, code, (who, body))
        self.assertEqual(self.e.req("PATCH", "/v1/child/nothing", json={"display_name": "Anna"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
