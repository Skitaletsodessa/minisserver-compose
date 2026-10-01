"""Step 2 (Task 19) - dry-run plan. Network allowed (MusicBrainz), NO writes to any
audio file. Produces /srv/data/music-tags/plan-<date>.csv.

Only considers files whose current Album tag is empty (checked live, not from the
before-JSON, so this script is also what the daily timer in step 4 reuses).
"""
import csv
import os
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from mutagen.oggopus import OggOpus
from mutagen.mp4 import MP4

sys.path.insert(0, str(Path(__file__).parent))
from mb_cache import MBCache
from mb_client import search_recordings, releases_for_recording
from matching import passes_gate, choose_release_group, normalize

LIBRARY_DIR = Path("/srv/library/music")
OUT_DIR = Path("/srv/data/music-tags")
SEARCH_CANDIDATES = 5


def safe_component(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = re.sub(r'[\\/:*?"<>|]', "_", s)
    s = s.strip().strip(".")
    return s[:150] or "untitled"


def read_basic_tags(path: Path):
    suffix = path.suffix.lower()
    try:
        if suffix == ".opus":
            audio = OggOpus(path)
            tags = audio.tags or {}
            title = (tags.get("title") or [""])[0]
            artist = (tags.get("artist") or [""])[0]
            album = (tags.get("album") or [""])[0]
        elif suffix == ".m4a":
            audio = MP4(path)
            tags = audio.tags or {}
            title = (tags.get("\xa9nam") or [""])[0]
            artist = (tags.get("\xa9art") or [""])[0]
            album = (tags.get("\xa9alb") or [""])[0]
        else:
            return None
        return {"title": title, "artist": artist, "album": album}
    except Exception:
        return None


def find_empty_album_files():
    files = []
    for root, dirs, filenames in os.walk(LIBRARY_DIR):
        if "_playlists" in root:
            continue
        for fn in filenames:
            if fn.lower().endswith((".opus", ".m4a")):
                files.append(Path(root) / fn)
    files.sort()

    targets = []
    for path in files:
        tags = read_basic_tags(path)
        if tags is None:
            continue
        if not tags["album"]:
            targets.append((path, tags))
    return targets


def pooled_release_groups(cache, candidates):
    """candidates: list of recording dicts that passed the confidence gate.
    Returns a flat list of release-group dicts (with release_count annotated) pooled
    across every candidate recording's releases."""
    rg_release_counts = {}
    rg_objects = {}
    rg_track_info = {}  # rg_id -> set of (position, track_count) seen across its releases

    for rec in candidates:
        rec_id = rec.get("id")
        releases = releases_for_recording(cache, rec_id)
        for rel in releases:
            rg = rel.get("release-group") or {}
            rg_id = rg.get("id")
            if not rg_id:
                continue
            rg_objects[rg_id] = rg
            rg_release_counts[rg_id] = rg_release_counts.get(rg_id, 0) + 1

            for medium in rel.get("medium-list", []):
                track_count = medium.get("track-count")
                for t in medium.get("track-list", []):
                    if (t.get("recording") or {}).get("id") == rec_id:
                        rg_track_info.setdefault(rg_id, set()).add((t.get("position"), track_count))

    pooled = []
    for rg_id, rg in rg_objects.items():
        rg = dict(rg)
        rg["release_count"] = rg_release_counts[rg_id]
        track_positions = rg_track_info.get(rg_id, set())
        if len(track_positions) == 1:
            pos, cnt = next(iter(track_positions))
            rg["_track_position"] = pos
            rg["_track_count"] = cnt
        else:
            rg["_track_position"] = None
            rg["_track_count"] = None
        pooled.append(rg)
    return pooled


def main():
    load_dotenv(Path(__file__).parent / ".env")
    if not os.environ.get("MB_USER_AGENT_CONTACT"):
        print("MB_USER_AGENT_CONTACT not set in .env - aborting before any network call", file=sys.stderr)
        sys.exit(1)

    cache = MBCache()
    targets = find_empty_album_files()
    print(f"Files with empty Album: {len(targets)}", file=sys.stderr)

    today = date.today().isoformat()
    out_path = OUT_DIR / f"plan-{today}.csv"

    rows = []
    for i, (path, tags) in enumerate(targets, 1):
        rel_path = str(path.relative_to(LIBRARY_DIR))
        title, artist = tags["title"], tags["artist"]

        if not title or not artist:
            rows.append({
                "path": rel_path, "title": title, "artist": artist,
                "action": "unmatched", "reason": "missing title or artist tag",
                "rank": "", "title_sim": "", "artist_sim": "",
                "proposed_album": "", "proposed_date": "", "proposed_track": "",
                "proposed_totaltracks": "", "recording_mbid": "", "release_group_mbid": "",
            })
            continue

        candidates_raw = search_recordings(cache, artist, title, limit=SEARCH_CANDIDATES)

        passing = []
        best_scores = (0.0, 0.0)       # best pair seen across ALL candidates (for the unmatched case)
        best_passing_scores = (0.0, 0.0)  # best pair among candidates that actually passed the gate
        for rec in candidates_raw:
            rec_title = rec.get("title", "")
            rec_artist = ", ".join(
                ac.get("artist", {}).get("name", "")
                for ac in rec.get("artist-credit", [])
                if isinstance(ac, dict) and ac.get("artist")
            )
            ok, t_sim, a_sim = passes_gate(title, artist, rec_title, rec_artist)
            best_scores = max(best_scores, (t_sim, a_sim))
            if ok:
                passing.append(rec)
                best_passing_scores = max(best_passing_scores, (t_sim, a_sim))

        if not passing:
            rows.append({
                "path": rel_path, "title": title, "artist": artist,
                "action": "unmatched", "reason": "no search result cleared the confidence gate",
                "rank": "", "title_sim": round(best_scores[0], 3), "artist_sim": round(best_scores[1], 3),
                "proposed_album": "", "proposed_date": "", "proposed_track": "",
                "proposed_totaltracks": "", "recording_mbid": "", "release_group_mbid": "",
            })
        else:
            pooled = pooled_release_groups(cache, passing)
            chosen, rank, ambiguous, _ = choose_release_group(pooled)

            if ambiguous or chosen is None:
                rows.append({
                    "path": rel_path, "title": title, "artist": artist,
                    "action": "ambiguous",
                    "reason": "tied release groups within the best rank" if ambiguous else "no usable release group",
                    "rank": rank or "", "title_sim": round(best_passing_scores[0], 3), "artist_sim": round(best_passing_scores[1], 3),
                    "proposed_album": "", "proposed_date": "", "proposed_track": "",
                    "proposed_totaltracks": "", "recording_mbid": passing[0].get("id", ""), "release_group_mbid": "",
                })
            else:
                year = (chosen.get("first-release-date") or "").split("-")[0]
                rows.append({
                    "path": rel_path, "title": title, "artist": artist,
                    "action": "fill",
                    "reason": "",
                    "rank": rank, "title_sim": round(best_passing_scores[0], 3), "artist_sim": round(best_passing_scores[1], 3),
                    "proposed_album": chosen.get("title", ""),
                    "proposed_albumartist": chosen.get("artist-credit-phrase", ""),
                    "proposed_date": year,
                    "proposed_track": chosen.get("_track_position") or "",
                    "proposed_totaltracks": chosen.get("_track_count") or "",
                    "recording_mbid": passing[0].get("id", ""),
                    "release_group_mbid": chosen.get("id", ""),
                })

        if i % 20 == 0:
            print(f"  ...{i}/{len(targets)}", file=sys.stderr)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "path", "title", "artist", "action", "reason", "rank", "title_sim", "artist_sim",
        "proposed_album", "proposed_albumartist", "proposed_date", "proposed_track",
        "proposed_totaltracks", "recording_mbid", "release_group_mbid",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, restval="")
        w.writeheader()
        w.writerows(rows)

    print(f"\nWrote {out_path} ({len(rows)} rows)")
    by_action = {}
    by_rank = {}
    for r in rows:
        by_action[r["action"]] = by_action.get(r["action"], 0) + 1
        if r["action"] == "fill":
            by_rank[r["rank"]] = by_rank.get(r["rank"], 0) + 1
    print("By action:", by_action)
    print("Fills by rank:", by_rank)

    cache.close()


if __name__ == "__main__":
    main()
