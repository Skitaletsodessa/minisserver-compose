#!/usr/bin/env python3
"""policy-drift-watch (Task 29): is the children's DNS policy in AdGuard really what the single source says it should be - and is the job that applies it alive?

Runs every 2 min on the host (it does not depend on the gateway or on the schedule service: the host job is the executor, so this watches the executor).
  expected   snapshot mode: the rules block rendered from the (verified) snapshot RIGHT NOW (and 75 s ago, the job runs once a minute and this may look a second early)
             files mode:    the hash the job reported for the block it applied (child-apply-status.json): detects a hand edit of the managed block in AdGuard
  reasons    the job's last run failed | the job has not run for more than 4 min | the managed block is missing | the live block differs from every acceptable block
  alerts     two consecutive runs with a reason: tier B "policy-drift" (one message, stays in the 08:00 heartbeat list)
             the same condition for 15 min: tier A "policy-drift-persistent" (children's filtering silently wrong or off is what Ivan must not miss: it repeats every 6 h)
             a run without a reason clears both. AdGuard not answering is `adguard-watch`'s alert, not this one (nothing is concluded from it).
The policy job keeps its own Telegram after 5 consecutive real failures (childpolicy._record_result); this watcher is the second line, independent of it.
"""
import datetime
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/srv/compose/_system/lib")
ADGUARD = os.environ.get("POLICY_DRIFT_ADGUARD", "/srv/compose/adguard")
sys.path.insert(0, ADGUARD)
# NOT *.json: watchnotify.active_detail() lists every *.json in this directory as an open alert, and this state has a "since" key (null when clean) - the 08:00 heartbeat of 2026-10-22 reported a phantom "policy-drift-watch since <now>" alert
STATE_FILE = os.environ.get("POLICY_DRIFT_STATE", "/var/lib/watch-alerts/policy-drift-watch.state")
TRANSIENT_RUNS = 2
PERSIST_S = 15 * 60
JOB_STALE_S = 4 * 60


def rules_hash(lines):
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(st, f)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


def reasons_of(obs):
    """The reasons the policy is not as it should be, from one observation. [] = fine. None = no conclusion possible (AdGuard did not answer)."""
    if not obs["agh_ok"]:
        return None
    out = []
    js = obs["job_status"]
    if js is None:
        out.append("the job has written no apply status")
    else:
        if js.get("ok") is False:
            out.append("the job's last run failed: %s" % (js.get("error") or "no reason given")[:160])
        if obs["job_age_s"] is not None and obs["job_age_s"] > JOB_STALE_S:
            out.append("the job has not run for %d min" % (obs["job_age_s"] // 60))
    if obs["live_hash"] is None:
        out.append("the managed block is missing in AdGuard")
    elif obs["expected_hashes"] and obs["live_hash"] not in obs["expected_hashes"]:
        out.append("the rules in AdGuard (%s) differ from the expected ones (%s, %s)" % (obs["live_hash"], "/".join(obs["expected_hashes"]), obs["expected_source"]))
    elif not obs["expected_hashes"]:
        out.append("nothing to compare with (%s)" % obs["expected_source"])
    return out


def evaluate(N, state, obs, now):
    """Pure decision logic. Returns the new state."""
    reasons = reasons_of(obs)
    if reasons is None:
        return state                                                      # AdGuard down: adguard-watch speaks; no conclusion, no state change
    if not reasons:
        if state.get("bad_runs"):
            N.clear("policy-drift", "")
            N.clear("policy-drift-persistent", "политика детского DNS снова совпадает с ожидаемой.")
        return {"bad_runs": 0, "since": None}
    state["bad_runs"] = state.get("bad_runs", 0) + 1
    state["since"] = state.get("since") or now
    text = "; ".join(reasons)
    if state["bad_runs"] >= TRANSIENT_RUNS:
        N.alert("policy-drift", "Детский DNS: правила в AdGuard расходятся с ожидаемыми (%s). Если это не проходит само - фильтрация детей может быть выключена." % text, tier="B")
    if now - state["since"] >= PERSIST_S:
        N.alert("policy-drift-persistent", "Детский DNS: расхождение держится уже %d мин (%s). Фильтрация детских устройств может не работать." % ((now - state["since"]) // 60, text), tier="A")
    return state


def observe(now_dt=None):
    import childpolicy
    import childsnapshot
    now_dt = now_dt or datetime.datetime.now(childpolicy.TZ)
    obs = {"agh_ok": False, "job_status": None, "job_age_s": None, "live_hash": None, "expected_hashes": [], "expected_source": "", "mode": childsnapshot.mode()}
    try:
        js = json.loads(Path(childsnapshot.STATUS).read_text())
        obs["job_status"] = js
        obs["job_age_s"] = max(0, int((now_dt - datetime.datetime.fromisoformat(js["at"])).total_seconds()))
    except (OSError, ValueError, KeyError):
        pass
    try:
        api = childpolicy.Api()
        rules = api.call("GET", "/filtering/status").get("user_rules") or []
        obs["agh_ok"] = True
    except Exception:                                         # noqa: BLE001 - AdGuard down or the password file unreadable: no conclusion
        return obs
    if childpolicy.BEGIN in rules and childpolicy.END in rules:
        obs["live_hash"] = rules_hash(rules[rules.index(childpolicy.BEGIN):rules.index(childpolicy.END) + 1])
    if obs["mode"] == "snapshot":
        loaded = childsnapshot.load()
        if loaded["snap"] is None:
            obs["expected_source"] = "no usable snapshot: %s" % loaded["live_problem"]
        else:
            sd = {t: childpolicy.service_domains(api, [t]) for t in ("telegram",) + tuple(childpolicy.GATED)}
            hs = []
            for at in (now_dt, now_dt - datetime.timedelta(seconds=75)):
                block, _ign = childsnapshot.render(sd, loaded["snap"], at)
                hs.append(rules_hash(block))
            obs["expected_hashes"], obs["expected_source"] = sorted(set(hs)), "snapshot " + loaded["snap"]["checksum"][:10]
    elif obs["job_status"] and obs["job_status"].get("rules_hash"):
        obs["expected_hashes"], obs["expected_source"] = [obs["job_status"]["rules_hash"]], "the hash the job reported"
    else:
        obs["expected_source"] = "no job status"
    return obs


def main():
    import watchnotify
    st = load_state()
    st = evaluate(watchnotify, st, observe(), time.time())
    save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
