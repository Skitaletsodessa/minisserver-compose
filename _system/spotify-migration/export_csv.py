#!/usr/bin/env python3
"""Task 14 §2 - export every Spotify playlist + Liked Songs to CSV under /srv/data/playlists/.
Read-only against Spotify; only ever writes local files. No audio touched or downloaded.
"""
import configparser
import csv
import re
import sys
import unicodedata
from pathlib import Path

import spotipy
from spotipy.oauth2 import SpotifyOAuth

HERE = Path(__file__).parent
SETTINGS = HERE / "settings.ini"
OUT_DIR = Path("/srv/data/playlists")

config = configparser.ConfigParser(interpolation=None)
config.read(SETTINGS)
client_id = config["spotify"]["client_id"]
client_secret = config["spotify"]["client_secret"]

auth = SpotifyOAuth(
    client_id=client_id,
    client_secret=client_secret,
    redirect_uri="https://127.0.0.1",
    scope="playlist-read-private playlist-read-collaborative user-library-read",
    cache_path=str(HERE / ".spotipy-cache"),
    open_browser=False,
)
sp = spotipy.Spotify(auth_manager=auth)

me = sp.current_user()
print(f"Authenticated as: {me['id']}", file=sys.stderr)

COLUMNS = ["position", "title", "artists", "album", "duration_ms", "isrc", "spotify_track_id", "added_at"]


def safe_filename(name: str) -> str:
    # NFKD-normalize and strip anything that isn't safe across ext4/backup tooling; keep it
    # readable rather than hashing, since these filenames are meant to be found by eye later.
    name = unicodedata.normalize("NFKD", name)
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.strip().strip(".")
    return name[:150] or "untitled"


def track_row(position, track, added_at):
    if track is None:
        return [position, "(unavailable - removed or region-locked)", "", "", "", "", "", added_at or ""]
    artists = ", ".join(a["name"] for a in track.get("artists", []))
    album = (track.get("album") or {}).get("name", "")
    duration_ms = track.get("duration_ms", "")
    isrc = (track.get("external_ids") or {}).get("isrc", "")
    tid = track.get("id", "") or ""
    return [position, track.get("name", ""), artists, album, duration_ms, isrc, tid, added_at or ""]


def write_csv(path: Path, rows: list[list]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        w.writerows(rows)


all_tracks_seen: dict[str, list] = {}  # spotify_track_id -> row (for the deduped combined file)
summary = []

# --- playlists ---
playlists = []
offset = 0
while True:
    page = sp.current_user_playlists(limit=50, offset=offset)
    playlists.extend([p for p in page["items"] if p is not None])
    if page["next"] is None:
        break
    offset += 50

for pl in playlists:
    name = pl.get("name", "(no name)")
    pid = pl.get("id")
    if not pid:
        print(f"Skipping playlist with no id: {name!r}", file=sys.stderr)
        continue

    rows = []
    offset = 0
    position = 0
    while True:
        page = sp.playlist_items(
            pid,
            fields="items(added_at,item(id,name,duration_ms,external_ids,artists(name),album(name))),next",
            additional_types=["track"],
            offset=offset,
            limit=100,
        )
        for entry in page["items"]:
            position += 1
            row = track_row(position, entry.get("item"), entry.get("added_at"))
            rows.append(row)
            tid = row[6]
            if tid:
                all_tracks_seen.setdefault(tid, row)
        if page["next"] is None:
            break
        offset += 100

    fname = f"playlist-{safe_filename(name)}.csv"
    out_path = OUT_DIR / fname
    write_csv(out_path, rows)
    summary.append((name, pid, len(rows), fname))
    print(f"{name}: {len(rows)} rows -> {fname}", file=sys.stderr)

# --- Liked Songs ---
liked_rows = []
offset = 0
position = 0
while True:
    page = sp.current_user_saved_tracks(limit=50, offset=offset)
    for entry in page["items"]:
        position += 1
        row = track_row(position, entry.get("track"), entry.get("added_at"))
        liked_rows.append(row)
        tid = row[6]
        if tid:
            all_tracks_seen.setdefault(tid, row)
    if page["next"] is None:
        break
    offset += 50

liked_path = OUT_DIR / "liked-songs.csv"
write_csv(liked_path, liked_rows)
print(f"Liked Songs: {len(liked_rows)} rows -> liked-songs.csv", file=sys.stderr)

# --- combined, deduplicated ---
combined_rows = []
for i, (tid, row) in enumerate(sorted(all_tracks_seen.items(), key=lambda kv: kv[1][1].lower()), start=1):
    combined_rows.append([i] + row[1:])  # renumber position as a simple index in the combined file
write_csv(OUT_DIR / "all-tracks.csv", combined_rows)
print(f"all-tracks.csv: {len(combined_rows)} unique rows", file=sys.stderr)

print("", file=sys.stderr)
print("Summary:", file=sys.stderr)
for name, pid, n, fname in summary:
    print(f"  {name}: {n} tracks -> {fname}", file=sys.stderr)
print(f"  Liked Songs: {len(liked_rows)} tracks -> liked-songs.csv", file=sys.stderr)
print(f"  all-tracks.csv (deduplicated): {len(combined_rows)} tracks", file=sys.stderr)
