"""Task 30c (ADR 0010): the update channel. Ed25519 against the RFC vectors and OpenSSL; the APK reader; release add/publish/stop; the signed manifest (tamper any field -> rejected);
the endpoints (anonymous and wrong credentials identical, update key limited to the two paths, only the current release served, the hash recomputed); who may publish
(admin WEB session only); backup/restore of the release store; no secret in logs, audit or the database."""
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from helpers import Env, BaseCase, PASSWORD, validate_response  # noqa: E402
from test_app import AppBase, A, DEV  # noqa: E402
import test_app  # noqa: E402
from gw import apkinfo, ed25519, releases  # noqa: E402
import apkfake  # noqa: E402

CERT = b"cert-A-of-the-release-key"
CERT_FP = apkfake.fp(CERT)
ACTOR = {"person_id": "ivan", "phone_id": None, "ip": "t"}


def setUpModule():
    test_app.setUpModule()


def tearDownModule():
    test_app.tearDownModule()


class Ed25519Vectors(unittest.TestCase):
    VECTORS = [  # RFC 8032 section 7.1, tests 1-3
        ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60", "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
         "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
        ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb", "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
         "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ]

    def test_rfc8032_vectors_1_and_2(self):
        for seed, pub, msg, sig in self.VECTORS[:2]:
            self.assertEqual(ed25519.public_key(bytes.fromhex(seed)).hex(), pub)
            self.assertEqual(ed25519.sign(bytes.fromhex(seed), bytes.fromhex(msg)).hex(), sig)
            self.assertTrue(ed25519.verify(bytes.fromhex(pub), bytes.fromhex(msg), bytes.fromhex(sig)))

    def test_rejects_tampering_and_malformed_input(self):
        seed = bytes(range(32))
        pub = ed25519.public_key(seed)
        sig = ed25519.sign(seed, b"message")
        self.assertTrue(ed25519.verify(pub, b"message", sig))
        self.assertFalse(ed25519.verify(pub, b"messagE", sig))
        for i in (0, 31, 32, 63):
            bad = bytearray(sig)
            bad[i] ^= 1
            self.assertFalse(ed25519.verify(pub, b"message", bytes(bad)), i)
        self.assertFalse(ed25519.verify(ed25519.public_key(bytes(32)), b"message", sig))
        self.assertFalse(ed25519.verify(pub, b"message", sig[:63]))
        self.assertFalse(ed25519.verify(pub[:31], b"message", sig))
        self.assertFalse(ed25519.verify(b"\xff" * 32, b"message", sig))

    @unittest.skipUnless(shutil.which("openssl"), "openssl is not installed")
    def test_agrees_with_openssl(self):
        d = tempfile.mkdtemp()
        seed = bytes(range(7, 39))
        pkcs8 = bytes.fromhex("302e020100300506032b657004220420") + seed
        open(os.path.join(d, "k.der"), "wb").write(pkcs8)
        open(os.path.join(d, "m"), "wb").write(b'{"hello":"world"}')
        r = subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", os.path.join(d, "k.der"), "-keyform", "DER", "-rawin", "-in", os.path.join(d, "m"), "-out", os.path.join(d, "s")],
                           capture_output=True)
        if r.returncode:
            self.skipTest("this openssl cannot do raw Ed25519: %s" % r.stderr[:80])
        self.assertEqual(open(os.path.join(d, "s"), "rb").read(), ed25519.sign(seed, b'{"hello":"world"}'))


class ApkReader(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def apk(self, name="a.apk", **kw):
        p = os.path.join(self.d, name)
        apkfake.make_apk(p, **kw)
        return p

    def test_reads_identity_and_signer(self):
        info = apkinfo.inspect(self.apk(version_code=7, version_name="2.3.4", certs=(CERT,)))
        self.assertEqual((info["package"], info["version_code"], info["version_name"], info["certs"]), ("home.minis.portal", 7, "2.3.4", [CERT_FP]))

    def test_v3_block_and_two_certs(self):
        self.assertEqual(apkinfo.inspect(self.apk(scheme="v3", certs=(CERT, b"other")))["certs"], [CERT_FP, apkfake.fp(b"other")])

    def test_refusals(self):
        with self.assertRaises(apkinfo.ApkError):
            apkinfo.inspect(self.apk(with_block=False))                       # unsigned / v1-only
        with self.assertRaises(apkinfo.ApkError):
            apkinfo.inspect(self.apk(manifest=False))
        junk = os.path.join(self.d, "junk.apk")
        open(junk, "wb").write(b"not a zip at all" * 100)
        with self.assertRaises(apkinfo.ApkError):
            apkinfo.inspect(junk)
        bad = self.apk("bad.apk")
        data = bytearray(open(bad, "rb").read())
        data[1200] ^= 0xFF                                                    # a damaged byte inside the stored classes.dex: CRC error
        open(bad, "wb").write(bytes(data))
        with self.assertRaises(apkinfo.ApkError):
            apkinfo.inspect(bad)

    REAL = "/srv/apps/portal/gateway/app/portal.apk"

    @unittest.skipUnless(os.path.isfile(REAL), "no real APK on this machine")
    def test_the_real_release_build_is_read_correctly(self):
        info = apkinfo.inspect(self.REAL)
        self.assertEqual(info["package"], "home.minis.portal")
        self.assertEqual(len(set(info["certs"])), 1)
        meta = json.load(open(os.path.join(os.path.dirname(self.REAL), "portal.json")))
        self.assertEqual((info["version_code"], info["certs"][0]), (int(meta["versionCode"]), meta["cert_sha256"].lower().replace(":", "")))


class Store(BaseCase):
    """The release store without HTTP: add / publish / stop / the manifest."""
    def setUp(self):
        super().setUp()
        self.d = tempfile.mkdtemp()
        self.rel = self.e.o["releases"]
        self.e.db.x("DELETE FROM releases")
        for k in ("release_cert_sha256", "release_stopped", "release_manifest", "release_current", "release_min_supported"):
            self.e.db.x("DELETE FROM meta WHERE key=?", (k,))
        shutil.rmtree(self.rel.dir, ignore_errors=True)
        os.makedirs(self.rel.dir)
        self.pub = ed25519.public_key(bytes(range(32)))

    def apk(self, name, **kw):
        p = os.path.join(self.d, name)
        apkfake.make_apk(p, **kw)
        return p

    def add(self, code=4, name="1.1.0", notes="Новая версия", certs=(CERT,), **kw):
        return self.rel.add(self.apk("v%d.apk" % code, version_code=code, version_name=name, certs=certs, **kw), notes, ACTOR)

    def refused(self, fn, code):
        with self.assertRaises(releases.ReleaseError) as c:
            fn()
        self.assertEqual(c.exception.code, code)

    def test_add_needs_a_pinned_certificate_first_and_stores_pending(self):
        self.refused(lambda: self.add(), "not_pinned")
        self.refused(lambda: self.rel.pin("zz", ACTOR), "bad_fingerprint")
        self.rel.pin(CERT_FP, ACTOR)
        self.refused(lambda: self.rel.pin(apkfake.fp(b"x"), ACTOR), "already_pinned")
        info = self.add()
        self.assertEqual((info["version_code"], self.rel.list()[0]["state"]), (4, "pending"))
        self.assertTrue(self.rel.file_ok(4))
        self.assertIsNone(self.rel.current_manifest())                               # nothing is served before approval

    def test_every_refusal_of_add(self):
        self.rel.pin(CERT_FP, ACTOR)
        self.refused(lambda: self.add(certs=(b"another-key",)), "wrong_certificate")
        self.refused(lambda: self.add(certs=(CERT, b"another-key")), "wrong_certificate")
        self.refused(lambda: self.add(with_block=False), "bad_apk")
        self.refused(lambda: self.add(package="evil.app"), "wrong_app")
        self.refused(lambda: self.add(notes=""), "notes")
        self.refused(lambda: self.rel.add(os.path.join(self.d, "missing.apk"), "x", ACTOR), "no_file")
        junk = os.path.join(self.d, "junk.apk")
        open(junk, "wb").write(b"junk" * 50)
        self.refused(lambda: self.rel.add(junk, "x", ACTOR), "bad_apk")
        self.add(code=4)
        self.refused(lambda: self.add(code=4), "version_not_higher")                  # equal
        self.refused(lambda: self.add(code=3, name="0.9"), "version_not_higher")      # lower
        self.assertEqual([r["version_code"] for r in self.rel.list()], [4])
        self.assertEqual(self.add(code=5, name="1.1.1")["version_code"], 5)

    def test_publish_signs_now_and_the_manifest_verifies(self):
        self.rel.pin(CERT_FP, ACTOR)
        self.add()
        self.assertIsNone(self.rel.current_manifest())
        m = self.rel.publish(4, ACTOR)
        self.assertEqual(self.rel.list()[0]["state"], "published")
        p = releases.verify_manifest(m, [self.pub])
        self.assertEqual((p["app_id"], p["version_code"], p["version_name"], p["min_supported_version_code"], p["apk_url"], p["apk_signing_cert_sha256"], p["changelog"]),
                         ("home.minis.portal", 4, "1.1.0", 0, "/app/releases/4/app.apk", CERT_FP, "Новая версия"))
        self.assertEqual((p["apk_size"], p["apk_sha256"]), (os.path.getsize(self.rel.apk_path(4)), hashlib.sha256(open(self.rel.apk_path(4), "rb").read()).hexdigest()))
        self.assertEqual(self.rel.current_manifest(), m)
        self.assertEqual(self.rel.served_file(4), self.rel.apk_path(4))
        self.assertTrue(self.rel.health()["manifest_ok"] and self.rel.health()["apk_ok"])

    def test_a_tampered_manifest_is_rejected_whichever_field_changes(self):
        self.rel.pin(CERT_FP, ACTOR)
        self.add()
        m = self.rel.publish(4, ACTOR)
        p = json.loads(m["payload"])
        for field, new in (("version_code", 99), ("apk_sha256", "0" * 64), ("apk_size", 1), ("apk_url", "/app/releases/99/app.apk"), ("apk_signing_cert_sha256", "1" * 64),
                           ("min_supported_version_code", 4), ("changelog", "другое"), ("version_name", "9.9"), ("published_at", "2030-01-01T00:00:00+0000"), ("app_id", "evil.app")):
            q = dict(p)
            q[field] = new
            bad = {**m, "payload": releases.canonical(q)}
            with self.assertRaises(releases.ReleaseError, msg=field):
                releases.verify_manifest(bad, [self.pub])
        for bad in ({**m, "signature": base64.b64encode(b"\0" * 64).decode()}, {**m, "payload": m["payload"] + " "}, {**m, "schema": 2}, {**m, "signature": "!!!"}, {"schema": 1}, "x"):
            with self.assertRaises(releases.ReleaseError):
                releases.verify_manifest(bad, [self.pub])
        with self.assertRaises(releases.ReleaseError):
            releases.verify_manifest(m, [ed25519.public_key(bytes(32))])                # a different key
        self.assertEqual(releases.verify_manifest(m, [ed25519.public_key(bytes(32)), self.pub])["version_code"], 4)    # the two-key rule: any embedded key

    def test_roll_forward_only_and_the_minimum_rules(self):
        self.rel.pin(CERT_FP, ACTOR)
        self.add(4)
        self.add(5, "1.1.1")
        self.add(6, "1.2.0")
        self.refused(lambda: self.rel.publish(5, ACTOR, min_supported=6), "bad_min")      # more than its own version_code
        self.rel.publish(5, ACTOR, min_supported=4)
        self.assertEqual([r["state"] for r in self.rel.list()], ["pending", "published", "pending"])
        self.refused(lambda: self.rel.publish(4, ACTOR), "version_not_higher")            # an older one never goes out after a newer
        self.refused(lambda: self.rel.publish(5, ACTOR), "not_pending")
        self.refused(lambda: self.rel.publish(6, ACTOR, min_supported=3), "bad_min")      # never lowered
        self.refused(lambda: self.rel.publish(6, ACTOR, min_supported=True), "bad_min")
        m = self.rel.publish(6, ACTOR)                                                    # unchanged by default
        self.assertEqual(json.loads(m["payload"])["min_supported_version_code"], 4)
        self.assertEqual([r["version_code"] for r in self.rel.list() if r["state"] == "superseded"], [5])
        self.assertIsNone(self.rel.served_file(5))                                         # a superseded build is not served any more

    def test_kill_switch_and_resume(self):
        self.rel.pin(CERT_FP, ACTOR)
        self.add()
        self.refused(lambda: self.rel.resume(ACTOR), "nothing_published")
        self.rel.publish(4, ACTOR)
        self.rel.stop(ACTOR)
        self.assertTrue(self.rel.stopped())
        self.assertIsNone(self.rel.current_manifest())
        self.assertIsNone(self.rel.served_file(4))
        self.rel.resume(ACTOR)
        self.assertIsNotNone(self.rel.current_manifest())
        self.rel.stop(ACTOR)
        self.add(5, "1.1.1")
        self.rel.publish(5, ACTOR)                                                         # a deliberate new publication ends the stop
        self.assertFalse(self.rel.stopped())

    def test_a_file_that_no_longer_matches_its_hash_is_not_served_or_published(self):
        self.rel.pin(CERT_FP, ACTOR)
        self.add()
        with open(self.rel.apk_path(4), "ab") as f:
            f.write(b"x")
        self.refused(lambda: self.rel.publish(4, ACTOR), "file_mismatch")
        shutil.rmtree(self.rel.dir)
        os.makedirs(self.rel.dir)
        self.assertFalse(self.rel.file_ok(4))

    def test_a_missing_signing_key_refuses_to_publish(self):
        self.rel.pin(CERT_FP, ACTOR)
        self.add()
        keep = self.rel.secrets
        self.rel.secrets = os.path.join(self.d, "nokeys")
        try:
            self.refused(lambda: self.rel.publish(4, ACTOR), "no_key")
        finally:
            self.rel.secrets = keep


class Endpoints(AppBase):
    """HTTP: who may see the manifest and the bytes, who may publish."""
    def setUp(self):
        super().setUp()
        self.rel = self.e.o["releases"]
        self.e.db.x("DELETE FROM releases")
        for k in ("release_cert_sha256", "release_stopped", "release_manifest", "release_current", "release_min_supported"):
            self.e.db.x("DELETE FROM meta WHERE key=?", (k,))
        shutil.rmtree(self.rel.dir, ignore_errors=True)
        os.makedirs(self.rel.dir)
        self.d = tempfile.mkdtemp()
        self.rel.pin(CERT_FP, ACTOR)

    def pending(self, code=4, name="1.1.0"):
        p = os.path.join(self.d, "v%d.apk" % code)
        apkfake.make_apk(p, version_code=code, version_name=name, certs=(CERT,))
        self.rel.add(p, "Что нового: %s" % name, ACTOR)

    def v(self, method, path, r):
        validate_response(method, path, r)                                    # the contract (openapi.yaml) is checked on these raw calls as well
        return r

    def web_admin(self):
        res = self.signed_in("webadmin")
        r = self.e.client.post("/v1/auth/login", json={"username": "webadmin", "password": PASSWORD, "totp": self.code("webadmin")}, headers={"X-Forwarded-For": self.ip()})
        self.assertEqual(r.status_code, 200, r.text)
        ck = {"Cookie": "gw_session=" + re.search(r"gw_session=([^;]+)", r.headers["set-cookie"]).group(1)}
        return ck, r.json()["csrf"], res

    def test_anonymous_and_wrong_credentials_get_the_same_answer_on_both_paths(self):
        self.pending()
        self.rel.publish(4, ACTOR)
        outs = set()
        for path in ("/app/update.json", "/app/releases/4/app.apk", "/app/releases/99/app.apk"):
            for headers in ({}, {"Authorization": "Bearer nonsense"}, {"Authorization": "Bearer ukey_nonsense"}, {"Cookie": "gw_session=nonsense"}):
                r = self.e.client.get(path, headers=headers)
                self.assertEqual(r.status_code, 401, (path, headers))
                outs.add(r.text)
        self.assertEqual(len(outs), 1, outs)
        self.assertNotIn("1.1.0", list(outs)[0])

    def test_device_token_update_key_and_session_each_work_and_nothing_is_cached(self):
        self.pending()
        self.rel.publish(4, ACTOR)
        res = self.signed_in("webadmin")
        ukey = res["update_key"]
        self.e.tokens["ukey-" + res["phone_id"]] = ukey                                        # assert_no_secrets scans for it too
        self.assertTrue(ukey.startswith("ukey_"))
        for cred in ({"Authorization": "Bearer " + ukey}, {"Authorization": "Bearer " + res["token"]}):
            r = self.e.req("GET", "/app/update.json", who=None, token="", headers=cred, validate=True)
            self.assertEqual(r.status_code, 200, cred)
            self.assertEqual(r.headers["cache-control"], "no-store")
            p = releases.verify_manifest(r.json(), [ed25519.public_key(bytes(range(32)))])
            self.assertEqual(p["version_code"], 4)
            self.assertTrue(r.json()["update"])
            a = self.e.req("GET", p["apk_url"], who=None, token="", headers=cred, validate=True)
            self.assertEqual((a.status_code, a.headers["content-type"], a.headers["cache-control"]), (200, "application/vnd.android.package-archive", "no-store"))
            self.assertEqual(hashlib.sha256(a.content).hexdigest(), p["apk_sha256"])
            self.assertEqual(len(a.content), p["apk_size"])
        r, ck = self.app_session(res["token"])
        self.assertEqual(self.e.client.get("/app/update.json", headers=ck).status_code, 200)

    def test_the_update_key_opens_nothing_else(self):
        res = self.signed_in("webadmin")
        h = {"Authorization": "Bearer " + res["update_key"]}
        for method, path in (("GET", "/v1/capabilities"), ("GET", "/v1/devices"), ("GET", "/v1/releases"), ("POST", "/v1/app/session"), ("POST", "/v1/app/logout"), ("POST", "/v1/app/update-key"),
                             ("GET", "/v1/parental/children"), ("GET", "/v1/people"), ("POST", "/v1/releases/stop"), ("GET", "/home"), ("GET", "/parental")):
            r = self.e.client.request(method, path, headers={**h, "Idempotency-Key": "k-" + path[-6:] + "12345"}, follow_redirects=False)
            self.assertIn(r.status_code, (401, 303, 404), (method, path, r.status_code))
        self.assertFalse(self.e.client.get("/v1/events", headers=h).status_code == 200)

    def test_revoking_the_device_kills_the_update_key_and_a_new_key_replaces_the_old(self):
        self.pending()
        self.rel.publish(4, ACTOR)
        res = self.signed_in("webadmin")
        h = {"Authorization": "Bearer " + res["update_key"]}
        self.assertEqual(self.e.client.get("/app/update.json", headers=h).status_code, 200)
        r = self.e.req("POST", "/v1/app/update-key", who=None, token=res["token"])
        self.assertEqual(r.status_code, 201)
        new = r.json()["update_key"]
        self.e.tokens["ukey-new-" + res["phone_id"]] = new
        self.assertEqual(self.e.client.get("/app/update.json", headers=h).status_code, 401)                 # the old key is gone
        self.assertEqual(self.e.client.get("/app/update.json", headers={"Authorization": "Bearer " + new}).status_code, 200)
        self.assertEqual(self.e.req("POST", "/v1/app/update-key", who=None, token=new, validate=False).status_code, 401)   # an update key cannot mint keys
        self.e.req("POST", "/v1/app/logout", who=None, token=res["token"])
        self.assertEqual(self.e.client.get("/app/update.json", headers={"Authorization": "Bearer " + new}).status_code, 401)

    def test_no_update_answers(self):
        res = self.signed_in("webadmin")
        h = {"Authorization": "Bearer " + res["update_key"]}
        r = self.e.req("GET", "/app/update.json", who=None, token="", headers=h)
        self.assertEqual(r.json(), {"schema": 1, "update": False, "reason": "none"})
        self.pending()
        self.rel.publish(4, ACTOR)
        self.rel.stop(ACTOR)
        r = self.e.req("GET", "/app/update.json", who=None, token="", headers=h)
        self.assertEqual(r.json(), {"schema": 1, "update": False, "reason": "stopped"})
        self.assertEqual(self.e.client.get("/app/releases/4/app.apk", headers=h).status_code, 404)

    def test_only_the_current_release_is_served_and_the_hash_is_checked_on_every_request(self):
        self.pending(4)
        self.pending(5, "1.1.1")
        self.rel.publish(4, ACTOR)
        res = self.signed_in("webadmin")
        h = {"Authorization": "Bearer " + res["update_key"]}
        self.assertEqual(self.e.client.get("/app/releases/4/app.apk", headers=h).status_code, 200)
        self.assertEqual(self.e.client.get("/app/releases/5/app.apk", headers=h).status_code, 404)           # pending: not served
        self.rel.publish(5, ACTOR)
        self.assertEqual(self.e.client.get("/app/releases/4/app.apk", headers=h).status_code, 404)           # superseded: not served
        with open(self.rel.apk_path(5), "ab") as f:
            f.write(b"tamper")
        r = self.e.client.get("/app/releases/5/app.apk", headers=h)
        self.assertEqual(r.status_code, 404)                                                                  # recomputed, not trusted
        self.assertFalse(self.rel.health()["apk_ok"])

    def test_who_may_publish(self):
        self.pending()
        res = self.signed_in("webadmin")
        tok = {"Authorization": "Bearer " + res["token"]}
        r, ck = self.app_session(res["token"])
        csrf = r.json()["csrf"]
        # an APP session of the admin: refused (web sign-in only)
        for method, path in (("GET", "/v1/releases"), ("POST", "/v1/releases/4/publish"), ("POST", "/v1/releases/stop"), ("POST", "/v1/releases/resume")):
            rr = self.with_cookie(method, path, ck, csrf=csrf, json={} if method == "POST" else None)
            self.assertEqual((rr.status_code, rr.json()["error"]["code"]), (403, "web_login_required"), path)
        self.assertEqual(self.rel.list()[0]["state"], "pending")
        # household and anonymous
        self.assertEqual(self.e.req("POST", "/v1/releases/4/publish", who="dasha", json={}, validate=False).status_code, 403)
        self.assertEqual(self.e.req("GET", "/v1/releases", who="dasha", validate=False).status_code, 403)
        self.assertEqual(self.e.client.post("/v1/releases/4/publish", json={}, headers={"Idempotency-Key": "anon-12345"}).status_code, 401)
        self.assertEqual(self.e.client.get("/app/releases", follow_redirects=False).status_code, 303)
        # the admin on the web
        ck2, csrf2, _res = self.web_admin()
        r = self.e.client.post("/v1/releases/4/publish", headers={**ck2, "Idempotency-Key": "pub-12345678"}, json={})
        self.assertEqual(r.status_code, 403, "CSRF token is required")
        self.assertEqual(r.json()["error"]["code"], "csrf")
        ph = {**ck2, "X-CSRF-Token": csrf2}
        r = self.v("POST", "/v1/releases/4/publish", self.e.client.post("/v1/releases/4/publish", headers={**ph, "Idempotency-Key": "pub-12345678"}, json={"min_supported_version_code": 4}))
        self.assertEqual((r.status_code, r.json()["published"]), (200, 4), r.text)
        again = self.e.client.post("/v1/releases/4/publish", headers={**ph, "Idempotency-Key": "pub-12345678"}, json={"min_supported_version_code": 4})
        self.assertEqual((again.status_code, again.json().get("replayed")), (200, True))                      # a repeat is the same result
        lst = self.v("GET", "/v1/releases", self.e.client.get("/v1/releases", headers=ph))
        self.assertEqual((lst.status_code, lst.json()["releases"][0]["state"], lst.json()["releases"][0]["current"], lst.json()["pinned_cert_sha256"]), (200, "published", True, CERT_FP))
        rows = [x for x in self.e.db.audit_rows(10) if x["action"].startswith("release_")]
        self.assertIn("release_published", [x["action"] for x in rows])
        self.assertEqual([x["person"] for x in rows if x["action"] == "release_published"], ["webadmin"])
        pg = self.e.client.get("/app/releases", headers=ck2)
        self.assertEqual(pg.status_code, 200)
        self.assertIn("Выпуск приложения", pg.text)
        self.assertIn("/static/releases.js", pg.text)
        self.v("POST", "/v1/releases/stop", self.e.client.post("/v1/releases/stop", headers={**ph, "Idempotency-Key": "stop-1234567"}))
        self.assertTrue(self.rel.stopped())
        self.v("POST", "/v1/releases/resume", self.e.client.post("/v1/releases/resume", headers={**ph, "Idempotency-Key": "resume-12345"}))
        self.assertFalse(self.rel.stopped())

    def test_a_refused_publish_says_why(self):
        ck, csrf, _res = self.web_admin()
        ph = {**ck, "X-CSRF-Token": csrf}
        r = self.e.client.post("/v1/releases/9/publish", headers={**ph, "Idempotency-Key": "pub-nothing1"}, json={})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "not_found"))
        self.pending()
        r = self.e.client.post("/v1/releases/4/publish", headers={**ph, "Idempotency-Key": "pub-badmin12"}, json={"min_supported_version_code": 9})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (422, "bad_min"))
        r = self.e.client.post("/v1/releases/4/publish", headers={**ph, "Idempotency-Key": "pub-badbody1"}, json={"x": 1})
        self.assertEqual(r.status_code, 400)

    def test_the_release_event_reaches_open_streams_and_health_reports_the_channel(self):
        self.pending()
        before = self.e.hub.seq
        self.rel.publish(4, ACTOR)
        ev = [x for x in self.e.hub.since(before) if x[2] == "release"]
        self.assertEqual([x[3]["version_code"] for x in ev], [4])
        res = self.signed_in("webadmin")
        h = self.e.req("GET", "/v1/health", who=None, token=res["token"]).json()["release"]
        self.assertEqual((h["published"], h["stopped"], h["pending"], h["manifest_ok"], h["apk_ok"], h["key_ok"]), (4, False, 0, True, True, True))

    def test_no_secret_in_the_database_or_audit(self):
        self.pending()
        self.rel.publish(4, ACTOR)
        res = self.signed_in("webadmin")
        self.e.tokens["ukey-hyg-" + res["phone_id"]] = res["update_key"]
        self.e.client.get("/app/update.json", headers={"Authorization": "Bearer " + res["update_key"]})
        self.e.assert_no_secrets()                                                # raw tokens, update keys and the signing seed are not in gateway.db or audit.jsonl
        seed = bytes(range(32)).hex().encode()
        for suffix in ("", "-wal"):
            p = os.path.join(self.e.data, "gateway.db" + suffix)
            if os.path.exists(p):
                self.assertNotIn(seed, open(p, "rb").read())
        self.assertNotIn(seed, open(self.e.db.jsonl, "rb").read())

    def test_backup_and_restore_of_the_release_store(self):
        self.pending(4)
        self.rel.publish(4, ACTOR)
        old_manifest = self.rel.current_manifest()
        bk = tempfile.mkdtemp()
        subprocess.run(["sqlite3", os.path.join(self.e.data, "gateway.db"), ".backup '%s'" % os.path.join(bk, "gateway.db")], check=True) if shutil.which("sqlite3") else None
        if not os.path.exists(os.path.join(bk, "gateway.db")):
            import sqlite3
            src = sqlite3.connect(os.path.join(self.e.data, "gateway.db"))
            dst = sqlite3.connect(os.path.join(bk, "gateway.db"))
            src.backup(dst)
            dst.close()
        shutil.copytree(self.rel.dir, os.path.join(bk, "releases"))
        restored = os.path.join(bk, "restored")
        os.makedirs(restored)
        from gw import db as dbmod
        shutil.copyfile(os.path.join(bk, "gateway.db"), os.path.join(restored, "gateway.db"))
        d3 = dbmod.DB(restored)
        shutil.copytree(os.path.join(bk, "releases"), os.path.join(restored, "releases"), dirs_exist_ok=True)
        r3 = releases.Releases(d3, restored, secrets_dir=self.rel.secrets)
        self.assertEqual(r3.current_manifest(), old_manifest)
        self.assertTrue(r3.health()["apk_ok"] and r3.health()["manifest_ok"])
        self.assertEqual(r3.served_file(4), r3.apk_path(4))
        self.assertEqual(r3.pinned_cert(), CERT_FP)


if __name__ == "__main__":
    unittest.main()
