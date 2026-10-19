import datetime as dt
import unittest
from sched import model

PLUG = {"id": "p", "kind": "plug", "guardrails": {"max_on_minutes": 120, "allowed_from": "06:00", "allowed_to": "21:00", "min_gap_minutes": 30}}
VAC = {"id": "v", "kind": "vacuum_plan", "guardrails": {"allowed_from": "08:00", "allowed_to": "22:00", "min_gap_minutes": 180, "max_triggers_per_day": 3}}
ALL = [1, 2, 3, 4, 5, 6, 7]


def R(i, s, e, days=ALL, **kw):
    return {"id": i, "days": days, "start": s, "end": e, **kw}


def codes(entity, rules):
    _n, probs = model.validate_rules(entity, rules)
    return sorted(p["code"] for p in probs)


class Guardrails(unittest.TestCase):
    def test_valid_dryer(self):
        n, p = model.validate_rules(PLUG, [R("morning", "06:30", "08:00"), R("evening", "18:30", "20:00")])
        self.assertEqual(p, [])
        self.assertEqual([r["id"] for r in n], ["morning", "evening"])

    def test_window_longer_than_max_on_is_rejected_not_clamped(self):
        n, p = model.validate_rules(PLUG, [R("a", "06:30", "09:00")])        # 150 min > 120
        self.assertEqual([x["code"] for x in p], ["max_on"])
        self.assertEqual(n, [])                                              # nothing was stored in a "fixed" form

    def test_exactly_the_limit_is_allowed(self):
        self.assertEqual(codes(PLUG, [R("a", "06:30", "08:30")]), [])
        self.assertEqual(codes(PLUG, [R("a", "06:30", "08:31")]), ["max_on"])

    def test_allowed_range(self):
        self.assertEqual(codes(PLUG, [R("a", "05:59", "07:00")]), ["allowed_range"])
        self.assertEqual(codes(PLUG, [R("a", "20:00", "21:01")]), ["allowed_range"])
        self.assertEqual(codes(PLUG, [R("a", "06:00", "07:00"), R("b", "20:00", "21:00")]), [])

    def test_dst_hours_rejected(self):
        wide = {**PLUG, "guardrails": {"max_on_minutes": 600, "allowed_from": "00:00", "allowed_to": "23:59", "min_gap_minutes": 0}}
        self.assertEqual(codes(wide, [R("a", "02:00", "05:00")]), ["dst_ambiguous"])
        self.assertEqual(codes(wide, [R("a", "03:59", "05:00")]), ["dst_ambiguous"])
        self.assertEqual(codes(wide, [R("a", "01:00", "03:30")]), ["dst_ambiguous"])
        self.assertEqual(codes(wide, [R("a", "04:00", "05:00")]), [])
        self.assertEqual(codes(wide, [R("a", "01:00", "02:00")]), ["dst_ambiguous"])

    def test_overlap_and_gap(self):
        self.assertEqual(codes(PLUG, [R("a", "06:30", "08:00"), R("b", "07:00", "09:00")]), ["overlap"])
        self.assertEqual(codes(PLUG, [R("a", "06:30", "08:00"), R("b", "08:20", "09:00")]), ["min_gap"])
        self.assertEqual(codes(PLUG, [R("a", "06:30", "08:00"), R("b", "08:30", "09:30")]), [])

    def test_overlap_only_on_shared_days(self):
        self.assertEqual(codes(PLUG, [R("a", "06:30", "08:00", [1, 2, 3]), R("b", "07:00", "09:00", [4, 5])]), [])

    def test_gap_wraps_round_the_week(self):
        plants = {**PLUG, "guardrails": {"max_on_minutes": 810, "allowed_from": "05:00", "allowed_to": "22:00", "min_gap_minutes": 360}}
        self.assertEqual(codes(plants, [R("a", "07:00", "20:00")]), [])
        # 05:00-22:00 daily leaves only 7 h of night: fine; but the night must stay >= 6 h
        self.assertEqual(codes(plants, [R("a", "05:00", "18:00"), R("b", "20:00", "21:00")]), ["min_gap"])
        self.assertEqual(codes(plants, [R("a", "05:00", "15:00")]), [])

    def test_plug_needs_end_and_vacuum_forbids_it(self):
        self.assertEqual(codes(PLUG, [{"id": "a", "days": ALL, "start": "07:00"}]), ["format"])
        self.assertEqual(codes(VAC, [R("a", "09:00", "10:00")]), ["not_allowed"])

    def test_window_must_end_after_start(self):
        self.assertEqual(codes(PLUG, [R("a", "08:00", "07:00")]), ["window"])
        self.assertEqual(codes(PLUG, [R("a", "08:00", "08:00")]), ["window"])

    def test_vacuum_gap_range_and_count(self):
        self.assertEqual(codes(VAC, [{"id": "a", "days": ALL, "start": "09:00"}, {"id": "b", "days": ALL, "start": "19:00"}]), [])
        self.assertEqual(codes(VAC, [{"id": "a", "days": ALL, "start": "09:00"}, {"id": "b", "days": ALL, "start": "10:00"}]), ["min_gap"])
        self.assertEqual(codes(VAC, [{"id": "a", "days": ALL, "start": "07:59"}]), ["allowed_range"])
        self.assertEqual(codes(VAC, [{"id": "a", "days": ALL, "start": "22:01"}]), ["allowed_range"])
        four = [{"id": "r%d" % i, "days": [1], "start": "%02d:00" % (8 + 3 * i)} for i in range(4)]
        self.assertIn("max_triggers", codes({**VAC, "guardrails": {**VAC["guardrails"], "min_gap_minutes": 0}}, four))

    def test_variables(self):
        ok = {"id": "a", "days": ALL, "start": "09:00", "variables": {"plan": "morning"}}
        self.assertEqual(codes(VAC, [ok]), [])
        self.assertEqual(codes(VAC, [{**ok, "variables": {"Plan": "x"}}]), ["variables"])
        self.assertEqual(codes(VAC, [{**ok, "variables": {"plan": {"x": 1}}}]), ["variables"])
        self.assertEqual(codes(PLUG, [R("a", "07:00", "08:00", variables={"x": 1})]), ["not_allowed"])

    def test_shape_errors(self):
        self.assertEqual(codes(PLUG, "nope"), ["type"])
        self.assertEqual(codes(PLUG, [R("A!", "07:00", "08:00")]), ["format"])
        self.assertEqual(codes(PLUG, [R("a", "07:00", "08:00", days=[])]), ["days"])
        self.assertEqual(codes(PLUG, [R("a", "07:00", "08:00", days=[1, 1])]), ["days"])
        self.assertEqual(codes(PLUG, [R("a", "07:00", "08:00", days=[8])]), ["days"])
        self.assertEqual(codes(PLUG, [R("a", "7:00", "08:00")]), ["format"])
        self.assertEqual(codes(PLUG, [R("a", "07:00", "08:00"), R("a", "09:00", "10:00")]), ["duplicate"])
        self.assertEqual(codes(PLUG, [R("a", "07:00", "08:00", extra=1)]), ["unknown_key"])
        self.assertEqual(codes(PLUG, [R("a", "07:00", "08:00", level=5)]), ["not_allowed"])
        self.assertIn("too_many", codes(PLUG, [R("r%d" % i, "07:00", "07:01") for i in range(13)]))

    def test_level_guardrail(self):
        dim = {"id": "d", "kind": "dimmer", "guardrails": {"max_level": 20, "allowed_from": "07:00", "allowed_to": "20:00"}}
        self.assertEqual(codes(dim, [{"id": "a", "days": ALL, "start": "08:00", "level": 20}]), [])
        self.assertEqual(codes(dim, [{"id": "a", "days": ALL, "start": "08:00", "level": 21}]), ["max_level"])

    def test_guardrail_values_validated(self):
        _g, p = model.validate_guardrails("plug", {"max_on_minutes": 0})
        self.assertTrue(p)
        _g, p = model.validate_guardrails("plug", {"max_on_minutes": 60, "allowed_from": "20:00", "allowed_to": "06:00"})
        self.assertTrue(p)
        _g, p = model.validate_guardrails("plug", {"allowed_from": "06:00", "allowed_to": "21:00"})
        self.assertEqual([x["code"] for x in p], ["required"])             # a plug without max_on has no fail-safe
        _g, p = model.validate_guardrails("plug", {"max_on_minutes": 60, "allowed_from": "06:00", "allowed_to": "21:00", "bogus": 1})
        self.assertEqual([x["code"] for x in p], ["unknown_key"])
        g, p = model.validate_guardrails("plug", PLUG["guardrails"])
        self.assertEqual((g, p), (PLUG["guardrails"], []))


class Edges(unittest.TestCase):
    RULES = [R("morning", "06:30", "08:00"), R("evening", "18:30", "20:00")]

    def test_day_edges(self):
        e = model.day_edges("plug", self.RULES, dt.date(2026, 10, 19))
        self.assertEqual(e, [(390, "on", "morning"), (480, "off", "morning"), (1110, "on", "evening"), (1200, "off", "evening")])

    def test_weekday_filter_and_skip(self):
        rules = [R("a", "07:00", "08:00", [1, 3])]
        self.assertEqual(len(model.day_edges("plug", rules, dt.date(2026, 10, 19))), 2)      # Monday
        self.assertEqual(model.day_edges("plug", rules, dt.date(2026, 10, 20)), [])          # Tuesday
        self.assertEqual(model.day_edges("plug", rules, dt.date(2026, 10, 19), {"2026-10-19"}), [(480, "off", "a")])

    def test_target_state_matches_old_reconcile_script(self):
        # old: t >= 390 and t < 480 or t >= 1110 and t < 1200
        for t in range(1440):
            at = dt.datetime(2026, 10, 19, t // 60, t % 60, tzinfo=model.TZ)
            old = "on" if (390 <= t < 480) or (1110 <= t < 1200) else "off"
            self.assertEqual(model.target_state("plug", self.RULES, at), old, t)

    def test_edges_between_and_next_edge(self):
        a = dt.datetime(2026, 10, 19, 0, 0, tzinfo=model.TZ)
        es = model.edges_between("plug", self.RULES, a, a + dt.timedelta(days=2))
        self.assertEqual(len(es), 8)
        self.assertEqual(es[0][0].hour, 6)
        ne = model.next_edge("plug", self.RULES, dt.datetime(2026, 10, 19, 8, 0, tzinfo=model.TZ))
        self.assertEqual((ne[0].hour, ne[0].minute, ne[1]), (18, 30, "on"))

    def test_dst_days_have_the_right_edges(self):
        # 2026-10-25 is the autumn DST change in Europe/Prague: wall-clock edges stay at their wall-clock times
        a = dt.datetime(2026, 10, 25, 0, 0, tzinfo=model.TZ)
        es = model.edges_between("plug", self.RULES, a, a + dt.timedelta(days=1))
        self.assertEqual([(t.hour, t.minute) for t, _a, _r in es], [(6, 30), (8, 0), (18, 30), (20, 0)])

    def test_effective(self):
        ent = {**PLUG}
        at = dt.datetime(2026, 10, 19, 7, 0, tzinfo=model.TZ)
        eff = model.effective(ent, self.RULES, [], at)
        self.assertEqual((eff["state"], eff["next_edge"]["action"]), ("on", "off"))
        ex = [{"id": "x1", "kind": "skip_date", "date": "2026-10-19", "created_at": "2026-10-18T10:00:00+02:00"}]
        eff = model.effective(ent, self.RULES, ex, at)
        self.assertEqual((eff["state"], eff["skipped_today"]), ("off", True))
        ov = [{"id": "x2", "kind": "override_until", "until": "09:00", "state": "on", "created_at": "2026-10-19T06:00:00+02:00"}]
        eff = model.effective(ent, self.RULES, ov, dt.datetime(2026, 10, 19, 8, 30, tzinfo=model.TZ))
        self.assertEqual((eff["state"], eff["source"]), ("on", "override"))


class Exceptions(unittest.TestCase):
    TODAY = dt.date(2026, 10, 19)

    def test_skip_date(self):
        ok, p = model.validate_exception(PLUG, {"kind": "skip_date", "date": "2026-10-21"}, self.TODAY)
        self.assertEqual((ok["date"], p), ("2026-10-21", []))
        self.assertEqual(model.validate_exception(PLUG, {"kind": "skip_date", "date": "2026-10-18"}, self.TODAY)[1][0]["code"], "past")
        self.assertEqual(model.validate_exception(PLUG, {"kind": "skip_date", "date": "2028-01-01"}, self.TODAY)[1][0]["code"], "far")
        self.assertEqual(model.validate_exception(PLUG, {"kind": "skip_date", "date": "tomorrow"}, self.TODAY)[1][0]["code"], "format")

    def test_override(self):
        ok, p = model.validate_exception(PLUG, {"kind": "override_until", "until": "20:00", "state": "on"}, self.TODAY)
        self.assertEqual(p, [])
        _o, p = model.validate_exception(PLUG, {"kind": "override_until", "until": "22:00", "state": "on"}, self.TODAY)
        self.assertEqual([x["code"] for x in p], ["allowed_range"])      # an override may not run past the allowed range
        _o, p = model.validate_exception(PLUG, {"kind": "override_until", "until": "03:00", "state": "off"}, self.TODAY)
        self.assertEqual([x["code"] for x in p], ["dst_ambiguous"])
        _o, p = model.validate_exception(VAC, {"kind": "override_until", "until": "20:00", "state": "on"}, self.TODAY)
        self.assertTrue(p)


if __name__ == "__main__":
    unittest.main()
