"""Task 29 Part 2: the host policy job's snapshot side (ADR 0008 (a)).

The schedule service writes `out/child-policy.json` (atomic, checksum) on every change. The job - not the service, not the gateway - renders AdGuard's rules
from it, evaluates TIME itself (windows by the clock, exceptions by their absolute expiry) and so keeps switching at the right minute when the service is down.

  * the snapshot is verified against the invariants (sched/child_invariants.py, shared code of the repository) before it is used; a snapshot that breaks one
    is REFUSED and the last good copy keeps being used (second enforcement, like the plugs' fail-safe OFF);
  * a good snapshot is copied to /var/lib/child-override/last-good-snapshot.json;
  * every run writes child-apply-status.json (into the service's data directory, read by the service's /v1/health and the gateway's parental health): which
    snapshot, the hash of the rules block it produced, when, and why not if it failed;
  * `source.conf` in child-policy/ says which source drives AdGuard: `files` (the old text files + overrides.json) or `snapshot`. In `files` mode the job still
    renders the block from the snapshot WITH THE SAME overrides and compares (the shadow run of Phase H); it never changes what is enforced.
"""
import datetime
import hashlib
import json
import os
import sys
import time
from pathlib import Path

PORTAL = os.environ.get("PORTAL_SRC", "/srv/compose/portal/schedules")
if PORTAL not in sys.path:
    sys.path.insert(0, PORTAL)
from sched import child_render, child_invariants  # noqa: E402

HERE = Path(__file__).resolve().parent
MODE_FILE = Path(os.environ.get("CHILD_SOURCE_FILE", str(HERE / "child-policy" / "source.conf")))
SNAPSHOT = Path(os.environ.get("CHILD_SNAPSHOT", "/srv/apps/portal/schedules/out/child-policy.json"))
STATE_DIR = Path(os.environ.get("CHILD_STATE_DIR", "/var/lib/child-override"))
LAST_GOOD = STATE_DIR / "last-good-snapshot.json"
STATUS = Path(os.environ.get("CHILD_APPLY_STATUS", "/srv/apps/portal/schedules/child-apply-status.json"))
SHADOW = STATE_DIR / "shadow.json"
SNAPSHOT_MAX_AGE_WARN_S = 24 * 3600


def mode():
    try:
        lines = [ln.split("#", 1)[0].strip() for ln in MODE_FILE.read_text().splitlines()]
    except OSError:
        return "files"
    m = next((ln for ln in lines if ln), "files")
    return m if m in ("files", "snapshot") else "files"            # anything unexpected means the proven old path


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _checksum_ok(snap):
    core = {k: v for k, v in snap.items() if k not in ("generated_at", "audit_seq", "checksum")}
    return hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest() == snap.get("checksum")


def load():
    """Returns {"snap", "source": live|last_good|None, "problems": [...], "live_problem": str|None}. Never raises."""
    live_problem = None
    try:
        snap = _read(SNAPSHOT)
        probs = [] if _checksum_ok(snap) else [{"code": "checksum", "message": "the checksum does not match the content"}]
        probs += child_invariants.verify_snapshot(snap)
        if not probs:
            try:
                STATE_DIR.mkdir(parents=True, exist_ok=True)
                tmp = LAST_GOOD.with_suffix(".tmp")
                tmp.write_text(json.dumps(snap), encoding="utf-8")
                os.replace(tmp, LAST_GOOD)
            except OSError:
                pass
            return {"snap": snap, "source": "live", "problems": [], "live_problem": None}
        live_problem = "; ".join(p["message"] for p in probs[:3])
    except (OSError, ValueError) as e:
        live_problem = "%s: %s" % (e.__class__.__name__, e)
    try:
        snap = _read(LAST_GOOD)
        if not child_invariants.verify_snapshot(snap) and _checksum_ok(snap):
            return {"snap": snap, "source": "last_good", "problems": [], "live_problem": live_problem}
    except (OSError, ValueError):
        pass
    return {"snap": None, "source": None, "problems": [], "live_problem": live_problem}


def policy_from(snap, ignore_denies=True):
    """The model child_render.render_block takes, from a (verified) snapshot. Defensive: a deny that would block an always-on host is dropped and reported."""
    ignored = []
    devices = []
    for d in snap["devices"]:
        deny = []
        for dom in d.get("deny_extra") or []:
            if ignore_denies and child_invariants.deny_breaks_baseline(dom):
                ignored.append("%s: %s" % (d["id"], dom))
            else:
                deny.append(dom)
        devices.append({"id": d["id"], "name": d["name"], "ip": d["ip"], "mac": d.get("mac") or "", "rules": d["rules"],
                        "allow_extra": d.get("allow_extra") or [], "deny_extra": deny})
    return {"devices": devices, "lists": snap["lists"], "always_services": snap["always_services"]}, ignored


def active_overrides(snap, now_ts):
    out = []
    for d in snap["devices"]:
        for x in d.get("exceptions") or []:
            if x["expires_ts"] > now_ts:
                out.append({"client": d["ip"], "target": x["target"]})
    return out


def clients_from(snap):
    return [(d["ip"], d["name"], d.get("mac") or "") for d in snap["devices"]]


def rules_hash(lines):
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


def render(service_domains, snap, now, overrides=None):
    """Block lines from a snapshot. overrides=None: the snapshot's own active exceptions (by `now`); a list: those instead (the shadow comparison)."""
    policy, ignored = policy_from(snap)
    ov = active_overrides(snap, now.timestamp()) if overrides is None else overrides
    return child_render.render_block(policy, service_domains, now, ov), ignored


def write_status(**fields):
    """child-apply-status.json: read by the service (/v1/health) and the gateway. Atomic, best effort (a failure here must never break the job)."""
    body = {"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), **fields}
    try:
        tmp = STATUS.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, STATUS)
    except OSError:
        pass


def shadow_record(same, now, detail=None, snapshot_checksum=None):
    """Counts of the files-vs-snapshot comparison of the shadow period (one line of state, not a log)."""
    try:
        st = _read(SHADOW)
    except (OSError, ValueError):
        st = {"checked": 0, "mismatches": 0, "first_checked": now.isoformat(timespec="seconds"), "last_mismatch": None}
    st["checked"] += 1
    st["last_checked"] = now.isoformat(timespec="seconds")
    st["snapshot_checksum"] = snapshot_checksum
    if not same:
        st["mismatches"] += 1
        st["last_mismatch"] = {"at": now.isoformat(timespec="seconds"), "detail": detail}
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SHADOW.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, SHADOW)
    except OSError:
        pass
    return st
