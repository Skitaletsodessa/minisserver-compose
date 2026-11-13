#!/usr/bin/env python3
"""Task 35 C2 (Ivan 2026-11-13: everything updates automatically; check after every update, roll back at once on a failure).

Weekly run (Sunday 11:00, minis-container-update.timer). For every entry of stacks.json, ONE AT A TIME:
  1. find a candidate by the entry's policy:   patch  = same major.minor, higher patch (same tag prefix/suffix, no pre-release)
                                               minor  = same major, higher minor/patch; a database dump is taken first (Immich)
                                               digest = the same tag moved to a new image (floating tags such as 12.4, 9)
                                               build  = our own image on a digest-pinned base: rebuild on the base tag's new digest, smoke test, deploy, roll back
                                               ha     = Home Assistant: newest stable release with patch >= 1 (skip x.y.0) that passes the isolated restore test
                                               manual = never touched here (Diun still announces new tags)
  2. pull the candidate; rewrite the image line of that stack's compose.yaml; `docker compose up -d --no-deps <service>`
  3. health: container running (and healthy where it has a HEALTHCHECK) plus the entry's own command, twice, 10 s apart; the restart counter must not move
  4. any failure: the compose file is restored byte for byte, the service recreated from the previous image (still present: nothing is pruned here), health checked again,
     ONE tier-B message; success: the compose change is committed and pushed (rule 6/9).
Every outcome is one line of container-updates.jsonl (read by post-update-run.sh for the summary line). The whole run takes a lock, refuses to run in quiet hours (21:00-08:00)
and while a vacuum plan is running unless --force. `--dry-run` only prints the plan. Never run with -x (no secrets are read here, but the habit matters).

usage: container-update.py [--dry-run] [--only NAME] [--force] [--conf FILE] [--log FILE]"""
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

DIR = os.path.dirname(os.path.abspath(__file__))
NOTIFY = os.environ.get("NOTIFY", "python3 /srv/compose/_system/lib/watchnotify.py").split()

# ── pure helpers (unit-tested) ────────────────────────────────────────────────────────────────────────────────────
VER = re.compile(r"^(v?)(\d+)\.(\d+)\.(\d+)(.*)$")


def parse_tag(tag):
    m = VER.match(tag)
    return (m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), m.group(5)) if m else None


def pick_candidate(current, tags, policy):
    """Highest tag newer than `current` that the policy allows, or None. Same prefix ('v') and same suffix ('-omnibus', '' ...) are required, so pre-releases (-rc1, -beta)
    and other flavours never match. patch: same major.minor. minor: same major."""
    c = parse_tag(current)
    if not c or policy not in ("patch", "minor"):
        return None
    best = None
    for t in tags:
        p = parse_tag(t)
        if not p or p[0] != c[0] or p[4] != c[4]:
            continue
        if p[1] != c[1]:                                             # never a new major
            continue
        if policy == "patch" and p[2] != c[2]:                      # patch: the minor stays too
            continue
        key = p[1:4]
        if key > c[1:4] and (best is None or key > best[0]):
            best = (key, t)
    return best[1] if best else None


def split_ref(ref):
    """'ghcr.io/x/y:1.2.3@sha256:abc' -> (repo 'ghcr.io/x/y', tag '1.2.3' or None, digest 'sha256:abc' or None)"""
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        ref, tag = ref.rsplit(":", 1)
    else:
        tag = None
    return ref, tag, digest


def build_ref(repo, tag, digest):
    return repo + (":" + tag if tag else "") + ("@" + digest if digest else "")


def image_line(text, image_repo):
    """The ONE `image:` line of the compose text that uses this repository (docker.io/ and library/ prefixes ignored). Returns (full line value, match) or None."""
    base = image_repo.replace("docker.io/", "")
    found = []
    for m in re.finditer(r"^(\s+image:\s*)(\S+)\s*$", text, re.M):
        if split_ref(m.group(2))[0].replace("docker.io/", "") == base:
            found.append(m)
    return found[0] if len(found) == 1 else None


def rewrite_compose(text, image_repo, new_ref):
    m = image_line(text, image_repo)
    if not m:
        raise ValueError("expected exactly one image line for %s" % image_repo)
    return text[:m.start(2)] + new_ref + text[m.end(2):]


def quiet_hours(now):
    return now.hour >= 21 or now.hour < 8


# ── registry / github (network) ───────────────────────────────────────────────────────────────────────────────────
def http_get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=dict({"User-Agent": "minis-container-update"}, **(headers or {})))
    return urllib.request.urlopen(req, timeout=timeout)


def registry_tags(repo, max_pages=40):
    """Tag list through the registry v2 API with the anonymous token flow (Docker Hub, ghcr.io, lscr.io -> ghcr.io)."""
    host, _, name = repo.partition("/")
    if "." not in host and ":" not in host:
        host, name = "docker.io", repo
    if host == "docker.io" and "/" not in name:
        name = "library/" + name
    base = {"docker.io": "https://registry-1.docker.io", "ghcr.io": "https://ghcr.io", "lscr.io": "https://ghcr.io"}.get(host)
    if not base and (host.startswith("127.") or host.startswith("localhost")):
        base = "http://" + host                                      # a local (test) registry: no token flow
    if not base:
        raise RuntimeError("unsupported registry " + host)
    # the challenge tells where to get a token
    try:
        http_get(base + "/v2/")
        token = None
    except urllib.error.HTTPError as e:
        ch = e.headers.get("Www-Authenticate", "")
        realm = re.search(r'realm="([^"]+)"', ch).group(1)
        svc = re.search(r'service="([^"]+)"', ch)
        url = "%s?scope=repository:%s:pull%s" % (realm, name, ("&service=" + svc.group(1)) if svc else "")
        token = json.load(http_get(url))["token"]
    headers = {"Authorization": "Bearer " + token} if token else {}
    tags, url = [], "%s/v2/%s/tags/list?n=1000" % (base, name)
    for _ in range(max_pages):
        r = http_get(url, headers)
        tags += json.load(r).get("tags") or []
        link = r.headers.get("Link", "")
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        if not m:
            break
        url = base + m.group(1) if m.group(1).startswith("/") else m.group(1)
    return tags


def github_releases(repo, n=30):
    return [r["tag_name"] for r in json.load(http_get("https://api.github.com/repos/%s/releases?per_page=%d" % (repo, n))) if not r.get("prerelease") and not r.get("draft")]


# ── docker (side effects) ─────────────────────────────────────────────────────────────────────────────────────────
def sh(cmd, cwd=None, timeout=600, shell=False):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, shell=shell)
    return r.returncode, (r.stdout + r.stderr).strip()


def compose(d, *args, timeout=900):
    return sh(["docker", "compose", *args], cwd=d, timeout=timeout)


def pull(ref):
    rc, out = sh(["docker", "pull", ref], timeout=1800)
    return rc == 0, out[-300:]


def local_digest(repo_tag):
    rc, out = sh(["docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", repo_tag])
    return out.split("@", 1)[1] if rc == 0 and "@" in out else None


def running_digest(container, repo):
    """Digest of the image a container runs (its RepoDigests entry for `repo`), or None."""
    rc, iid = sh(["docker", "inspect", "--format", "{{.Image}}", container])
    if rc != 0:
        return None
    rc, out = sh(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", iid])
    if rc != 0:
        return None
    try:
        ds = json.loads(out)
    except ValueError:
        return None
    base = repo.replace("docker.io/", "")
    for d in ds:
        if d.split("@", 1)[0].replace("docker.io/", "") == base:
            return d.split("@", 1)[1]
    return ds[0].split("@", 1)[1] if ds else None


def container_state(name):
    rc, out = sh(["docker", "inspect", "--format", "{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.RestartCount}}", name])
    if rc != 0:
        return None
    run, health, restarts = out.split("|")
    return {"running": run == "true", "health": health, "restarts": int(restarts)}


def wait_healthy(entry, timeout=None, sleep=time.sleep):
    """True when every container of the entry runs (healthy where it has a HEALTHCHECK), the entry's command passes, twice in a row 10 s apart, with no restart in between."""
    timeout = timeout or entry.get("timeout", 180)
    names = entry["containers"]
    start = time.time()
    base = {n: (container_state(n) or {}).get("restarts") for n in names}
    ok_streak = 0
    while time.time() - start < timeout:
        good = True
        for n in names:
            s = container_state(n)
            if not s or not s["running"] or s["health"] not in ("none", "healthy") or s["restarts"] != base[n]:
                good = False
        if good and entry.get("cmd"):
            rc, _ = sh(entry["cmd"], shell=True, timeout=60)
            good = rc == 0
        ok_streak = ok_streak + 1 if good else 0
        if ok_streak >= 2:
            return True
        sleep(10 if good else 5)
    return False


def log_line(path, d):
    d = dict({"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, **d)
    with open(path, "a") as f:
        f.write(json.dumps(d) + "\n")


def alert(key, text):
    r = subprocess.run([*NOTIFY, "alert", "--tier", "B", key, text], capture_output=True, text=True)
    if r.stdout.strip():
        print(r.stdout.strip())


def git_publish(path, msg):
    root = "/srv/compose"
    if not path.startswith(root + "/"):
        return
    import shlex
    sh("cd %s && git add %s && git commit -qm %s && git push -q" % (root, shlex.quote(path[len(root) + 1:]), shlex.quote(msg)), shell=True, timeout=120)


# ── one entry ─────────────────────────────────────────────────────────────────────────────────────────────────────
def candidate_for(entry, current_ref, cfg):
    """(new_ref, why) or (None, reason). Pulls the candidate to learn its digest."""
    first = entry["members"][0]
    repo, tag, digest = split_ref(current_ref)
    policy = entry["policy"]
    if policy == "manual":
        return None, "manual: " + entry.get("reason", "not automated")
    if policy in ("patch", "minor"):
        src = entry.get("source", "registry")
        if tag is None:
            return None, "no tag to compare"
        tags = github_releases(src.split(":", 1)[1]) if src.startswith("github:") else registry_tags(repo)
        new_tag = pick_candidate(tag, tags, policy)
        if not new_tag:
            return None, "current"
        ok, out = pull(build_ref(repo, new_tag, None))
        if not ok:
            return None, "pull of %s failed: %s" % (new_tag, out[-120:])
        d = local_digest(build_ref(repo, new_tag, None)) if digest else None
        return build_ref(repo, new_tag, d), "%s -> %s" % (tag, new_tag)
    if policy == "digest":
        ok, out = pull(build_ref(repo, tag, None))
        if not ok:
            return None, "pull of %s failed: %s" % (tag, out[-120:])
        d = local_digest(build_ref(repo, tag, None))
        cur = digest or running_digest(first.get("container", first["service"]), repo)         # an unpinned tag: compare with what really runs
        if not d or d == cur:
            return None, "current"
        return build_ref(repo, tag, d), "%s: new image %s" % (tag, d[:19])
    return None, "unknown policy " + policy


def update_own(entry, args, logpath):
    """Our own image (gateway, schedules, relay) on a base pinned by digest: take the new digest of the base tag, rebuild, smoke-test the NEW image before anything is touched,
    deploy with the health check, and on any failure put the previous image and the previous Dockerfile back."""
    name = entry["name"]
    d = entry["dir"]
    df = os.path.join(d, "Dockerfile")
    text = open(df).read()
    m = re.search(r"^FROM (\S+?)@(sha256:[0-9a-f]{64})\s*$", text, re.M)
    if not m:
        return {"service": name, "result": "skipped", "reason": "no digest-pinned FROM line"}
    old_dig = m.group(2)
    base_tag = entry["base_tag"]
    ok, out = pull(base_tag)
    if not ok:
        return {"service": name, "result": "skipped", "reason": "pull of %s failed" % base_tag}
    new_dig = local_digest(base_tag)
    if not new_dig or new_dig == old_dig:
        return {"service": name, "result": "current", "reason": "current"}
    why = "base %s: %s -> %s" % (base_tag, old_dig[7:19], new_dig[7:19])
    if args.dry_run:
        return {"service": name, "result": "would-update", "reason": why}
    rc, prev_id = sh(["docker", "inspect", "--format", "{{.Image}}", entry["containers"][0]])
    if rc != 0:
        return {"service": name, "result": "skipped", "reason": "the running container was not found"}
    prev_tag = split_ref(entry["image"])[0] + ":minis-prev"                  # keeps the running image alive (and is the manual rollback target)
    sh(["docker", "tag", prev_id, prev_tag])
    open(df, "w").write(text[:m.start(2)] + new_dig + text[m.end(2):])
    rc, out = compose(d, "build", entry["service"], timeout=1800)
    smoke = rc == 0 and sh(entry["smoke"].format(image=entry["image"]), shell=True, timeout=300)[0] == 0
    if not smoke:
        open(df, "w").write(text)
        sh(["docker", "tag", prev_tag, entry["image"]])
        alert("container-update-" + name, "rebuild of %s on the new base (%s) FAILED before deployment (build or smoke test); nothing was changed, the old image still runs. %s" % (name, why, out[-160:]))
        return {"service": name, "result": "failed", "reason": why + " (build/smoke failed, not deployed)"}
    rc, out = compose(d, "up", "-d", "--no-deps", "--force-recreate", entry["service"])
    if rc == 0 and wait_healthy(entry):
        git_publish(df, "auto-update: %s %s" % (name, why))
        return {"service": name, "result": "updated", "reason": why, "to": new_dig}
    open(df, "w").write(text)
    trc, tout = sh(["docker", "tag", prev_tag, entry["image"]])
    rc2, out2 = compose(d, "up", "-d", "--no-deps", "--force-recreate", entry["service"])
    back = trc == 0 and rc2 == 0 and wait_healthy(entry)
    print("rollback of %s: retag rc=%s, recreate rc=%s, healthy=%s" % (name, trc, rc2, back))
    alert("container-update-" + name, "%s on the new base (%s) FAILED its health check and was ROLLED BACK; the previous image is %s. Details: %s"
          % (name, why, "healthy again" if back else "NOT healthy - needs a look NOW", out[-160:]))
    return {"service": name, "result": "rolled-back" if back else "failed", "reason": why}


def update_entry(entry, args, logpath):
    if entry["policy"] == "build":
        return update_own(entry, args, logpath)
    d = entry["dir"]
    compose_path = os.path.join(d, "compose.yaml")
    text = open(compose_path).read()
    first = entry["members"][0]
    m = image_line(text, first["image"])
    if not m:
        return {"service": entry["name"], "result": "skipped", "reason": "image line not found"}
    current_ref = m.group(2)
    if entry["policy"] == "ha":
        return update_ha(entry, args, logpath, text, current_ref)
    new_ref, why = candidate_for(entry, current_ref, None)
    if not new_ref:
        return {"service": entry["name"], "result": "current" if why == "current" else "skipped", "reason": why}
    if args.dry_run:
        return {"service": entry["name"], "result": "would-update", "reason": why, "to": new_ref}
    return apply_update(entry, text, compose_path, current_ref, new_ref, why, logpath)


def apply_update(entry, old_text, compose_path, current_ref, new_ref, why, logpath):
    name = entry["name"]
    # all members move to the same tag (e.g. Immich server and machine learning)
    new_text = old_text
    for mem in entry["members"]:
        mm = image_line(new_text, mem["image"])
        if mm is None:
            return {"service": name, "result": "skipped", "reason": "member image line not found: " + mem["image"]}
        repo, tag, digest = split_ref(mm.group(2))
        nrepo, ntag, ndig = split_ref(new_ref)
        if repo != nrepo:
            ok, out = pull(build_ref(repo, ntag, None))
            if not ok:
                return {"service": name, "result": "skipped", "reason": "pull for member %s failed" % mem["image"]}
            ndig = local_digest(build_ref(repo, ntag, None)) if digest else None
        new_text = rewrite_compose(new_text, mem["image"], build_ref(repo, ntag, ndig))
    if entry.get("before_update"):
        rc, out = sh(entry["before_update"], shell=True, timeout=1800)
        if rc != 0:
            return {"service": name, "result": "skipped", "reason": "pre-update step failed (%s): %s" % (entry["before_update"], out[-100:])}
    prev_tags = {}                                                             # repo -> its minis-prev tag: keeps the running image alive and re-points a floating tag on rollback
    for mem in entry["members"]:
        rc, out = sh(["docker", "inspect", "--format", "{{.Image}}", mem.get("container", mem["service"])])
        if rc == 0:
            mrepo = split_ref(image_line(old_text, mem["image"]).group(2))[0]
            sh(["docker", "tag", out, mrepo + ":minis-prev"])
            prev_tags[mem["image"]] = mrepo
    open(compose_path, "w").write(new_text)
    d = os.path.dirname(compose_path)
    services = [m["service"] for m in entry["members"]]
    rc, out = compose(d, "up", "-d", "--no-deps", *services)
    healthy = rc == 0 and wait_healthy(entry)
    if healthy:
        git_publish(compose_path, "auto-update: %s %s" % (name, why))
        return {"service": name, "result": "updated", "reason": why, "to": new_ref, "from": current_ref}
    # roll back at once
    open(compose_path, "w").write(old_text)
    for mem in entry["members"]:                                               # a floating tag (12.4, 9) now points at the NEW image: point it back at the old one
        if mem["image"] in prev_tags:
            orepo, otag, _d = split_ref(image_line(old_text, mem["image"]).group(2))
            if otag:
                sh(["docker", "tag", prev_tags[mem["image"]] + ":minis-prev", orepo + ":" + otag])
    rc2, out2 = compose(d, "up", "-d", "--no-deps", "--force-recreate", *services)
    back = rc2 == 0 and wait_healthy(entry, timeout=entry.get("timeout", 180))
    alert("container-update-" + name, "container update of %s (%s) FAILED its health check and was ROLLED BACK to %s; the previous version is %s again. %s Details: %s"
          % (name, why, current_ref, "healthy" if back else "NOT healthy - needs a look NOW", "" if back else "(rollback also failed)", out[-160:]))
    return {"service": name, "result": "rolled-back" if back else "failed", "reason": why, "to": new_ref, "from": current_ref}


def update_ha(entry, args, logpath, text, current_ref):
    """Home Assistant: newest stable release with patch >= 1 that is newer than the running one and passes restore-test-ha.sh with the candidate image."""
    name = entry["name"]
    rc, out = sh(["docker", "inspect", "--format", '{{index .Config.Labels "org.opencontainers.image.version"}}', entry["containers"][0]])
    cur_ver = out.strip() if rc == 0 else ""
    cp = parse_tag(cur_ver) if cur_ver else None
    cands = [t for t in github_releases("home-assistant/core") if re.match(r"^\d{4}\.\d+\.\d+$", t)]
    best = None
    for t in cands:
        p = parse_tag(t)
        if p and p[3] >= 1 and cp and p[1:4] > cp[1:4] and (best is None or p[1:4] > best[0]):
            best = (p[1:4], t)
    if not best:
        return {"service": name, "result": "current", "reason": "running %s, no stable x.y.1+ release is newer" % cur_ver}
    ref = "ghcr.io/home-assistant/home-assistant:" + best[1]
    ok, out = pull(ref)
    if not ok:
        return {"service": name, "result": "skipped", "reason": "pull of %s failed" % best[1]}
    dig = local_digest(ref)
    new_ref = "ghcr.io/home-assistant/home-assistant@" + dig
    why = "%s -> %s" % (cur_ver, best[1])
    if args.dry_run:
        return {"service": name, "result": "would-update", "reason": why + " (after the restore test)", "to": new_ref}
    rc, out = sh("HA_IMAGE=%s /srv/compose/_system/restic-backup/restore-test-ha.sh" % new_ref, shell=True, timeout=3000)
    if rc != 0:
        alert("container-update-" + name, "Home Assistant %s was NOT installed: the isolated restore test failed with the candidate image (%s). %s" % (best[1], why, out[-200:]))
        return {"service": name, "result": "skipped", "reason": "restore test failed for " + best[1]}
    return apply_update(entry, text, os.path.join(entry["dir"], "compose.yaml"), current_ref, new_ref, why, logpath)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-post", action="store_true", help="do not run post-update-run.sh afterwards (tests)")
    ap.add_argument("--conf", default=os.path.join(DIR, "stacks.json"))
    ap.add_argument("--log", default=os.path.join(DIR, "container-updates.jsonl"))
    args = ap.parse_args()
    now = datetime.now()
    if not args.dry_run and not args.force and quiet_hours(now):
        print("quiet hours (21:00-08:00): not running without --force")
        return 0
    lock = open("/tmp/minis-container-update.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another container update run holds the lock")
        return 0
    entries = json.load(open(args.conf))["entries"]
    results = []
    for e in entries:
        if args.only and e["name"] != args.only:
            continue
        try:
            r = update_entry(e, args, args.log)
        except Exception as ex:  # noqa: BLE001 - one entry must never stop the others
            r = {"service": e["name"], "result": "skipped", "reason": "error %s: %s" % (type(ex).__name__, str(ex)[:150])}
        results.append(r)
        print("%-18s %-12s %s" % (r["service"], r["result"], r.get("reason", "")), flush=True)
        if not args.dry_run and r["result"] in ("updated", "rolled-back", "failed"):
            log_line(args.log, r)
    changed = [r for r in results if r["result"] in ("updated", "rolled-back", "failed")]
    if changed and not args.dry_run and not args.no_post:
        subprocess.run([os.path.join(DIR, "post-update-run.sh"), "containers"], capture_output=True, text=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
