"""Step 3 (Task 19) - apply. Writes only the plan's `fill` rows, restricted to the
ranks Ivan approved (--ranks, e.g. "1,4" - rank 2 excluded per his review of report-19).

Re-checks each file live before writing (the plan may be stale by the time this runs):
Album is written only if it's still empty; AlbumArtist/Date/Track/TotalTracks/MB ids
are each written only if THAT specific field is still empty, independently - never
overwrites anything Ivan or an earlier pass already set.

Every written file is re-read afterward and compared against the plan.
"""
import argparse
import csv
import sys
from pathlib import Path

from mutagen.oggopus import OggOpus
from mutagen.mp4 import MP4

LIBRARY_DIR = Path("/srv/library/music")


def read_audio(path: Path):
    suffix = path.suffix.lower()
    if suffix == ".opus":
        return OggOpus(path), {
            "album": "album", "albumartist": "albumartist", "date": "date",
            "track": "tracknumber", "totaltracks": "totaltracks",
            "rg_id": "musicbrainz_releasegroupid", "track_id": "musicbrainz_releasetrackid",
        }
    elif suffix == ".m4a":
        return MP4(path), {
            "album": "\xa9alb", "albumartist": "aART", "date": "\xa9day",
            "track": "trkn", "totaltracks": "trkn",
            "rg_id": "----:com.apple.iTunes:MusicBrainz Release Group Id",
            "track_id": "----:com.apple.iTunes:MusicBrainz Release Track Id",
        }
    raise ValueError(f"unsupported suffix {suffix}")


def tag_is_empty(audio, key) -> bool:
    v = audio.tags.get(key) if audio.tags else None
    return not v


def set_text(audio, key, value):
    audio.tags[key] = [str(value)]


def apply_one(row, dry_run=False):
    rel_path = row["path"]
    full = LIBRARY_DIR / rel_path
    if not full.exists():
        return "missing-file", None

    audio, keys = read_audio(full)
    suffix = full.suffix.lower()

    if not tag_is_empty(audio, keys["album"]):
        return "already-tagged", None  # changed since the plan was made - don't touch

    written = {}

    set_text(audio, keys["album"], row["proposed_album"])
    written["album"] = row["proposed_album"]

    if row.get("proposed_albumartist") and tag_is_empty(audio, keys["albumartist"]):
        set_text(audio, keys["albumartist"], row["proposed_albumartist"])
        written["albumartist"] = row["proposed_albumartist"]

    if row.get("proposed_date") and tag_is_empty(audio, keys["date"]):
        set_text(audio, keys["date"], row["proposed_date"])
        written["date"] = row["proposed_date"]

    if row.get("proposed_track") and tag_is_empty(audio, keys["track"]):
        if suffix == ".opus":
            set_text(audio, keys["track"], row["proposed_track"])
        else:
            total = int(row["proposed_totaltracks"]) if row.get("proposed_totaltracks") else 0
            audio.tags[keys["track"]] = [(int(row["proposed_track"]), total)]
        written["track"] = row["proposed_track"]

    if suffix == ".opus" and row.get("proposed_totaltracks") and tag_is_empty(audio, keys["totaltracks"]):
        set_text(audio, keys["totaltracks"], row["proposed_totaltracks"])
        written["totaltracks"] = row["proposed_totaltracks"]

    if row.get("release_group_mbid") and tag_is_empty(audio, keys["rg_id"]):
        set_text(audio, keys["rg_id"], row["release_group_mbid"])
        written["rg_id"] = row["release_group_mbid"]

    if row.get("release_track_mbid") and tag_is_empty(audio, keys["track_id"]):
        set_text(audio, keys["track_id"], row["release_track_mbid"])
        written["track_id"] = row["release_track_mbid"]

    if not dry_run:
        audio.save()
    return "written", written


def verify_one(row, written):
    full = LIBRARY_DIR / row["path"]
    audio, keys = read_audio(full)
    suffix = full.suffix.lower()
    mismatches = []

    def get_str(key):
        v = audio.tags.get(key)
        return v[0] if v else None

    if "album" in written and get_str(keys["album"]) != row["proposed_album"]:
        mismatches.append("album")
    if "albumartist" in written and get_str(keys["albumartist"]) != row["proposed_albumartist"]:
        mismatches.append("albumartist")
    if "date" in written and get_str(keys["date"]) != row["proposed_date"]:
        mismatches.append("date")
    if "track" in written:
        if suffix == ".opus":
            if get_str(keys["track"]) != row["proposed_track"]:
                mismatches.append("track")
        else:
            trkn = audio.tags.get(keys["track"])
            if not trkn or str(trkn[0][0]) != row["proposed_track"]:
                mismatches.append("track")
    if "rg_id" in written and get_str(keys["rg_id"]) != row["release_group_mbid"]:
        mismatches.append("rg_id")
    if "track_id" in written and get_str(keys["track_id"]) != row["release_track_mbid"]:
        mismatches.append("track_id")

    return mismatches


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan_csv")
    ap.add_argument("--ranks", required=True, help="comma-separated ranks to apply, e.g. 1,4")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    allowed_ranks = set(args.ranks.split(","))

    with open(args.plan_csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    targets = [r for r in rows if r["action"] == "fill" and r["rank"] in allowed_ranks]
    print(f"Target rows (fill, rank in {allowed_ranks}): {len(targets)}", file=sys.stderr)

    counts = {"written": 0, "already-tagged": 0, "missing-file": 0, "error": 0}
    mismatch_files = []

    for i, row in enumerate(targets, 1):
        try:
            status, written = apply_one(row, dry_run=args.dry_run)
            counts[status] = counts.get(status, 0) + 1
            if status == "written" and not args.dry_run:
                mismatches = verify_one(row, written)
                if mismatches:
                    mismatch_files.append((row["path"], mismatches))
                    print(f"MISMATCH {row['path']}: {mismatches}", file=sys.stderr)
        except Exception as e:
            counts["error"] += 1
            print(f"ERROR {row['path']}: {e}", file=sys.stderr)

        if i % 20 == 0:
            print(f"  ...{i}/{len(targets)}", file=sys.stderr)

    print(f"\n{counts}")
    print(f"Mismatches after re-read: {len(mismatch_files)}")
    for p, m in mismatch_files:
        print(f"  {p}: {m}")


if __name__ == "__main__":
    main()
