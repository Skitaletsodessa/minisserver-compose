"""Temporary exceptions to the children's DNS policy (Task 20).

An exception opens one target for one child device until a deadline, then expires on its
own (the policy timer re-renders every minute and drops expired ones). State is a small
JSON file; it is runtime state, not configuration, so it is not in git or the backup set.
"""
import datetime
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Prague")
STATE_DIR = Path("/var/lib/child-override")
STATE = STATE_DIR / "overrides.json"

# target id -> label shown to people. "all" = no restrictions at all for that device
# (the house-wide ad/tracker lists still apply).
TARGETS = {
    "youtube": "YouTube",
    "roblox": "Roblox",
    "maps": "Google Maps",
    "all": "Всё (без ограничений)",
}


def _now(now=None):
    now = now or datetime.datetime.now(TZ)
    return now if now.tzinfo else now.replace(tzinfo=TZ)


def _read():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _write(entries):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE)


def active(now=None):
    now = _now(now)
    return [e for e in _read() if datetime.datetime.fromisoformat(e["expires"]) > now]


def add(client, target, minutes=None, now=None):
    """minutes=None means until the end of today (Prague time)."""
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}; choose from {', '.join(TARGETS)}")
    now = _now(now)
    if minutes is None:
        expires = now.replace(hour=23, minute=59, second=59, microsecond=0)
    else:
        expires = now + datetime.timedelta(minutes=int(minutes))
    entries = [e for e in active(now) if not (e["client"] == client and e["target"] == target)]
    entries.append({"client": client, "target": target,
                    "created": now.isoformat(timespec="seconds"),
                    "expires": expires.isoformat(timespec="seconds")})
    _write(entries)
    return expires


def remove(client=None, target=None, now=None):
    now = _now(now)
    keep = [e for e in active(now)
            if not ((client is None or e["client"] == client) and (target is None or e["target"] == target))]
    removed = len(active(now)) - len(keep)
    _write(keep)
    return removed
