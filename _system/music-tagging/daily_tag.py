"""Step 4 (Task 19) - the daily job. Finds files with an empty Album tag (live check,
same as plan.py - new files Ivan adds show up automatically, nothing re-lists them),
matches each against MusicBrainz, and writes the result directly for ranks 1 and 4
(rank 2 excluded - Ivan's call after report-19 found it produces real wrong matches,
e.g. bootleg/various-artists compilations, not just lower-confidence ones).

A file that doesn't match anything is naturally retried at most once every 30 days,
via mb_client's cache TTL on misses - no separate skip list needed. A file that already
has an Album is never looked at twice (the live check IS the skip list).

If MusicBrainz is unreachable or blocks us, the first network-level error aborts the
run and this exits non-zero - that's the signal OnFailure= should alert on. A run where
every file was simply unmatched (MusicBrainz reachable, nothing found) exits 0; that is
a normal, boring day, not a failure.

A Navidrome full scan runs only if at least one file was actually changed.
"""
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
from mutagen.oggopus import OggOpus
from mutagen.mp4 import MP4
from musicbrainzngs import MusicBrainzError

sys.path.insert(0, str(Path(__file__).parent))
from mb_cache import MBCache
from mb_client import search_recordings, releases_for_recording
from matching import passes_gate, choose_release_group
from plan import find_empty_album_files, pooled_release_groups, SEARCH_CANDIDATES
from apply import read_audio, tag_is_empty, set_text

ALLOWED_RANKS = {1, 4}


def match_and_write(cache, path, tags):
    title, artist = tags["title"], tags["artist"]
    if not title or not artist:
        return "unmatched"

    candidates_raw = search_recordings(cache, artist, title, limit=SEARCH_CANDIDATES)
    passing = []
    for rec in candidates_raw:
        rec_title = rec.get("title", "")
        rec_artist = ", ".join(
            ac.get("artist", {}).get("name", "")
            for ac in rec.get("artist-credit", [])
            if isinstance(ac, dict) and ac.get("artist")
        )
        ok, _, _ = passes_gate(title, artist, rec_title, rec_artist)
        if ok:
            passing.append(rec)

    if not passing:
        return "unmatched"

    pooled = pooled_release_groups(cache, passing)
    chosen, rank, ambiguous, _ = choose_release_group(pooled)

    if ambiguous or chosen is None:
        return "ambiguous"
    if rank not in ALLOWED_RANKS:
        return f"skipped-rank-{rank}"

    year = (chosen.get("first-release-date") or "").split("-")[0]
    audio, keys = read_audio(path)
    suffix = path.suffix.lower()

    if not tag_is_empty(audio, keys["album"]):
        return "already-tagged"  # race with something else touching the file

    set_text(audio, keys["album"], chosen.get("title", ""))
    albumartist = chosen.get("artist-credit-phrase", "")
    if albumartist and tag_is_empty(audio, keys["albumartist"]):
        set_text(audio, keys["albumartist"], albumartist)
    if year and tag_is_empty(audio, keys["date"]):
        set_text(audio, keys["date"], year)

    pos = chosen.get("_track_position")
    cnt = chosen.get("_track_count")
    if pos and tag_is_empty(audio, keys["track"]):
        if suffix == ".opus":
            set_text(audio, keys["track"], pos)
        else:
            audio.tags[keys["track"]] = [(int(pos), int(cnt) if cnt else 0)]
    if suffix == ".opus" and cnt and tag_is_empty(audio, keys["totaltracks"]):
        set_text(audio, keys["totaltracks"], cnt)

    rg_id = chosen.get("id")
    if rg_id and tag_is_empty(audio, keys["rg_id"]):
        set_text(audio, keys["rg_id"], rg_id)
    track_id = chosen.get("_release_track_id")
    if track_id and tag_is_empty(audio, keys["track_id"]):
        set_text(audio, keys["track_id"], track_id)

    audio.save()
    return "written"


def main():
    load_dotenv(Path(__file__).parent / ".env")
    cache = MBCache()

    targets = find_empty_album_files()
    print(f"Files with empty Album: {len(targets)}", file=sys.stderr)

    counts = {}
    network_failure = None

    for i, (path, tags) in enumerate(targets, 1):
        try:
            status = match_and_write(cache, path, tags)
        except (MusicBrainzError, RuntimeError) as e:
            # RuntimeError here is specifically mb_client.configure()'s own guard
            # (missing MB_USER_AGENT_CONTACT) - a broken setup, not a per-file issue,
            # so it gets the same hard-abort treatment as a real network failure.
            network_failure = e
            print(f"MusicBrainz unreachable/blocked, or misconfigured: {e}", file=sys.stderr)
            break
        except Exception as e:
            status = "error"
            print(f"ERROR {path}: {e}", file=sys.stderr)

        counts[status] = counts.get(status, 0) + 1

    print(f"\n{counts}")
    cache.close()

    if network_failure is not None:
        print("Aborting - MusicBrainz was unreachable, not just silent on these tracks.", file=sys.stderr)
        sys.exit(1)

    written = counts.get("written", 0)
    if written > 0:
        print(f"{written} file(s) changed - triggering a Navidrome scan.", file=sys.stderr)
        result = subprocess.run(
            ["docker", "exec", "navidrome", "/app/navidrome", "scan", "--full"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"Navidrome scan failed: {result.stderr}", file=sys.stderr)
            sys.exit(1)
    else:
        print("Nothing written - no scan needed.", file=sys.stderr)


if __name__ == "__main__":
    main()
