#!/usr/bin/env python3
"""Task 14: add Spotify tracks (not already on YT Music) to a YouTube Music playlist AND like
them (so they also populate "Liked Music"), per Ivan's request.

Switched from the official YouTube Data API v3 (used for the pilot) to ytmusicapi's own
browser-auth calls once both add_playlist_items() and rate_song() were confirmed working
against this account (empirically tested directly - the write path was broken for OAuth, and
multiple independent reports online said browser-auth also broke for writes as of Dec 2025,
but it works for this account/session as of today; verify again if this is ever re-run after
a long gap, don't assume it still holds). This is simpler and not quota-metered like the
official API, so no daily-limit pacing is needed - but it is still an unofficial internal API,
so this script still paces itself and stops cleanly on repeated failures rather than hammering it.

Usage: create_ytm_playlist.py --limit N [--dry-run] [--no-like]
"""
import argparse
import configparser
import csv
import re
import sys
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from ytmusicapi import YTMusic

# Blind top-of-search-results trust produced real wrong matches on the pilot run (e.g. "Across
# the Stars" by Ryker Ashford matched to a different song, "Shoot for the Stars", by the same
# artist; "A Cruel Angel's Thesis" by Jonathan Young matched to a cover by a different artist,
# "Eluzai"). A similarity gate on both title and artist is required, not optional - a candidate
# that fails it is recorded as "low_confidence"/"not_found" for manual review, never inserted.
TITLE_MIN_SIMILARITY = 0.75
ARTIST_MIN_SIMILARITY = 0.55  # looser: "feat." credits, romanization, single-vs-multi artist

HERE = Path(__file__).parent
OUT_DIR = Path("/srv/data/playlists")
CANDIDATES_CSV = OUT_DIR / "transfer-candidates.csv"
PROGRESS_CSV = OUT_DIR / "transfer-progress.csv"  # append-only log of every attempt
PLAYLIST_STATE = HERE / ".ytm-playlist-id"

PLAYLIST_TITLE = "From Spotify (auto-import)"
PLAYLIST_DESC = "Tracks from Ivan's Spotify playlists/Liked Songs not already on YouTube Music. Created by Task 14."

TERMINAL_STATUSES = {"inserted", "not_found"}  # *_failed is retryable, not terminal


def norm(s: str) -> str:
    # Apostrophes must be dropped (not turned into a space) BEFORE the ascii-encode step, and
    # consistently regardless of which apostrophe character was used - Spotify's data uses a
    # straight quote (U+0027, ASCII, survives encode/ignore) while YouTube Music titles commonly
    # use a curly one (U+2019, non-ASCII, silently dropped by encode/ignore with no trace) -
    # that asymmetry alone turned "fool's" into "fool s" from one source and "fools" from the
    # other, breaking every substring/containment check downstream. Caught on the pilot run.
    s = (s or "").replace("'", "").replace("’", "").replace("‘", "")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def title_similarity(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    ratio = SequenceMatcher(None, na, nb).ratio()
    # A clean substring match (e.g. "A Fool's Parade" inside "A Fool's Parade (feat. X)") is a
    # same-song signal that plain SequenceMatcher ratio under-scores once the suffix gets long
    # relative to the title - caught on the pilot run rejecting exactly this shape of match.
    if na and nb and (na in nb or nb in na):
        ratio = max(ratio, 0.85)
    return ratio


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def best_match(candidate_title, candidate_artist, results):
    """Among search results that pass BOTH thresholds independently, return the best-scoring
    one. Picking a single "best by summed score" first and checking thresholds only afterward
    lets a good title match with a slightly-off artist string lose to a bad title match with a
    perfect artist string - caught on the pilot run. Returns (result, title_sim, artist_sim),
    or (None, closest_tsim, closest_asim) if nothing clears the bar."""
    passing = []
    closest = None
    closest_score = -1.0
    for r in results:
        rtitle = r.get("title", "")
        rartists = ", ".join(a["name"] for a in r.get("artists", []) if a.get("name"))
        tsim = title_similarity(candidate_title, rtitle)
        asim = max(
            (similarity(candidate_artist.split(",")[0], a) for a in rartists.split(",")) if rartists else [0.0],
            default=0.0,
        )
        if tsim >= TITLE_MIN_SIMILARITY and asim >= ARTIST_MIN_SIMILARITY:
            passing.append((r, tsim, asim))
        score = tsim + asim
        if score > closest_score:
            closest_score, closest = score, (r, tsim, asim)
    if passing:
        return max(passing, key=lambda x: x[1] + x[2])
    if closest is None:
        return None, 0.0, 0.0
    _, tsim, asim = closest
    return None, tsim, asim


def load_progress():
    done = set()
    if PROGRESS_CSV.exists():
        with open(PROGRESS_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["status"] in TERMINAL_STATUSES:
                    done.add((row["title"], row["artists"]))
    return done


def append_progress(row):
    is_new = not PROGRESS_CSV.exists()
    with open(PROGRESS_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["title", "artists", "spotify_source", "status", "video_id", "matched_title", "matched_artist", "liked"])
        w.writerow(row)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-like", action="store_true", help="add to playlist only, skip liking")
    args = ap.parse_args()

    yt = YTMusic(str(HERE / "browser.json"))

    already_done = load_progress()
    with open(CANDIDATES_CSV, newline="", encoding="utf-8") as f:
        candidates = list(csv.DictReader(f))
    todo = [c for c in candidates if (c["title"], c["artists"]) not in already_done]
    if args.limit is not None:
        todo = todo[: args.limit]

    print(f"{len(already_done)} already done, {len(todo)} to process this run", file=sys.stderr)
    if not todo:
        print("Nothing to do.", file=sys.stderr)
        return

    playlist_id = None
    if not args.dry_run:
        if PLAYLIST_STATE.exists():
            playlist_id = PLAYLIST_STATE.read_text().strip()
            print(f"Reusing existing playlist: {playlist_id}", file=sys.stderr)
        else:
            playlist_id = yt.create_playlist(PLAYLIST_TITLE, PLAYLIST_DESC, privacy_status="PRIVATE")
            PLAYLIST_STATE.write_text(playlist_id)
            print(f"Created playlist: {playlist_id}", file=sys.stderr)

    inserted = liked = failed = not_found = 0
    consecutive_failures = 0
    for c in todo:
        query = f"{c['title']} {c['artists'].split(',')[0]}"
        try:
            results = yt.search(query, filter="songs", limit=5)
        except Exception as e:
            print(f"SEARCH FAILED: {c['title']!r} - {e}", file=sys.stderr)
            results = []

        r, tsim, asim = best_match(c["title"], c["artists"], results)
        if not r:
            not_found += 1
            append_progress([c["title"], c["artists"], c["spotify_source"], "not_found", "", "", "", ""])
            if results:
                top = results[0]
                mt = top.get("title", "")
                ma = ", ".join(a["name"] for a in top.get("artists", []) if a.get("name"))
                print(f"  MISS: {c['title']} / {c['artists']}  =/=  {mt} / {ma}  (tsim={tsim:.2f} asim={asim:.2f})", file=sys.stderr)
            time.sleep(0.3)
            continue

        vid = r["videoId"]
        mtitle = r.get("title", "")
        martists = ", ".join(a["name"] for a in r.get("artists", []) if a.get("name"))

        if args.dry_run:
            print(f"  OK: {c['title']} / {c['artists']}  ->  {mtitle} / {martists} ({vid})")
            time.sleep(0.3)
            continue

        did_like = False
        try:
            yt.add_playlist_items(playlist_id, [vid], duplicates=False)
            status = "inserted"
            inserted += 1
            consecutive_failures = 0
        except Exception as e:
            status = f"insert_failed_{type(e).__name__}"
            failed += 1
            consecutive_failures += 1
            print(f"INSERT FAILED for {c['title']!r}: {e}", file=sys.stderr)

        if status == "inserted" and not args.no_like:
            try:
                yt.rate_song(vid, "LIKE")
                did_like = True
                liked += 1
            except Exception as e:
                print(f"LIKE FAILED for {c['title']!r}: {e}", file=sys.stderr)

        append_progress([c["title"], c["artists"], c["spotify_source"], status, vid, mtitle, martists, "yes" if did_like else ""])

        if consecutive_failures >= 5:
            print("5 consecutive insert failures - stopping cleanly, re-run to resume.", file=sys.stderr)
            break
        time.sleep(0.5)

    print(f"\nInserted {inserted}, liked {liked}, failed {failed}, not_found {not_found} this run", file=sys.stderr)


if __name__ == "__main__":
    main()
