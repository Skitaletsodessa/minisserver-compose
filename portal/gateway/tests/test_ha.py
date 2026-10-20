"""The HA link on its own: subscribe_entities decoding, reconnect with backoff, the polling fallback, a refused token, the allow-list, read-back waits."""
import asyncio
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
from fakes import FakeHA, HA_TOKEN  # noqa: E402
from gw.ha import HA, parse_ts  # noqa: E402
from gw.hub import Hub  # noqa: E402
from gw.auth import GwError  # noqa: E402

SW = "switch.living_room_rack"


def token_file(text=HA_TOKEN):
    d = tempfile.mkdtemp(prefix="gw-ha-")
    p = os.path.join(d, "t")
    with open(p, "w") as f:
        f.write(text + "\n")
    os.chmod(p, 0o600)
    return p


class Decode(unittest.TestCase):
    def setUp(self):
        self.ha = HA("http://127.0.0.1:1", token_file(), hub=None)
        self.ha.set_scope({SW, "sensor.x"}, {SW})
        self.changes = []
        self.ha.on_change = lambda eid, old, new: self.changes.append((eid, old["state"] if old else None, new["state"]))

    def test_added_changed_removed(self):
        self.ha.apply_entities_event({"a": {SW: {"s": "off", "a": {"friendly_name": "R"}, "lc": 100.0, "lu": 101.0}, "switch.not_watched": {"s": "on", "a": {}, "lc": 1, "lu": 1}}})
        self.assertEqual(self.ha.state(SW), "off")
        self.assertIsNone(self.ha.get("switch.not_watched"))                      # outside the allow-list: dropped
        self.ha.apply_entities_event({"c": {SW: {"+": {"s": "on", "lc": 200.0, "lu": 200.0}}}})
        self.assertEqual((self.ha.state(SW), self.ha.get(SW)["changed"]), ("on", 200.0))
        self.assertEqual(self.ha.get(SW)["attrs"], {"friendly_name": "R"})         # attributes survive a state-only diff
        self.ha.apply_entities_event({"c": {SW: {"+": {"a": {"extra": 1}}, "-": {"a": ["friendly_name"]}}}})
        self.assertEqual(self.ha.get(SW)["attrs"], {"extra": 1})
        self.ha.apply_entities_event({"r": [SW]})
        self.assertEqual(self.ha.state(SW), "unavailable")
        self.assertEqual([c[1:] for c in self.changes], [(None, "off"), ("off", "on"), ("on", "on"), ("on", "unavailable")])

    def test_timestamps(self):
        self.assertEqual(parse_ts(12.5), 12.5)
        self.assertIsNotNone(parse_ts("2026-10-20T14:57:00+00:00"))
        self.assertIsNone(parse_ts("garbage"))
        self.assertIsNone(parse_ts(None))


class Link(unittest.TestCase):
    def setUp(self):
        self.fake = FakeHA().start()

    def tearDown(self):
        self.fake.stop()

    def run_ha(self, scenario, token=HA_TOKEN):
        async def go():
            hub = Hub()
            ha = HA(self.fake.url, token_file(token), hub=hub)
            ha.set_scope({SW, "switch.bedroom_parrot"}, {SW, "switch.bedroom_parrot"})
            await ha.start()
            try:
                return await scenario(ha, hub)
            finally:
                await ha.stop()
        return asyncio.run(go())

    @staticmethod
    async def until(cond, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            await asyncio.sleep(0.05)
        return False

    def test_websocket_link_and_push(self):
        async def sc(ha, hub):
            self.assertTrue(await self.until(lambda: ha.link == "ws"))
            self.fake.set(SW, "on")
            self.assertTrue(await self.until(lambda: ha.state(SW) == "on"))
            h = ha.health()
            self.assertEqual(h["link"], "ws")
            self.assertLess(h["last_event_age_s"], 5)
            obs, waited = await ha.wait_state(SW, lambda s: s == "on", 1)
            self.assertEqual(obs, "on")
            self.assertLess(waited, 0.5)
            obs, waited = await ha.wait_state(SW, lambda s: s == "flying", 0.6)             # read-back that never comes true: bounded, never raises
            self.assertEqual(obs, "on")
            self.assertGreaterEqual(waited, 0.55)
        self.run_ha(sc)

    def test_falls_back_to_polling_and_says_so(self):
        self.fake.no_ws = True

        async def sc(ha, hub):
            self.assertTrue(await self.until(lambda: ha.link == "poll", 25))
            self.fake.set(SW, "on")
            self.assertTrue(await self.until(lambda: ha.state(SW) == "on", 15))              # the poll loop (5 s) or the read-back refresh finds it
            obs, _w = await ha.wait_state(SW, lambda s: s == "on", 2)
            self.assertEqual(obs, "on")
            # switching still works and is confirmed by polling
            await ha.call("switch", "turn_off", SW)
            obs, _w = await ha.wait_state(SW, lambda s: s == "off", 4)
            self.assertEqual(obs, "off")
        self.run_ha(sc)

    def test_reconnect_after_the_socket_drops_and_the_link_events(self):
        async def sc(ha, hub):
            self.assertTrue(await self.until(lambda: ha.link == "ws"))
            self.fake.down = True
            self.assertTrue(await self.until(lambda: ha.link == "down", 40))
            self.fake.down = False
            self.assertTrue(await self.until(lambda: ha.link == "ws", 40))
            self.fake.set("switch.bedroom_parrot", "on")
            self.assertTrue(await self.until(lambda: ha.state("switch.bedroom_parrot") == "on"))
            kinds = [e[3]["ha"] for e in hub.ring if e[2] == "link"]
            self.assertIn("down", kinds)
            self.assertEqual(kinds[-1], "ws")
            self.assertFalse(ha.stale())
        self.run_ha(sc)

    def test_a_refused_token_never_leaks_into_anything(self):
        secret = "wrong-token-ABCDEF-must-never-appear"

        async def sc(ha, hub):
            await asyncio.sleep(2.5)
            self.assertIn(ha.link, ("down", "poll"))
            self.assertIsNone(ha.state(SW))
            with self.assertRaises(GwError) as c:
                await ha.call("switch", "turn_on", SW)
            blob = str(c.exception.args) + c.exception.code + c.exception.message + str(c.exception.details) + str(ha.health())
            self.assertNotIn(secret, blob)
            self.assertEqual(c.exception.status, 502)
        self.run_ha(sc, token=secret)
        self.assertEqual(self.fake.calls, [])

    def test_an_unreadable_token_file_is_a_down_link_not_a_crash(self):
        async def go():
            ha = HA(self.fake.url, "/nonexistent/path/token", hub=Hub())
            ha.set_scope({SW}, {SW})
            await ha.start()
            await asyncio.sleep(1.5)
            self.assertEqual(ha.link, "down")
            await ha.stop()
        asyncio.run(go())

    def test_allow_list_is_enforced_before_any_request(self):
        async def sc(ha, hub):
            await self.until(lambda: ha.link == "ws")
            for dom, svc, eid in (("switch", "turn_on", "switch.other"), ("homeassistant", "stop", SW), ("switch", "toggle", SW), ("light", "turn_off", SW)):
                with self.assertRaises(GwError) as c:
                    await ha.call(dom, svc, eid)
                self.assertEqual(c.exception.code, "not_allowed")
        self.run_ha(sc)
        self.assertEqual(self.fake.calls, [])

    def test_scope_change_resubscribes(self):
        async def sc(ha, hub):
            self.assertTrue(await self.until(lambda: ha.link == "ws"))
            ha.set_scope({SW, "switch.minisserver"}, {SW})
            await asyncio.sleep(0.2)
            self.fake.set("switch.minisserver", "on")
            self.assertTrue(await self.until(lambda: ha.state("switch.minisserver") == "on", 12))
        self.run_ha(sc)


if __name__ == "__main__":
    unittest.main()
