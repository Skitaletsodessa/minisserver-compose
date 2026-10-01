"""Cached wrapper around the two MusicBrainz calls this job needs. 1 req/sec, mandatory
User-Agent (contact supplied by Ivan). Every response is cached, including misses."""
import os
import musicbrainzngs as mb

from mb_cache import MBCache

_configured = False


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


def search_recordings(cache: MBCache, artist: str, title: str, limit: int = 5):
    found, is_miss, value, _ = cache.get("search_recordings", artist, title, str(limit))
    if found:
        return [] if is_miss else value

    configure()
    try:
        res = mb.search_recordings(artist=artist, recording=title, limit=limit)
        recordings = res.get("recording-list", [])
    except Exception:
        recordings = []

    if not recordings:
        cache.put("search_recordings", artist, title, str(limit), is_miss=True)
        return []
    cache.put("search_recordings", artist, title, str(limit), value=recordings)
    return recordings


def releases_for_recording(cache: MBCache, recording_id: str):
    found, is_miss, value, _ = cache.get("browse_releases", recording_id)
    if found:
        return [] if is_miss else value

    configure()
    try:
        res = mb.browse_releases(
            recording=recording_id,
            includes=["release-groups", "media", "recordings", "artist-credits"],
            limit=100,
        )
        releases = res.get("release-list", [])
    except Exception:
        releases = []

    if not releases:
        cache.put("browse_releases", recording_id, is_miss=True)
        return []
    cache.put("browse_releases", recording_id, value=releases)
    return releases
