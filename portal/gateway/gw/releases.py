"""The app's own update channel (Task 30c, ADR 0010): release store, signed manifest, publish/stop, the update-only credential.

Lifecycle:  operator `gwctl release add APK --notes ...`  ->  state `pending`  ->  an admin presses «Опубликовать» in the web portal (password + code, tailnet)  ->  the manifest
is SIGNED at that moment (Ed25519) and becomes the one the phones fetch  ->  the previous published release becomes `superseded`. Roll-forward only: a published version_code
never goes down. «Остановить выдачу» is a kill switch (the endpoint answers "no update"; nothing is un-published to an older build).

The signing key (a 32-byte seed, hex, mode 600) is made on the host by tools/release_keygen.py and mounted read-only at /secrets; the APK signing key never reaches the server."""
import base64
import hashlib
import json
import os
import re
import shutil
import time

from . import apkinfo, ed25519

APP_ID = "home.minis.portal"
MAX_APK = 80 * 1024 * 1024
KEY_FILE = "release-manifest.key"
PUB_FILE = "release-manifest.pub"
KEY_ID = "k1"
SCHEMA = 1
META_CERT, META_STOPPED, META_MANIFEST, META_CURRENT, META_MIN = "release_cert_sha256", "release_stopped", "release_manifest", "release_current", "release_min_supported"
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS releases(
  version_code INTEGER PRIMARY KEY, version_name TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL, cert_sha256 TEXT NOT NULL, notes TEXT NOT NULL,
  state TEXT NOT NULL, added_by TEXT, added_at REAL NOT NULL, published_by TEXT, published_at REAL, min_supported INTEGER);
"""


class ReleaseError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def verify_manifest(manifest, public_keys):
    """What the app does, in Python (the tests and `gwctl release verify` use it): the signature over the exact payload string must verify under one of the public keys;
    only then is the payload parsed. Returns the payload dict or raises ReleaseError."""
    try:
        if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
            raise ReleaseError("schema", "unknown manifest schema")
        payload, sig = manifest["payload"], base64.b64decode(manifest["signature"], validate=True)
        if not isinstance(payload, str):
            raise ReleaseError("payload", "payload must be a string")
    except (KeyError, ValueError, TypeError):
        raise ReleaseError("format", "the manifest is malformed")
    if not any(ed25519.verify(k, payload.encode("utf-8"), sig) for k in public_keys):
        raise ReleaseError("signature", "the signature does not verify")
    try:
        p = json.loads(payload)
    except ValueError:
        raise ReleaseError("payload", "the payload is not JSON")
    if p.get("schema") != SCHEMA or p.get("app_id") != APP_ID:
        raise ReleaseError("app", "the manifest is not for this app")
    return p


class Releases:
    def __init__(self, db, data_dir, secrets_dir="/secrets", clock=time.time, hub=None):
        self.db, self.clock, self.hub = db, clock, hub
        self.dir = os.path.join(data_dir, "releases")
        self.secrets = secrets_dir
        os.makedirs(self.dir, mode=0o750, exist_ok=True)
        db.c.executescript(SCHEMA_SQL)

    # ── keys ───────────────────────────────────────────────────────────────────────────────────────────────────────
    def _seed(self):
        try:
            seed = bytes.fromhex(open(os.path.join(self.secrets, KEY_FILE)).read().strip())
        except (OSError, ValueError):
            raise ReleaseError("no_key", "the manifest signing key is missing: run tools/release_keygen.py on the host")
        if len(seed) != 32:
            raise ReleaseError("no_key", "the manifest signing key is malformed")
        return seed

    def public_key(self):
        try:
            return bytes.fromhex(open(os.path.join(self.secrets, PUB_FILE)).read().strip())
        except (OSError, ValueError):
            return None

    # ── reads ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def pinned_cert(self):
        return self.db.meta_get(META_CERT)

    def list(self):
        cur = self.db.meta_get(META_CURRENT)
        out = []
        for r in self.db.q("SELECT * FROM releases ORDER BY version_code DESC"):
            out.append({"version_code": r["version_code"], "version_name": r["version_name"], "size": r["size"], "sha256": r["sha256"], "cert_sha256": r["cert_sha256"], "notes": r["notes"],
                        "state": r["state"], "added_by": r["added_by"], "added_at": iso(r["added_at"]), "published_by": r["published_by"],
                        "published_at": iso(r["published_at"]) if r["published_at"] else None, "min_supported_version_code": r["min_supported"],
                        "current": cur is not None and int(cur) == r["version_code"]})
        return out

    def stopped(self):
        return self.db.meta_get(META_STOPPED) == "1"

    def apk_path(self, code):
        return os.path.join(self.dir, str(int(code)), "app.apk")

    def file_ok(self, code):
        """The stored file still has the recorded SHA-256 (recomputed now, never trusted from the record)."""
        r = self.db.q1("SELECT sha256, size FROM releases WHERE version_code=?", (int(code),))
        path = self.apk_path(code)
        if not r or not os.path.isfile(path) or os.path.getsize(path) != r["size"]:
            return False
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest() == r["sha256"]

    def current_manifest(self):
        """The signed manifest phones should see, or None (nothing published or the kill switch is on)."""
        if self.stopped():
            return None
        raw = self.db.meta_get(META_MANIFEST)
        return json.loads(raw) if raw else None

    def served_file(self, code):
        """The path of the published APK for `code`, only when it IS the current published release, the channel is not stopped and the file still matches its hash."""
        cur = self.db.meta_get(META_CURRENT)
        if self.stopped() or cur is None or int(cur) != int(code) or not self.file_ok(code):
            return None
        return self.apk_path(code)

    def health(self):
        """For /v1/health (the host watcher): is the channel sound?"""
        cur = self.db.meta_get(META_CURRENT)
        out = {"published": int(cur) if cur is not None else None, "stopped": self.stopped(), "pending": self.db.q1("SELECT COUNT(*) c FROM releases WHERE state='pending'")["c"],
               "published_at": None, "manifest_ok": None, "apk_ok": None, "key_ok": self.public_key() is not None}
        if cur is not None:
            r = self.db.q1("SELECT published_at FROM releases WHERE version_code=?", (int(cur),))
            out["published_at"] = iso(r["published_at"]) if r and r["published_at"] else None
            pub = self.public_key()
            try:
                raw = self.db.meta_get(META_MANIFEST)
                verify_manifest(json.loads(raw), [pub] if pub else [])
                out["manifest_ok"] = True
            except (ReleaseError, TypeError, ValueError):
                out["manifest_ok"] = False
            out["apk_ok"] = self.file_ok(int(cur))
        return out

    # ── writes ─────────────────────────────────────────────────────────────────────────────────────────────────────
    def pin(self, fingerprint, actor):
        fp = (fingerprint or "").strip().lower().replace(":", "")
        if not re.fullmatch(r"[0-9a-f]{64}", fp):
            raise ReleaseError("bad_fingerprint", "a SHA-256 fingerprint is 64 hex digits")
        old = self.pinned_cert()
        if old and old != fp:
            raise ReleaseError("already_pinned", "a different certificate is already pinned: it is part of the trust anchor and is never changed from here")
        self.db.meta_set(META_CERT, fp)
        self.db.audit("release_pin", person=actor.get("person_id"), target=fp[:16], ip=actor.get("ip"))
        return fp

    def add(self, path, notes, actor):
        """gwctl release add: verifies, copies, stores as pending. Refusals are ReleaseError with a stable code."""
        pinned = self.pinned_cert()
        if not pinned:
            raise ReleaseError("not_pinned", "no signing certificate is pinned yet: run `gwctl release pin <sha256 of the APK signing certificate>` first")
        if not isinstance(notes, str) or not 1 <= len(notes.strip()) <= 600:
            raise ReleaseError("notes", "the release notes (1-600 characters, Russian, plain words) are required")
        if not os.path.isfile(path):
            raise ReleaseError("no_file", "no such file")
        if os.path.getsize(path) > MAX_APK:
            raise ReleaseError("too_large", "the file is larger than %d MB" % (MAX_APK // 1048576))
        try:
            info = apkinfo.inspect(path)
        except apkinfo.ApkError as e:
            raise ReleaseError("bad_apk", str(e))
        if info["package"] != APP_ID:
            raise ReleaseError("wrong_app", "the package is %r, not %r" % (info["package"], APP_ID))
        if pinned not in info["certs"] or len(set(info["certs"])) != 1:
            raise ReleaseError("wrong_certificate", "the APK is not signed with the pinned certificate (found %s)" % ", ".join(c[:16] for c in info["certs"]))
        top = self.db.q1("SELECT MAX(version_code) m FROM releases")["m"]
        if top is not None and info["version_code"] <= top:
            raise ReleaseError("version_not_higher", "versionCode %d is not higher than the highest release so far (%d): roll forward only" % (info["version_code"], top))
        dest = self.apk_path(info["version_code"])
        os.makedirs(os.path.dirname(dest), mode=0o750, exist_ok=True)
        tmp = dest + ".tmp"
        shutil.copyfile(path, tmp)
        os.replace(tmp, dest)
        os.chmod(dest, 0o640)
        with self.db.tx():
            self.db.c.execute("INSERT INTO releases(version_code,version_name,size,sha256,cert_sha256,notes,state,added_by,added_at) VALUES (?,?,?,?,?,?,?,?,?)",
                              (info["version_code"], info["version_name"], info["size"], info["sha256"], pinned, notes.strip(), "pending", actor.get("person_id"), self.clock()))
        self.db.audit("release_added", person=actor.get("person_id"), target="%d %s" % (info["version_code"], info["version_name"]), after={"sha256": info["sha256"], "size": info["size"]}, ip=actor.get("ip"))
        return info

    def publish(self, code, actor, min_supported=None):
        """Signs the manifest NOW and makes this the current release. Only a pending release can be published."""
        r = self.db.q1("SELECT * FROM releases WHERE version_code=?", (int(code),))
        if not r:
            raise ReleaseError("not_found", "no such release")
        if r["state"] != "pending":
            raise ReleaseError("not_pending", "only a pending release can be published (this one is %s)" % r["state"])
        cur = self.db.meta_get(META_CURRENT)
        if cur is not None and int(code) <= int(cur):
            raise ReleaseError("version_not_higher", "a newer release is already published: roll forward only")
        if not self.file_ok(code):
            raise ReleaseError("file_mismatch", "the stored file does not match its recorded hash")
        mn = self.db.meta_get(META_MIN)
        mn = int(mn) if mn else 0
        if min_supported is not None:
            if isinstance(min_supported, bool) or not isinstance(min_supported, int) or not 0 <= min_supported <= int(code):
                raise ReleaseError("bad_min", "min_supported_version_code must be a whole number from 0 to this version_code")
            if min_supported < mn:
                raise ReleaseError("bad_min", "the minimum supported version never goes down (it is %d now)" % mn)
            mn = min_supported
        seed = self._seed()
        now = self.clock()
        payload = canonical({"schema": SCHEMA, "app_id": APP_ID, "key_id": KEY_ID, "version_name": r["version_name"], "version_code": r["version_code"], "min_supported_version_code": mn,
                             "apk_url": "/app/releases/%d/app.apk" % r["version_code"], "apk_sha256": r["sha256"], "apk_size": r["size"], "apk_signing_cert_sha256": r["cert_sha256"],
                             "changelog": r["notes"], "published_at": iso(now)})
        sig = ed25519.sign(seed, payload.encode("utf-8"))
        manifest = {"schema": SCHEMA, "key_id": KEY_ID, "payload": payload, "signature": base64.b64encode(sig).decode()}
        with self.db.tx():
            self.db.c.execute("UPDATE releases SET state='superseded' WHERE state='published'")
            self.db.c.execute("UPDATE releases SET state='published', published_by=?, published_at=?, min_supported=? WHERE version_code=?", (actor.get("person_id"), now, mn, int(code)))
            for k, v in ((META_CURRENT, str(int(code))), (META_MANIFEST, json.dumps(manifest)), (META_MIN, str(mn)), (META_STOPPED, "0")):
                self.db.c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
        self.db.audit("release_published", person=actor.get("person_id"), phone=actor.get("phone_id"), target="%d %s" % (r["version_code"], r["version_name"]),
                      after={"min_supported_version_code": mn}, ip=actor.get("ip"))
        if self.hub:
            self.hub.publish("release", {"version_code": r["version_code"], "version_name": r["version_name"]})
        return manifest

    def stop(self, actor):
        self.db.meta_set(META_STOPPED, "1")
        self.db.audit("release_stopped", person=actor.get("person_id"), phone=actor.get("phone_id"), ip=actor.get("ip"))

    def resume(self, actor):
        if self.db.meta_get(META_CURRENT) is None:
            raise ReleaseError("nothing_published", "nothing is published")
        self.db.meta_set(META_STOPPED, "0")
        self.db.audit("release_resumed", person=actor.get("person_id"), phone=actor.get("phone_id"), ip=actor.get("ip"))
