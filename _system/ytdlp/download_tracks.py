#!/usr/bin/env python3
"""Task 14 follow-up (Ivan's explicit override of the task's own no-audio-download rule):
download audio for the tracks matched during the Spotify->YouTube Music transfer, using the
CLEAN Spotify metadata (title/artist/album) from transfer-progress.csv for tagging - not
YouTube's own title string, which is frequently decorated ("(feat. X)", "- Remastered",
romanized-title suffixes) and would make a messy library.

Output: /srv/library/music/<Artist>/<Title>.<ext> - one file per track, best available audio,
ffmpeg-remuxed with correct id3/vorbis tags set explicitly from our own data.

This is a deliberate, explicit exception to Task 14's "no audio is ever downloaded" rule -
Ivan authorized it directly in chat after being shown the conflict. Not a default or a habit;
don't extend this pattern to future tasks without the same explicit override.

Usage: download_tracks.py --limit N [--dry-run] [--format best|opus|m4a]
"""
import argparse
import csv
import re
import shlex
import subprocess
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).parent
DATA_DIR = Path("/srv/data/playlists")
PROGRESS_CSV = DATA_DIR / "transfer-progress.csv"
LIBRARY_DIR = Path("/srv/library/music")
DOWNLOAD_LOG = DATA_DIR / "download-progress.csv"
YT_DLP = HERE / "venv" / "bin" / "yt-dlp"


def safe_component(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = re.sub(r'[\\/:*?"<>|]', "_", s)
    s = s.strip().strip(".")
    return s[:150] or "untitled"


def load_done():
    done = set()
    if DOWNLOAD_LOG.exists():
        with open(DOWNLOAD_LOG, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["status"] == "downloaded":
                    done.add(row["video_id"])
    return done


def append_log(row):
    is_new = not DOWNLOAD_LOG.exists()
    with open(DOWNLOAD_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["video_id", "title", "artists", "status", "path"])
        w.writerow(row)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--format", default="best", help="yt-dlp --audio-format value (best/opus/m4a/mp3/...)")
    args = ap.parse_args()

    with open(PROGRESS_CSV, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["status"] == "inserted" and r["video_id"]]

    # dedupe by video_id (a retried track can have more than one "inserted" row)
    seen_vid = {}
    for r in rows:
        seen_vid[r["video_id"]] = r
    tracks = list(seen_vid.values())

    done = load_done()
    todo = [t for t in tracks if t["video_id"] not in done]
    if args.limit is not None:
        todo = todo[: args.limit]

    print(f"{len(done)} already downloaded, {len(tracks)} total matched, {len(todo)} to do this run", file=sys.stderr)
    if not todo:
        return

    for t in todo:
        artist = t["artists"].split(",")[0].strip() or "Unknown Artist"
        title = t["title"].strip()
        vid = t["video_id"]
        out_dir = LIBRARY_DIR / safe_component(artist)
        out_path_template = str(out_dir / f"{safe_component(title)}.%(ext)s")

        if args.dry_run:
            print(f"  WOULD DOWNLOAD: {artist} / {title}  ({vid})  ->  {out_path_template}")
            continue

        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(YT_DLP),
            "-x", "--audio-format", args.format,
            "--embed-thumbnail", "--no-embed-metadata",  # we set metadata ourselves below, not from YouTube's own title
            "--postprocessor-args",
            "ffmpeg:-metadata title=" + shlex.quote(title) + " -metadata artist=" + shlex.quote(artist),
            "-o", out_path_template,
            "--no-progress", "--quiet", "--no-warnings",
            f"https://music.youtube.com/watch?v={vid}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            append_log([vid, title, t["artists"], "downloaded", out_path_template])
            print(f"  OK: {artist} / {title}", file=sys.stderr)
        else:
            append_log([vid, title, t["artists"], "failed", ""])
            print(f"  FAILED: {artist} / {title}: {result.stderr[-300:]}", file=sys.stderr)


if __name__ == "__main__":
    main()
