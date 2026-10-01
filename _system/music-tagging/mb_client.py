"""Cached wrapper around the two MusicBrainz calls this job needs. 1 req/sec, mandatory
User-Agent (contact supplied by Ivan). Every response is cached, including misses.

A cached MISS is honored for 30 days (the daily timer's retry policy - tasks/task-19.md
step 4), then re-queried. A cached HIT is honored indefinitely; a real match doesn't go
stale the way "nothing found yet" might.

Network-level failures (MusicBrainz unreachable, blocked, etc.) are NOT caught here and
NOT treated as an ordinary zero-result miss - they propagate to the caller, which is
what lets the daily timer tell "genuinely nothing matched" apart from "couldn't even
ask" and exit non-zero only for the latter.
"""
import os
from datetime import date

import musicbrainzngs as mb

from mb_cache import MBCache

_configured = False
MISS_TTL_DAYS = 30


def configure():
    global _configured
    if _configured:
        return
    contact = os.environ.get("MB_USER_AGENT_CONTACT")
    if not contact:
        raise RuntimeError("MB_USER_AGENT_CONTACT not set - refusing to query MusicBrainz anonymously")
    mb.set_useragent("minisserver-tagger", "1.0", contact)
    mb.set_rate_limit(limit_or_interval=1.0, new_requests=1)
    _configured = True


def _miss_is_stale(fetched_date: str) -> bool:
    if not fetched_date:
        return True
    try:
        age = (date.today() - date.fromisoformat(fetched_date)).days
    except ValueError:
        return True
    return age >= MISS_TTL_DAYS


def search_recordings(cache: MBCache, artist: str, title: str, limit: int = 5):
    found, is_miss, value, fetched_date = cache.get("search_recordings", artist, title, str(limit))
    if found:
        if not is_miss:
            return value
        if not _miss_is_stale(fetched_date):
            return []
        # stale miss - fall through and re-query

    configure()
    res = mb.search_recordings(artist=artist, recording=title, limit=limit)
    recordings = res.get("recording-list", [])

    if not recordings:
        cache.put("search_recordings", artist, title, str(limit), is_miss=True)
        return []
    cache.put("search_recordings", artist, title, str(limit), value=recordings)
    return recordings


def releases_for_recording(cache: MBCache, recording_id: str):
    found, is_miss, value, fetched_date = cache.get("browse_releases", recording_id)
    if found:
        if not is_miss:
            return value
        if not _miss_is_stale(fetched_date):
            return []
        # stale miss - fall through and re-query

    configure()
    res = mb.browse_releases(
        recording=recording_id,
        includes=["release-groups", "media", "recordings", "artist-credits"],
        limit=100,
    )
    releases = res.get("release-list", [])

    if not releases:
        cache.put("browse_releases", recording_id, is_miss=True)
        return []
    cache.put("browse_releases", recording_id, value=releases)
    return releases
