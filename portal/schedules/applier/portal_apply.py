#!/usr/bin/env python3
"""portal-apply: the host-side applier of ADR 2. The schedule service writes /srv/apps/portal/schedules/out/ (render + READY marker);
this program puts the rendered files into Home Assistant's generated/ directory, with every step checked:

  1. the render is complete (READY matches render.json, every file's sha256 matches);
  2. the files are written into generated/ (atomic per file; the directory itself is never replaced: it is a bind mount);
  3. HA's own check_config accepts the whole configuration;                     -> else the previous files come back
  4. a local-only webhook automation reloads automations + scripts (no restart; there is no admin token anywhere);
  5. VERIFY: what HA loaded (aliases carry a content hash, read from the recorder database like ha-alerts does) must
     equal what was rendered, within a timeout;                                  -> else rollback + reload + alert
  6. the generated files are committed to /srv/compose and pushed (HA starts from git alone even if the portal data are lost);
  7. apply-status.json is written; the service audits it ("applied" / "not_applied").

Run with no arguments: apply if the render differs from what is applied, otherwise just re-verify (mismatch detection, every 5 min).
Never run with `bash -x`: the webhook id is read from a mode-600 file and must not be printed.
"""
import os
import re
import sys
import json
import time
import fcntl
import sqlite3
import hashlib
import subprocess
import datetime as dt
import urllib.request
import urllib.error

sys.path.insert(0, "/srv/compose/_system/lib")

DEFAULTS = {
    "out": "/srv/apps/portal/schedules/out",
    "gen": "/srv/compose/homeassistant/generated",
    "status": "/srv/apps/portal/schedules/apply-status.json",
    "state": "/srv/apps/portal/applier",
    "webhook_file": "/srv/apps/portal/applier/webhook-id",
    "ha_url": "http://127.0.0.1:8123",
    "ha_db": "/srv/apps/homeassistant/home-assistant_v2.db",
    "git_repo": "/srv/compose",
    "git_path": "homeassistant/generated",
    "verify_timeout": 150,
    "verify_every": 5,
}


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sha(b):
    return hashlib.sha256(b).hexdigest()


ANSI = re.compile(chr(27) + re.escape("[") + "[0-9;]*m")
BAD_LINE = re.compile("^ *(ERROR|CRITICAL)([^A-Za-z]|$)|Failed config")


def judge_check_config(returncode, output):
    """HA's check_config exits 0 even when an individual automation is invalid ("... has been disabled" is only an ERROR log line),
    which the Task 27 rehearsal on a real HA showed. A clean run prints just 'Testing configuration at /config'; any ERROR/CRITICAL
    line or 'Failed config' makes it a failure. Returns (ok, short reason)."""
    text = ANSI.sub("", output)
    bad = [l.strip() for l in text.splitlines() if BAD_LINE.search(l)]
    if returncode == 0 and not bad:
        return True, ""
    tail = bad[:3] or [l.strip() for l in text.splitlines() if l.strip()][-3:]
    return False, ("rc=%s: " % returncode + " | ".join(tail))[:400]


class Applier:
    def __init__(self, cfg=None, log=print):
        self.c = {**DEFAULTS, **(cfg or {})}
        self.log = log
        self.sleep = time.sleep

    # ── hooks (replaced in tests) ─────────────────────────────────────────────
    def alert(self, key, text, clear=False):
        import watchnotify
        if clear:
            watchnotify.clear(key, text)
        else:
            watchnotify.alert(key, text, tier="B")

    def check_config(self):
        r = subprocess.run(["docker", "exec", "homeassistant", "python", "-m", "homeassistant", "--script", "check_config", "-c", "/config"],
                           capture_output=True, text=True, timeout=240)
        return judge_check_config(r.returncode, r.stdout + r.stderr)

    def reload(self):
        try:
            with open(self.c["webhook_file"]) as f:
                wid = f.read().strip()
        except OSError as e:
            return False, "webhook id unreadable (%s)" % e.__class__.__name__
        req = urllib.request.Request("%s/api/webhook/%s" % (self.c["ha_url"], wid), data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status == 200, "HTTP %d" % r.status
        except urllib.error.HTTPError as e:
            return False, "HTTP %d" % e.code
        except (urllib.error.URLError, OSError) as e:
            return False, "HA unreachable (%s)" % e.__class__.__name__

    def loaded(self):
        """What HA currently has loaded, from the recorder: (set of automation aliases, {script key: alias}).
        The recorder does not keep an automation's `id` attribute (HA marks it unrecorded), so automations are identified by their alias -
        which carries the content hash. An entity whose last state is empty/unavailable/unknown is gone (removed by a reload)."""
        conn = sqlite3.connect("file:%s?mode=ro" % self.c["ha_db"], uri=True, timeout=15)
        try:
            rows = conn.execute("""
                SELECT m.entity_id, s.state, a.shared_attrs FROM states_meta m
                JOIN states s ON s.state_id = (SELECT state_id FROM states WHERE metadata_id = m.metadata_id ORDER BY last_updated_ts DESC LIMIT 1)
                LEFT JOIN state_attributes a ON a.attributes_id = s.attributes_id
                WHERE m.entity_id LIKE 'automation.%' OR m.entity_id LIKE 'script.%'""").fetchall()
        finally:
            conn.close()
        autos, scripts = set(), {}
        for eid, state, attrs in rows:
            if state in (None, "", "unavailable", "unknown") or not attrs:
                continue
            try:
                name = json.loads(attrs).get("friendly_name")
            except ValueError:
                continue
            if eid.startswith("automation."):
                autos.add(name)
            else:
                scripts[eid[len("script."):]] = name
        return autos, scripts

    def git_commit_push(self, message):
        repo, path = self.c["git_repo"], self.c["git_path"]
        run = lambda *a, t=60: subprocess.run(["git", "-C", repo] + list(a), capture_output=True, text=True, timeout=t)
        run("add", "-A", "--", path)
        if run("diff", "--cached", "--quiet", "--", path).returncode == 0:
            return True, "nothing to commit"
        r = run("commit", "-q", "-m", message + "\n\nCo-Authored-By: portal-apply <noreply@minisserver>", "--", path)
        if r.returncode != 0:
            return False, "commit failed: " + r.stderr.strip()[:200]
        r = run("push", "-q", t=90)
        return (r.returncode == 0), ("pushed" if r.returncode == 0 else "push failed: " + r.stderr.strip()[:200])

    # ── files ─────────────────────────────────────────────────────────────────
    def read_render(self):
        out = self.c["out"]
        with open(os.path.join(out, "render.json"), encoding="utf-8") as f:
            meta = json.load(f)
        with open(os.path.join(out, "READY"), encoding="utf-8") as f:
            seq, h = f.read().split()
        if int(seq) != meta["audit_seq"] or h != meta["set_hash"]:
            raise ValueError("READY does not match render.json (a render is being written)")
        files = {}
        for rel, digest in meta["files"].items():
            with open(os.path.join(out, "render", rel), "rb") as f:
                b = f.read()
            if sha(b) != digest:
                raise ValueError("rendered file %s does not match its recorded hash" % rel)
            files[rel] = b
        return meta, files

    def read_gen(self):
        gen, files = self.c["gen"], {}
        for root, _d, names in os.walk(gen):
            for n in names:
                p = os.path.join(root, n)
                rel = os.path.relpath(p, gen).replace(os.sep, "/")
                if n.startswith(".") or ".tmp." in n:
                    continue
                with open(p, "rb") as f:
                    files[rel] = f.read()
        return files

    def write_gen(self, files):
        """Make generated/ equal `files` (rel path -> bytes). Per-file atomic rename; the directory is never replaced."""
        gen = self.c["gen"]
        os.makedirs(gen, exist_ok=True)
        have = self.read_gen()
        for rel, data in files.items():
            if have.get(rel) == data:
                continue
            p = os.path.join(gen, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = "%s.tmp.%d" % (p, os.getpid())
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        for rel in set(have) - set(files):
            os.remove(os.path.join(gen, rel))

    def status(self):
        try:
            with open(self.c["status"], encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def write_status(self, **kw):
        st = {**self.status(), **kw}
        tmp = self.c["status"] + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(tmp, self.c["status"])
        return st

    # ── verification ──────────────────────────────────────────────────────────
    @staticmethod
    def compare(expected, autos, scripts):
        """Returns a list of human-readable differences (empty = what HA loaded equals what was rendered).
        Generated items are recognised by their alias shape 'Sch <...> [sch xxxxxxxx]' / script key 'sch_*'."""
        d = []
        want = set(expected["automations"].values())
        for k, alias in expected["automations"].items():
            if alias not in autos:
                d.append("automation %s: '%s' is not loaded" % (k, alias))
        for k, alias in expected["scripts"].items():
            if scripts.get(k) != alias:
                d.append("script %s: expected '%s', loaded '%s'" % (k, alias, scripts.get(k)))
        for alias in sorted(x for x in autos if isinstance(x, str) and x.startswith("Sch ") and " [sch " in x):
            if alias not in want:
                d.append("automation '%s' is loaded but not rendered" % alias)
        for k in sorted(scripts):
            if k.startswith("sch_") and k not in expected["scripts"]:
                d.append("script %s is loaded but not rendered" % k)
        return d

    def verify_loaded(self, expected, timeout=None):
        """Polls until the loaded set equals `expected` or the timeout passes. Returns (ok, differences)."""
        timeout = self.c["verify_timeout"] if timeout is None else timeout
        end, diffs = time.time() + timeout, ["not checked"]
        while True:
            try:
                autos, scripts = self.loaded()
                diffs = self.compare(expected, autos, scripts)
            except sqlite3.Error as e:
                diffs = ["recorder database unreadable (%s)" % e.__class__.__name__]
            if not diffs:
                return True, []
            if time.time() >= end:
                return False, diffs
            self.sleep(self.c["verify_every"])

    # ── main flow ─────────────────────────────────────────────────────────────
    def apply(self, force=False):
        """One pass. Returns a short result string; details go to apply-status.json and the log."""
        os.makedirs(self.c["state"], exist_ok=True)
        try:
            meta, files = self.read_render()
        except (OSError, ValueError) as e:
            self.log("no complete render yet: %s" % e)
            return "no-render"
        st = self.status()
        current = self.read_gen()
        same_files = current == files
        if same_files and st.get("set_hash") == meta["set_hash"] and st.get("result") == "applied":
            return self.reverify(meta)
        if st.get("result") == "failed" and st.get("set_hash") == meta["set_hash"] and not force:
            age = time.time() - dt.datetime.fromisoformat(st["at"]).timestamp() if st.get("at") else 1e9
            if age < 3600:          # the same render failed a moment ago: wait for a new render (or an hour) instead of reloading HA every 5 min
                self.log("render #%d failed %d min ago; not retrying until it changes" % (meta["audit_seq"], age // 60))
                return "failed-held"
        t0 = now_iso()
        self.log("applying render #%d set %s (%d files)" % (meta["audit_seq"], meta["set_hash"], len(files)))
        previous = current
        prev_expected = self.last_good()
        self.write_status(seq=meta["audit_seq"], set_hash=meta["set_hash"], result="pending", at=t0, detail="applying", loaded_ok=False)
        self.write_gen(files)
        ok, why = self.check_config()
        if not ok:
            return self.fail(meta, previous, prev_expected, "check_config rejected the generated files: " + why, reload_needed=False)
        ok, why = self.reload()
        if not ok:
            # HA down or webhook missing: not a rollback case if the files are valid; keep them, stay pending, retry on the next timer tick
            self.write_status(result="pending", detail="reload not done (%s); will retry" % why, at=now_iso(), loaded_ok=False)
            self.pending_alert(meta, why)
            return "pending"
        ok, diffs = self.verify_loaded(meta["expected"])
        if not ok:
            return self.fail(meta, previous, prev_expected, "HA did not load what was rendered: " + "; ".join(diffs[:3]), reload_needed=True)
        pushed, pmsg = self.git_commit_push("portal: apply render #%d (set %s)" % (meta["audit_seq"], meta["set_hash"]))
        self.save_good(meta)
        self.write_status(seq=meta["audit_seq"], set_hash=meta["set_hash"], result="applied", at=t0, loaded_ok=True,
                          verified_at=now_iso(), detail="applied; git: " + pmsg, pending_since=None)
        self.alert("portal-apply", "portal: render #%d applied again" % meta["audit_seq"], clear=True)
        self.alert("portal-pending", "portal: pending render applied", clear=True)
        if pushed:
            self.alert("portal-push", "portal: generated config pushed", clear=True)
        else:
            self.alert("portal-push", "portal: the generated HA config is applied but NOT pushed to GitHub (%s). Run: git -C /srv/compose push" % pmsg)
        self.log("applied #%d; %s" % (meta["audit_seq"], pmsg))
        return "applied"

    def fail(self, meta, previous, prev_expected, why, reload_needed):
        """Roll the files back to what was there, make HA load that again, say so loudly."""
        self.log("FAILED: %s -- rolling back" % why)
        self.write_gen(previous)
        rolled = "files restored"
        if reload_needed:
            ok, rwhy = self.reload()
            rolled += "; reload %s" % ("done" if ok else "FAILED (%s)" % rwhy)
            if ok and prev_expected:
                vok, vd = self.verify_loaded(prev_expected)
                rolled += "; previous set %s" % ("verified" if vok else "NOT verified: " + "; ".join(vd[:2]))
        self.write_status(seq=meta["audit_seq"], set_hash=meta["set_hash"], result="failed", at=now_iso(), loaded_ok=False,
                          detail="%s (%s)" % (why, rolled))
        self.alert("portal-apply", "portal: render #%d NOT applied - %s. Rollback: %s. The schedule in the service differs from what Home Assistant runs; "
                   "undo the change (schedctl undo) or fix the cause." % (meta["audit_seq"], why, rolled))
        return "failed"

    def pending_alert(self, meta, why):
        st = self.status()
        since = st.get("pending_since")
        if not since:
            since = time.time()
            self.write_status(pending_since=since)
        if time.time() - since > 15 * 60:
            self.alert("portal-pending", "portal: render #%d has been waiting %d min to be applied (%s)" % (meta["audit_seq"], (time.time() - since) // 60, why))

    def reverify(self, meta):
        """Nothing new to apply: check that Home Assistant still runs what was applied (a restart that lost the files, a hand edit...)."""
        ok, diffs = self.verify_loaded(meta["expected"], timeout=0)
        st = self.status()
        if ok:
            self.write_status(loaded_ok=True, verified_at=now_iso(), mismatch_count=0)
            self.alert("portal-mismatch", "portal: Home Assistant runs the rendered schedule again", clear=True)
            return "verified"
        n = int(st.get("mismatch_count", 0)) + 1
        self.write_status(loaded_ok=False, mismatch_count=n, detail="mismatch: " + "; ".join(diffs[:3]))
        self.log("MISMATCH (%d): %s" % (n, "; ".join(diffs[:3])))
        if n >= 2:             # one bad read can be a restart in progress; two in a row, five minutes apart, is a real difference
            self.alert("portal-mismatch", "portal: Home Assistant does NOT run the rendered schedule: %s" % "; ".join(diffs[:3]))
        return "mismatch"

    def last_good(self):
        try:
            with open(os.path.join(self.c["state"], "last-good-expected.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def save_good(self, meta):
        p = os.path.join(self.c["state"], "last-good-expected.json")
        with open(p + ".tmp", "w") as f:
            json.dump(meta["expected"], f, sort_keys=True)
        os.replace(p + ".tmp", p)


def main():
    cfg = dict(DEFAULTS)
    os.makedirs(cfg["state"], exist_ok=True)
    lock = open(os.path.join(cfg["state"], "lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another apply is running")
        return 0
    res = Applier(cfg).apply(force="--force" in sys.argv)
    print("portal-apply:", res)
    return 0 if res in ("applied", "verified", "no-render", "pending", "failed-held") else 1


if __name__ == "__main__":
    sys.exit(main())
