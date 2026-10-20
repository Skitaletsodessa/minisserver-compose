"""People, login, sessions, setup links, enrolment, phones: the rules of ADR 0007 at the domain level (synthetic accounts, scratch database, fake clock)."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from gw import auth, config, db as dbmod, people as peoplemod  # noqa: E402
from gw.auth import GwError  # noqa: E402
from gw.hub import Hub  # noqa: E402
from fakes import Clock  # noqa: E402

A = {"person_id": "test", "phone_id": None, "ip": "t"}
PW = "correct horse battery staple 42"


def mk():
    clk = Clock()
    d = tempfile.mkdtemp(prefix="gw-people-")
    db = dbmod.DB(d, clock=clk)
    hub = Hub(clock=clk)
    return db, peoplemod.People(db, hub, clock=clk), clk, hub


def web_account(P, db, clk, name="Ivan", role="admin"):
    """Creates a person with a password and TOTP the way the setup page does."""
    P.create(A, name, role, parent=True, web_login=True)
    pid = peoplemod.slug(name)
    raw, _ = P.create_setup_link(A, pid)
    _p, seed = P.setup_begin(raw)
    P.setup_complete(raw, PW, auth.totp_now(seed, clk()), "1.1.1.1")
    return pid, seed


def code_for(seed, clk):
    return auth.totp_now(seed, clk())


class Primitives(unittest.TestCase):
    def test_totp_rfc6238_vector(self):
        # RFC 6238 appendix B, SHA-1, secret "12345678901234567890", T=59 -> 94287082; our code has 6 digits -> 287082
        import base64
        seed = base64.b32encode(b"12345678901234567890").decode()
        self.assertEqual(auth.totp_now(seed, 59), "287082")
        self.assertEqual(auth.totp_now(seed, 1111111109), "081804")

    def test_totp_window_and_replay(self):
        seed = auth.new_totp_seed()
        t = 1_700_000_000.0
        c = auth.totp_now(seed, t)
        step = auth.verify_totp(seed, c, 0, now=t)
        self.assertIsNotNone(step)
        self.assertIsNone(auth.verify_totp(seed, c, step, now=t))                       # the same code twice
        self.assertIsNotNone(auth.verify_totp(seed, auth.totp_now(seed, t + 30), step, now=t))      # next step is fine (+-1 window)
        self.assertIsNone(auth.verify_totp(seed, auth.totp_now(seed, t + 300), 0, now=t))       # far in the future
        self.assertIsNone(auth.verify_totp(seed, "12345", 0, now=t))
        self.assertIsNone(auth.verify_totp(seed, "abcdef", 0, now=t))

    def test_password_hashing(self):
        h = auth.hash_password(PW)
        self.assertTrue(h.startswith("scrypt$"))
        self.assertNotIn(PW, h)
        self.assertTrue(auth.verify_password(PW, h))
        self.assertFalse(auth.verify_password(PW + "x", h))
        self.assertNotEqual(h, auth.hash_password(PW))                                  # per-user salt
        self.assertFalse(auth.verify_password(PW, "garbage"))
        self.assertIsNotNone(auth.password_problem("short"))
        self.assertIsNone(auth.password_problem(PW))

    def test_enrolment_code_alphabet(self):
        c = auth.new_code()
        self.assertEqual(len(c), 12)
        self.assertEqual(auth.norm_code(auth.fmt_code(c).lower()), c)
        self.assertFalse(set(c) & set("01OI"))


class SetupAndLogin(unittest.TestCase):
    def setUp(self):
        self.db, self.P, self.clk, self.hub = mk()

    def login(self, name, pw, totp, ip="9.9.9.9"):
        return self.P.login(name, pw, totp, ip)

    def test_setup_link_is_single_use_and_expires(self):
        self.P.create(A, "Ivan", "admin", True)
        raw, _ = self.P.create_setup_link(A, "ivan")
        _p, seed = self.P.setup_begin(raw)
        self.P.setup_complete(raw, PW, auth.totp_now(seed, self.clk()), "1.1.1.1")
        with self.assertRaises(GwError) as c:
            self.P.setup_begin(raw)
        self.assertEqual(c.exception.status, 410)
        raw2, _ = self.P.create_setup_link(A, "ivan")
        self.clk.advance(config.SETUP_TTL_S + 1)
        with self.assertRaises(GwError):
            self.P.setup_begin(raw2)

    def test_setup_needs_a_strong_password_and_the_right_code(self):
        self.P.create(A, "Ivan", "admin", True)
        raw, _ = self.P.create_setup_link(A, "ivan")
        _p, seed = self.P.setup_begin(raw)
        with self.assertRaises(GwError) as c:
            self.P.setup_complete(raw, "short", auth.totp_now(seed, self.clk()), "1.1.1.1")
        self.assertEqual(c.exception.code, "weak_password")
        with self.assertRaises(GwError) as c:
            self.P.setup_complete(raw, PW, "000000", "1.1.1.1")
        self.assertEqual(c.exception.code, "bad_totp")

    def test_admin_login_needs_password_and_totp(self):
        pid, seed = web_account(self.P, self.db, self.clk)
        self.clk.advance(31)
        raw, sess = self.login("ivan", PW, code_for(seed, self.clk))
        self.assertEqual(sess["person"]["role"], "admin")
        self.assertTrue(sess["csrf"])
        self.clk.advance(31)
        for bad in ((PW, None), (PW, "000000"), ("wrong password here", code_for(seed, self.clk))):
            with self.assertRaises(GwError) as c:
                self.login("ivan", bad[0], bad[1])
            self.assertEqual((c.exception.status, c.exception.code), (401, "invalid_credentials"))

    def test_a_totp_code_works_once(self):
        pid, seed = web_account(self.P, self.db, self.clk)
        self.clk.advance(31)
        c = code_for(seed, self.clk)
        self.login("ivan", PW, c)
        with self.assertRaises(GwError):
            self.login("ivan", PW, c)

    def test_unknown_account_and_wrong_password_look_the_same(self):
        web_account(self.P, self.db, self.clk)
        errs = []
        for name in ("ivan", "nobody"):
            try:
                self.login(name, "wrong password here", "123456")
            except GwError as e:
                errs.append((e.status, e.code, e.message))
        self.assertEqual(len(set(errs)), 1)

    def test_account_delay_grows_and_applies_to_the_right_password_too(self):
        pid, seed = web_account(self.P, self.db, self.clk)
        self.clk.advance(31)
        for _ in range(4):                       # three free failures, the fourth starts the delay
            with self.assertRaises(GwError):
                self.login("ivan", "bad password 000000", "000000")
        with self.assertRaises(GwError) as c:
            self.login("ivan", PW, code_for(seed, self.clk))              # even the right credentials wait
        self.assertEqual((c.exception.status, c.exception.code), (429, "account_delay"))
        first = c.exception.details["retry_after_s"]
        self.clk.advance(first + 1)
        with self.assertRaises(GwError):
            self.login("ivan", "bad password 000000", "000000")            # failure number 5 -> a longer delay
        with self.assertRaises(GwError) as c2:
            self.login("ivan", PW, code_for(seed, self.clk))
        self.assertGreater(c2.exception.details["retry_after_s"], first)
        self.clk.advance(901)
        self.clk.advance(31)
        self.login("ivan", PW, code_for(seed, self.clk))                  # the window has passed

    def test_address_limit_blocks_everyone_from_that_address(self):
        web_account(self.P, self.db, self.clk)
        for i in range(peoplemod.IP_FAIL_LIMIT):
            with self.assertRaises(GwError):
                self.login("user%d" % i, "bad password 000000", "000000", ip="6.6.6.6")      # many accounts, one address
        with self.assertRaises(GwError) as c:
            self.login("anyone", "x", "000000", ip="6.6.6.6")
        self.assertEqual(c.exception.code, "too_many_attempts")
        with self.assertRaises(GwError) as c:
            self.login("anyone", "x", "000000", ip="7.7.7.7")                # another address is not blocked (plain 401)
        self.assertEqual(c.exception.status, 401)

    def test_non_admin_without_web_login_cannot_log_in(self):
        self.P.create(A, "Dasha", "household", True, web_login=False)
        with self.assertRaises(GwError) as c:
            self.login("dasha", PW, None)
        self.assertEqual(c.exception.status, 401)
        with self.assertRaises(GwError) as c:
            self.P.create_setup_link(A, "dasha")
        self.assertEqual(c.exception.code, "web_login_disabled")

    def test_idle_timeout_alone(self):
        pid, seed = web_account(self.P, self.db, self.clk)
        self.clk.advance(31)
        raw, _ = self.login("ivan", PW, code_for(seed, self.clk))
        self.clk.advance(config.SESSION_IDLE_S + 1)
        self.assertIsNone(self.P.session_principal(raw))

    def test_absolute_timeout_even_when_active(self):
        pid, seed = web_account(self.P, self.db, self.clk)
        self.clk.advance(31)
        raw, _ = self.login("ivan", PW, code_for(seed, self.clk))
        t_end = self.clk() + config.SESSION_ABS_S
        while self.clk() < t_end - 3600:
            self.clk.advance(3600 * 6)
            if self.clk() < t_end:
                self.assertIsNotNone(self.P.session_principal(raw))
        self.clk.advance(7200)
        self.assertIsNone(self.P.session_principal(raw))

    def test_deactivating_a_person_ends_sessions_and_phones(self):
        pid, seed = web_account(self.P, self.db, self.clk)
        self.P.create(A, "Dasha", "household", True)
        code, _ = self.P.create_enrolment_code(A, "dasha")
        res = self.P.enrol(code, {"label": "p"}, "1.1.1.1")
        self.P.patch(A, "dasha", {"active": False})
        self.assertIsNone(self.P.phone_principal(res["token"]))
        self.assertTrue(self.P.phones()[0]["revoked"])

    def test_the_last_admin_is_protected(self):
        web_account(self.P, self.db, self.clk)
        for fields in ({"active": False}, {"role": "household"}):
            with self.assertRaises(GwError) as c:
                self.P.patch(A, "ivan", fields)
            self.assertEqual(c.exception.code, "last_admin")

    def test_reset_credentials_forgets_password_and_sessions(self):
        pid, seed = web_account(self.P, self.db, self.clk)
        self.clk.advance(31)
        raw, _ = self.login("ivan", PW, code_for(seed, self.clk))
        self.P.reset_credentials(A, "ivan")
        self.assertIsNone(self.P.session_principal(raw))
        self.clk.advance(31)
        with self.assertRaises(GwError):
            self.login("ivan", PW, code_for(seed, self.clk))
        self.assertFalse(self.P.get("ivan")["has_web_credentials"])


class Enrolment(unittest.TestCase):
    def setUp(self):
        self.db, self.P, self.clk, self.hub = mk()
        self.P.create(A, "Ivan", "admin", True)
        self.P.create(A, "Dasha", "household", True)

    def test_flow_and_token_is_hashed(self):
        code, exp = self.P.create_enrolment_code(A, "dasha")
        res = self.P.enrol(auth.fmt_code(code).lower(), {"label": "Dasha phone", "model": "X"}, "1.1.1.1")
        self.assertEqual(res["person"]["id"], "dasha")
        stored = [dict(r) for r in self.db.q("SELECT * FROM phones")]
        self.assertNotIn(res["token"], str(stored))
        self.assertEqual(stored[0]["token_hash"], auth.sha(res["token"]))
        p = self.P.phone_principal(res["token"])
        self.assertEqual((p["person_id"], p["role"], p["phone_id"]), ("dasha", "household", res["phone_id"]))
        self.assertNotIn(code, str([dict(r) for r in self.db.q("SELECT * FROM enrol_codes")]))      # codes are stored hashed

    def test_a_code_works_once_expires_and_unknown_codes_look_alike(self):
        code, _ = self.P.create_enrolment_code(A, "dasha")
        self.P.enrol(code, {"label": "a"}, "1.1.1.1")
        msgs = set()
        for c in (code, auth.new_code(), "!!!"):
            try:
                self.P.enrol(c, {"label": "b"}, "1.1.1.1")
            except GwError as e:
                msgs.add((e.status, e.code, e.message))
        self.assertEqual(len(msgs), 1)
        code, _ = self.P.create_enrolment_code(A, "dasha")
        self.clk.advance(config.ENROL_TTL_S + 1)
        with self.assertRaises(GwError) as c:
            self.P.enrol(code, {"label": "late"}, "1.1.1.1")
        self.assertEqual(c.exception.code, "invalid_code")

    def test_a_new_code_kills_the_older_unused_one(self):
        c1, _ = self.P.create_enrolment_code(A, "dasha")
        c2, _ = self.P.create_enrolment_code(A, "dasha")
        with self.assertRaises(GwError):
            self.P.enrol(c1, {"label": "a"}, "1.1.1.1")
        self.P.enrol(c2, {"label": "a"}, "1.1.1.1")

    def test_guessing_is_rate_limited_per_address(self):
        for _ in range(peoplemod.ENROL_FAIL_LIMIT):
            with self.assertRaises(GwError):
                self.P.enrol(auth.new_code(), {"label": "g"}, "5.5.5.5")
        code, _ = self.P.create_enrolment_code(A, "dasha")
        with self.assertRaises(GwError) as c:
            self.P.enrol(code, {"label": "g"}, "5.5.5.5")                    # even the right code, from the blocked address
        self.assertEqual(c.exception.code, "too_many_attempts")
        self.P.enrol(code, {"label": "g"}, "5.5.5.6")

    def test_limits_5_per_person_and_20_overall(self):
        for i in range(config.MAX_PHONES_PER_PERSON):
            code, _ = self.P.create_enrolment_code(A, "dasha")
            self.P.enrol(code, {"label": "d%d" % i}, "1.1.1.1")
        with self.assertRaises(GwError) as c:
            self.P.create_enrolment_code(A, "dasha")
        self.assertEqual(c.exception.code, "device_limit")
        # revoke one: possible again
        self.P.revoke_phone({"role": "admin", "person_id": "ivan", "phone_id": None, "ip": "t"}, self.P.phones()[0]["id"])
        code, _ = self.P.create_enrolment_code(A, "dasha")
        self.P.enrol(code, {"label": "again"}, "1.1.1.1")
        # overall: fill up to 20 with other people
        for n in range(3):
            self.P.create(A, "Guest %d" % n, "readonly", False)
        total = self.P._active_phones()
        pid = 0
        while total < config.MAX_PHONES_TOTAL:
            who = "guest-%d" % (pid % 3)
            if self.P._active_phones(who) >= config.MAX_PHONES_PER_PERSON:
                pid += 1
                continue
            code, _ = self.P.create_enrolment_code(A, who)
            self.P.enrol(code, {"label": "g"}, "1.1.1.1")
            total += 1
        with self.assertRaises(GwError) as c:
            self.P.create_enrolment_code(A, "ivan")
        self.assertEqual(c.exception.code, "device_limit")

    def test_the_code_can_not_be_used_for_an_inactive_person(self):
        code, _ = self.P.create_enrolment_code(A, "dasha")
        self.P.patch(A, "dasha", {"active": False})
        with self.assertRaises(GwError):
            self.P.enrol(code, {"label": "x"}, "1.1.1.1")

    def test_revocation_takes_effect_on_the_next_request_and_kicks_the_stream(self):
        code, _ = self.P.create_enrolment_code(A, "dasha")
        res = self.P.enrol(code, {"label": "x"}, "1.1.1.1")
        self.assertIsNotNone(self.P.phone_principal(res["token"]))
        sid, q = None, None
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            async def go():
                nonlocal sid, q
                sid, q = self.hub.subscribe(res["phone_id"])
                self.P.revoke_phone({"role": "admin", "person_id": "ivan", "phone_id": None, "ip": "t"}, res["phone_id"])
                return q.get_nowait()
            self.assertIsNone(loop.run_until_complete(go()))                  # None = "close this stream"
        finally:
            loop.close()
        self.assertIsNone(self.P.phone_principal(res["token"]))
        self.assertFalse(self.P.phone_principal_valid(res["phone_id"]))

    def test_a_person_may_revoke_only_their_own_phone(self):
        code, _ = self.P.create_enrolment_code(A, "dasha")
        res = self.P.enrol(code, {"label": "x"}, "1.1.1.1")
        with self.assertRaises(GwError) as c:
            self.P.revoke_phone({"role": "household", "person_id": "someone", "phone_id": None, "ip": "t"}, res["phone_id"])
        self.assertEqual(c.exception.status, 403)
        self.P.revoke_phone({"role": "household", "person_id": "dasha", "phone_id": None, "ip": "t"}, res["phone_id"])

    def test_the_ninety_day_expiry_slides(self):
        code, _ = self.P.create_enrolment_code(A, "dasha")
        res = self.P.enrol(code, {"label": "x"}, "1.1.1.1")
        for _ in range(4):
            self.clk.advance(60 * 86400)                                     # used every 60 days: never expires
            self.assertIsNotNone(self.P.phone_principal(res["token"]))
        self.clk.advance(config.PHONE_SLIDING_S + 1)                          # unused for 90 days: expired
        self.assertIsNone(self.P.phone_principal(res["token"]))


class AuditAndSecrets(unittest.TestCase):
    def test_audit_chain_and_no_secret_in_it(self):
        db, P, clk, _hub = mk()
        pid, seed = web_account(P, db, clk)
        clk.advance(31)
        P.login("ivan", PW, code_for(seed, clk), "2.2.2.2")
        try:
            P.login("ivan", "wrong password here", "000000", "2.2.2.2")
        except GwError:
            pass
        P.create(A, "Dasha", "household", True)
        code, _ = P.create_enrolment_code(A, "dasha")
        res = P.enrol(code, {"label": "p"}, "3.3.3.3")
        ok, rows, bad = db.verify_audit()
        self.assertTrue(ok and rows >= 6, (ok, rows, bad))
        blob = open(db.jsonl).read() + str(db.audit_rows(500)) + str([dict(r) for r in db.q("SELECT * FROM audit")])
        for secret in (PW, seed, res["token"], code, auth.fmt_code(code), "wrong password here"):
            self.assertNotIn(secret, blob)
        actions = [r["action"] for r in db.audit_rows(100)]
        for a in ("login", "login_failed", "setup_complete", "enrol", "enrol_code_create", "person_create"):
            self.assertIn(a, actions)

    def test_the_audit_is_append_only_and_tampering_is_detected(self):
        import sqlite3
        db, P, clk, _hub = mk()
        P.create(A, "Ivan", "admin", True)
        P.create(A, "Dasha", "household", True)
        for sql in ("UPDATE audit SET action='x' WHERE seq=1", "DELETE FROM audit WHERE seq=1"):
            with self.assertRaises(sqlite3.DatabaseError):
                db.c.execute(sql)
        raw = sqlite3.connect(os.path.join(db.dir, "gateway.db"))
        raw.execute("DROP TRIGGER audit_no_update")
        raw.execute("UPDATE audit SET after='{\"forged\":1}' WHERE seq=2")
        raw.commit()
        raw.close()
        ok, _rows, bad = db.verify_audit()
        self.assertEqual((ok, bad), (False, 2))


if __name__ == "__main__":
    unittest.main()
