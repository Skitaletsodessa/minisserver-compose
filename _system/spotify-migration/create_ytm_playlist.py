#!/usr/bin/env python3
"""Task 14: create a YouTube Music playlist from Spotify tracks not already on YT Music,
via the official YouTube Data API v3 (not the internal API ytmusicapi/spotify_to_ytmusic hit -
that write path is confirmed broken by Google as of 2026-09, see docs/report-14.md).

Search for each track's videoId via ytmusicapi (browser-auth, read-only, does not count
against the Data API v3 daily quota) - then create the playlist and insert items through the
official API (OAuth token, scope https://www.googleapis.com/auth/youtube), which DOES count
against quota (playlists.insert ~50 units, playlistItems.insert ~50 units each; default daily
quota is commonly 10,000 units, so roughly 190 inserts/day is the practical ceiling until
proven otherwise - this script stops cleanly on a quota error rather than retrying).

Usage: create_ytm_playlist.py --limit N [--playlist-id ID] [--start-after CSV_ROW_INDEX]
  --limit N        only process the first N rows of transfer-candidates.csv not yet attempted
                    (pilot runs use a small N; omit for "all remaining")
  --playlist-id ID  reuse an existing playlist instead of creating a new one (for resuming)
  --dry-run         search only, do not create/insert anything
"""
import argparse
import configparser
import csv
import json
import re
import sys
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import requests
from ytmusicapi import YTMusic

# Blind top-of-search-results trust produced real wrong matches on a pilot run (e.g. "Across
# the Stars" by Ryker Ashford matched to a different song, "Shoot for the Stars", by the same
# artist; "A Cruel Angel's Thesis" by Jonathan Young matched to a cover by a different artist,
# "Eluzai"). A similarity gate on both title and artist is required, not optional - a candidate
# that fails it is recorded as "low_confidence" for manual review rather than silently inserted.
TITLE_MIN_SIMILARITY = 0.75
ARTIST_MIN_SIMILARITY = 0.55  # looser: "feat." credits, romanization, single-vs-multi artist


def norm(s: str) -> str:
    # Apostrophes must be dropped (not turned into a space) BEFORE the ascii-encode step, and
    # consistently regardless of which apostrophe character was used - Spotify's data uses a
    # straight quote (U+0027, ASCII, survives encode/ignore) while YouTube Music titles commonly
    # use a curly one (U+2019, non-ASCII, silently dropped by encode/ignore with no trace) -
    # that asymmetry alone turned "fool's" into "fool s" from one source and "fools" from the
    # other, breaking every substring/containment check downstream. Caught on a real pilot run:
    # "A Fool's Parade" wrongly failed to match "A Fool's Parade (feat. Alex Yarmak)".
    s = (s or "").replace("'", "").replace("’", "").replace("‘", "")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def title_similarity(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    ratio = SequenceMatcher(None, na, nb).ratio()
    # A clean substring match (e.g. "A Fool's Parade" inside "A Fool's Parade (feat. X)", or
    # "50c WISDOM" inside "50c WISDOM - 50cent WISDOM") is a same-song signal that plain
    # SequenceMatcher ratio under-scores once the suffix gets long relative to the title -
    # caught on a real pilot run rejecting exactly this shape of correct match.
    if na and nb and (na in nb or nb in na):
        ratio = max(ratio, 0.85)
    return ratio


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def best_match(candidate_title, candidate_artist, results):
    """Score every search result. Among results that pass BOTH thresholds independently, return
    the best-scoring one - picking a single "best overall by summed score" first and only
    checking the gate afterward lets a good title match with a slightly-off artist string lose
    out to a bad title match with a perfect artist string (caught on a real pilot run: "A
    Fool's Parade" vs "A Fool's Parade (feat. Alex Yarmak)" was a clean substring match but
    scored below a same-artist, wrong-song result on combined score alone).
    Returns (result, title_sim, artist_sim), or (None, best_tsim, best_asim) of the closest
    miss if nothing clears the bar, for the caller to log for manual review."""
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
    r, tsim, asim = closest
    if tsim < TITLE_MIN_SIMILARITY or asim < ARTIST_MIN_SIMILARITY:
        return None, tsim, asim
    return r, tsim, asim

HERE = Path(__file__).parent
OUT_DIR = Path("/srv/data/playlists")
CANDIDATES_CSV = OUT_DIR / "transfer-candidates.csv"
PROGRESS_CSV = OUT_DIR / "transfer-progress.csv"  # append-only log of every attempt
PLAYLIST_STATE = HERE / ".ytm-playlist-id"  # not a secret, just remembers the created playlist

PLAYLIST_TITLE = "From Spotify (auto-import)"
PLAYLIST_DESC = "Tracks from Ivan's Spotify playlists/Liked Songs not already on YouTube Music. Created by Task 14."


def get_access_token(client_id, client_secret, refresh_token):
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


TERMINAL_STATUSES = {"inserted", "not_found"}  # insert_failed_* is retryable, not terminal


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
            w.writerow(["title", "artists", "spotify_source", "status", "video_id", "matched_title", "matched_artist"])
        w.writerow(row)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--playlist-id", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    config = configparser.ConfigParser(interpolation=None)
    config.read(HERE / "settings.ini")
    client_id = config["youtube"]["client_id"]
    client_secret = config["youtube"]["client_secret"]
    yt_token = json.loads(config["youtube"]["headers"])
    refresh_token = yt_token["refresh_token"]

    yt_search = YTMusic(str(HERE / "browser.json"))

    already_done = load_progress()

    with open(CANDIDATES_CSV, newline="", encoding="utf-8") as f:
        candidates = list(csv.DictReader(f))

    todo = [c for c in candidates if (c["title"], c["artists"]) not in already_done]
    if args.limit is not None:
        todo = todo[: args.limit]

    print(f"{len(already_done)} already attempted, {len(todo)} to process this run", file=sys.stderr)
    if not todo:
        print("Nothing to do.", file=sys.stderr)
        return

    # --- search phase (ytmusicapi/browser-auth, free) ---
    matches = []  # (candidate_row, video_id or None, matched_title, matched_artist, reason)
    low_confidence = []  # for manual review - a real candidate existed but didn't clear the bar
    for c in todo:
        query = f"{c['title']} {c['artists'].split(',')[0]}"
        try:
            results = yt_search.search(query, filter="songs", limit=5)
        except Exception as e:
            print(f"SEARCH FAILED: {c['title']!r} - {e}", file=sys.stderr)
            results = []
        r, tsim, asim = best_match(c["title"], c["artists"], results)
        if r:
            vid = r.get("videoId")
            mtitle = r.get("title", "")
            martists = ", ".join(a["name"] for a in r.get("artists", []) if a.get("name"))
            matches.append((c, vid, mtitle, martists))
        else:
            matches.append((c, None, "", ""))
            if results:
                top = results[0]
                low_confidence.append(
                    (c, top.get("title", ""), ", ".join(a["name"] for a in top.get("artists", []) if a.get("name")), tsim, asim)
                )
        time.sleep(0.3)  # be polite, this is an unofficial API even if quota-free

    if low_confidence:
        print(f"\n{len(low_confidence)} candidates had a search result but failed the similarity gate (best guess shown, NOT inserted):", file=sys.stderr)
        for c, mt, ma, tsim, asim in low_confidence:
            print(f"  {c['title']} / {c['artists']}  =/=  {mt} / {ma}  (title_sim={tsim:.2f} artist_sim={asim:.2f})", file=sys.stderr)

    found = [m for m in matches if m[1]]
    not_found = [m for m in matches if not m[1]]
    print(f"Search: {len(found)} matched, {len(not_found)} not found", file=sys.stderr)

    if args.dry_run:
        for c, vid, mt, ma in matches:
            print(f"  {'OK' if vid else 'MISS'}: {c['title']} / {c['artists']}  ->  {mt} / {ma} ({vid})")
        return

    if not found:
        print("Nothing matched, nothing to create/insert.", file=sys.stderr)
        for c, vid, mt, ma in not_found:
            append_progress([c["title"], c["artists"], c["spotify_source"], "not_found", "", "", ""])
        return

    # --- write phase (official Data API v3, counts against quota) ---
    access_token = get_access_token(client_id, client_secret, refresh_token)
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    playlist_id = args.playlist_id
    if not playlist_id and PLAYLIST_STATE.exists():
        playlist_id = PLAYLIST_STATE.read_text().strip()
        print(f"Reusing existing playlist from previous run: {playlist_id}", file=sys.stderr)

    if not playlist_id:
        r = requests.post(
            "https://www.googleapis.com/youtube/v3/playlists",
            params={"part": "snippet,status"},
            headers=headers,
            json={
                "snippet": {"title": PLAYLIST_TITLE, "description": PLAYLIST_DESC},
                "status": {"privacyStatus": "private"},
            },
        )
        if r.status_code != 200:
            print(f"PLAYLIST CREATE FAILED: {r.status_code} {r.text}", file=sys.stderr)
            sys.exit(1)
        playlist_id = r.json()["id"]
        PLAYLIST_STATE.write_text(playlist_id)
        print(f"Created playlist: {playlist_id}", file=sys.stderr)

    inserted = 0
    for c, vid, mt, ma in found:
        r = requests.post(
            "https://www.googleapis.com/youtube/v3/playlistItems",
            params={"part": "snippet"},
            headers=headers,
            json={"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": vid}}},
        )
        if r.status_code == 200:
            append_progress([c["title"], c["artists"], c["spotify_source"], "inserted", vid, mt, ma])
            inserted += 1
        elif r.status_code == 403 and "quota" in r.text.lower():
            print(f"QUOTA EXCEEDED after {inserted} inserts this run. Stopping cleanly - re-run later to resume.", file=sys.stderr)
            break
        else:
            print(f"INSERT FAILED for {c['title']!r}: {r.status_code} {r.text[:200]}", file=sys.stderr)
            append_progress([c["title"], c["artists"], c["spotify_source"], f"insert_failed_{r.status_code}", vid, mt, ma])
        time.sleep(0.5)

    for c, vid, mt, ma in not_found:
        append_progress([c["title"], c["artists"], c["spotify_source"], "not_found", "", "", ""])

    print(f"\nInserted {inserted}/{len(found)} matched tracks into playlist {playlist_id}", file=sys.stderr)
    print(f"{len(not_found)} tracks had no YT Music match at all (recorded as not_found)", file=sys.stderr)


if __name__ == "__main__":
    main()
