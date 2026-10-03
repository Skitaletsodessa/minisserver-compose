#!/usr/bin/env python3
"""watchnotify - the ONE place every watchdog talks to Telegram through (Task 24 A).

Why it exists: on 2026-10-02/03 an alert fired once, nobody answered, and the fault ran for 26 h. Before this module every
watchdog sent a single message with `curl -s ... >/dev/null`: no record whether it was delivered, the "already alerted" flag
was set before the send (a failed send was never retried), and nothing ever repeated.

Behaviour:
  alert(key, text)  first call: send `text`, remember the outage. While the same key keeps being alerted: ONE reminder every
                    WATCH_REMIND_SECONDS (default 6 h) saying how long it has been failing. A send that failed is retried on the
                    next call (the outage is only marked "delivered" after Telegram answered ok:true).
  clear(key, text)  send `text` plus the outage duration, forget the outage. A failed send goes to an outbox and is retried.
  send(text)        one-off event message (no state), with delivery logging and an outbox on failure.
  Every send logs one journal line: "watchnotify: delivered message_id=N" or "watchnotify: FAILED ...". The token is never logged.

Environment overrides (tests): WATCH_TEST=1 (prefix "[TEST] "), WATCH_STATE_DIR, WATCH_REMIND_SECONDS, WATCH_ENV_FILE.
CLI:  watchnotify.py alert KEY TEXT | clear KEY TEXT | send TEXT | status | flush
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TEST = bool(os.environ.get("WATCH_TEST"))
TAG = "[TEST] " if TEST else ""
STATE_DIR = os.environ.get("WATCH_STATE_DIR") or ("/tmp/watch-alerts-test" if TEST else "/var/lib/watch-alerts")
ENV_FILE = os.environ.get("WATCH_ENV_FILE", "/srv/compose/scrutiny/.env")
REMIND = int(os.environ.get("WATCH_REMIND_SECONDS", "21600"))
OUTBOX = os.path.join(STATE_DIR, "outbox.jsonl")
OUTBOX_MAX_AGE = 48 * 3600


def _log(msg):
    print("watchnotify: " + msg, file=sys.stderr, flush=True)


def _creds():
    env = {}
    for line in open(ENV_FILE):
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"]


def _post(text):
    """One attempt. Returns (ok, detail). The token never appears in the detail."""
    try:
        token, chat = _creds()
    except (OSError, KeyError) as e:
        return False, f"no credentials ({type(e).__name__})"
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data), timeout=20) as r:
            body = json.load(r)
        if body.get("ok"):
            return True, f"message_id={body.get('result', {}).get('message_id')}"
        return False, f"telegram said ok=false: {str(body.get('description'))[:80]}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 - DNS failure, timeout, TLS ...
        return False, f"{type(e).__name__}: {str(e)[:80]}"


def _send_raw(text, attempts=3):
    if TEST:
        _log("TEXT >>> " + text.replace("\n", "\n    | "))
    for i in range(attempts):
        ok, detail = _post(text)
        if ok:
            _log("delivered " + detail)
            return True
        _log(f"attempt {i + 1}/{attempts} failed: {detail}")
        if i < attempts - 1:
            time.sleep(2 + 4 * i)
    _log("FAILED, message queued in the outbox")
    return False


def _queue(text):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(OUTBOX, "a") as f:
        f.write(json.dumps({"t": int(time.time()), "text": text}) + "\n")


def flush():
    """Re-send queued messages (a failed send, e.g. during a DNS outage, is not lost)."""
    try:
        lines = open(OUTBOX).read().splitlines()
    except OSError:
        return
    keep, now = [], time.time()
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if now - item["t"] > OUTBOX_MAX_AGE:
            continue
        if not _send_raw("(delayed %s) %s" % (_dur(now - item["t"]), item["text"]), attempts=1):
            keep.append(line)
    tmp = OUTBOX + ".tmp"
    with open(tmp, "w") as f:
        f.write("".join(l + "\n" for l in keep))
    os.replace(tmp, OUTBOX)


def _dur(seconds):
    seconds = int(seconds)
    h, m = seconds // 3600, (seconds % 3600) // 60
    return f"{h} h {m:02d} min" if h else f"{m} min"


def _state_file(key):
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
    return os.path.join(STATE_DIR, safe + ".json")


def _load(key):
    try:
        return json.load(open(_state_file(key)))
    except (OSError, ValueError):
        return None


def _save(key, st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = _state_file(key) + ".tmp"
    json.dump(st, open(tmp, "w"))
    os.replace(tmp, _state_file(key))


def send(text):
    """One-off event message with delivery logging; queued if it cannot be delivered."""
    flush()
    if not _send_raw(TAG + text):
        _queue(TAG + text)
        return False
    return True


def alert(key, text, force=False):
    """Call on EVERY check while the fault persists. Sends the first message, then a reminder every REMIND seconds."""
    flush()
    now = time.time()
    st = _load(key)
    if st is None:
        st = {"since": now, "last_sent": 0, "delivered": False, "reminders": 0, "text": text}
    st["text"] = text
    first = not st["delivered"]
    due = (not first) and (now - st["last_sent"] >= REMIND)
    if first or due or force:
        if first or force:
            msg = TAG + text
        else:
            st["reminders"] += 1
            since = time.strftime("%Y-%m-%d %H:%M", time.localtime(st["since"]))
            msg = f"{TAG}REMINDER #{st['reminders']}: still failing since {since} ({_dur(now - st['since'])}).\n{text}"
        if _send_raw(msg):
            st["last_sent"], st["delivered"] = now, True
        else:
            st["last_sent"] = 0 if first else st["last_sent"]       # retry on the next call
    _save(key, st)
    return st


def clear(key, text):
    """Call when the fault is gone. Sends the recovery message with the total outage duration."""
    flush()
    st = _load(key)
    if st is None:
        return False
    dur = _dur(time.time() - st["since"])
    msg = f"{TAG}{text}\n(outage lasted {dur}{', ' + str(st['reminders']) + ' reminder(s) sent' if st.get('reminders') else ''})"
    if not _send_raw(msg):
        _queue(msg)
    try:
        os.remove(_state_file(key))
    except OSError:
        pass
    return True


def active():
    """[(key, since_epoch, delivered)] for every outage currently open."""
    out = []
    try:
        names = os.listdir(STATE_DIR)
    except OSError:
        return out
    for n in sorted(names):
        if n.endswith(".json"):
            try:
                st = json.load(open(os.path.join(STATE_DIR, n)))
                out.append((n[:-5], st["since"], st.get("delivered", False)))
            except (OSError, ValueError, KeyError):
                pass
    return out


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "alert" and len(argv) >= 4:
        alert(argv[2], " ".join(argv[3:]))
    elif cmd == "clear" and len(argv) >= 4:
        clear(argv[2], " ".join(argv[3:]))
    elif cmd == "send" and len(argv) >= 3:
        return 0 if send(" ".join(argv[2:])) else 1
    elif cmd == "flush":
        flush()
    elif cmd == "status":
        act = active()
        print(f"{len(act)} alert(s) active")
        for k, since, delivered in act:
            print(f"  {k}: since {time.strftime('%F %H:%M', time.localtime(since))} ({_dur(time.time() - since)}), delivered={delivered}")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
