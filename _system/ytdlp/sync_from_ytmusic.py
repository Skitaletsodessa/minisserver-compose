#!/usr/bin/env python3
"""Pulls Ivan's current YouTube Music library (Liked Music + every playlist) and downloads
audio for any track not already downloaded, tagged from YouTube Music's own title/artist.

Spotify is no longer part of this pipeline (Ivan's decision, 2026-09-30) - this reads directly
from YouTube Music via ytmusicapi/browser-auth, the same credential already confirmed working
for both reads and writes on this account. Meant to run unattended on a timer (see
ytmusic-sync.service/.timer); every step is resumable and safe to re-run.

Usage: sync_from_ytmusic.py [--limit N] [--dry-run]
"""
import argparse
import csv
import re
import shlex
import subprocess
import sys
import unicodedata
from pathlib import Path

from ytmusicapi import YTMusic

HERE = Path(__file__).parent
LIBRARY_DIR = Path("/srv/library/music")
DOWNLOAD_LOG = HERE / "download-progress.csv"
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
            w.writerow(["video_id", "title", "artists", "source", "status", "path"])
        w.writerow(row)


def fetch_library_tracks(yt):
    """Every track across Liked Music and every playlist, deduplicated by videoId."""
    tracks = {}  # videoId -> (title, artists, source_label)

    liked = yt.get_liked_songs(limit=5000)
    for t in liked.get("tracks", []):
        vid = t.get("videoId")
        if not vid:
            continue
        artists = ", ".join(a["name"] for a in t.get("artists", []) if a.get("name"))
        tracks.setdefault(vid, (t.get("title", ""), artists, "Liked Music"))

    for pl in yt.get_library_playlists(limit=100):
        pid, title = pl.get("playlistId"), pl.get("title")
        if pid in ("LM", "SE"):  # Liked Music already covered above; Episodes for Later = podcasts
            continue
        try:
            detail = yt.get_playlist(pid, limit=5000)
        except Exception as e:
            print(f"Could not fetch playlist {title!r} ({pid}): {e}", file=sys.stderr)
            continue
        for t in detail.get("tracks", []):
            vid = t.get("videoId")
            if not vid:
                continue
            artists = ", ".join(a["name"] for a in t.get("artists", []) if a.get("name"))
            tracks.setdefault(vid, (t.get("title", ""), artists, f"playlist:{title}"))

    return tracks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--format", default="best")
    args = ap.parse_args()

    yt = YTMusic(str(HERE / "browser.json"))

    print("Fetching current YouTube Music library...", file=sys.stderr)
    library = fetch_library_tracks(yt)
    print(f"{len(library)} tracks in Liked Music + playlists", file=sys.stderr)

    done = load_done()
    todo = [(vid, title, artists, source) for vid, (title, artists, source) in library.items() if vid not in done]
    if args.limit is not None:
        todo = todo[: args.limit]

    print(f"{len(done)} already downloaded, {len(todo)} new track(s) to download", file=sys.stderr)
    if not todo:
        return

    downloaded = failed = 0
    for vid, title, artists, source in todo:
        artist_for_path = (artists.split(",")[0].strip() if artists else "") or "Unknown Artist"
        out_dir = LIBRARY_DIR / safe_component(artist_for_path)
        out_path_template = str(out_dir / f"{safe_component(title)}.%(ext)s")

        if args.dry_run:
            print(f"  WOULD DOWNLOAD: {artist_for_path} / {title}  ({vid}, {source})  ->  {out_path_template}")
            continue

        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(YT_DLP),
            "-x", "--audio-format", args.format,
            "--embed-thumbnail", "--no-embed-metadata",
            "--postprocessor-args",
            "ffmpeg:-metadata title=" + shlex.quote(title) + " -metadata artist=" + shlex.quote(artist_for_path),
            "-o", out_path_template,
            "--no-progress", "--quiet", "--no-warnings",
            f"https://music.youtube.com/watch?v={vid}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            append_log([vid, title, artists, source, "downloaded", out_path_template])
            downloaded += 1
            print(f"  OK: {artist_for_path} / {title}", file=sys.stderr)
        else:
            append_log([vid, title, artists, source, "failed", ""])
            failed += 1
            print(f"  FAILED: {artist_for_path} / {title}: {result.stderr[-300:]}", file=sys.stderr)

    print(f"\nDownloaded {downloaded}, failed {failed}", file=sys.stderr)

    # An unattended/cron run must not report success by staying quiet when every single
    # attempt failed (e.g. the browser-auth session expired and every yt-dlp call errors) -
    # per-track failures are caught and logged individually above, but if there was real work
    # and none of it succeeded, systemd's OnFailure= should hear about it, not see exit 0.
    if not args.dry_run and len(todo) > 0 and downloaded == 0 and failed > 0:
        print("Every download attempt failed - treating this as a hard failure.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
