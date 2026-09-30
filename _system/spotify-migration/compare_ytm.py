#!/usr/bin/env python3
"""Compare Spotify's all-tracks.csv against Ivan's existing YouTube Music library
(read-only, via browser-auth). Writes /srv/data/playlists/ytmusic-comparison-YYYY-MM-DD.md.
"""
import csv
import datetime
import re
import sys
import unicodedata
from pathlib import Path

from ytmusicapi import YTMusic

HERE = Path(__file__).parent
OUT_DIR = Path("/srv/data/playlists")

yt = YTMusic(str(HERE / "browser.json"))


def norm(s: str) -> str:
    # Apostrophes dropped (not turned into a space) BEFORE the ascii-encode, and consistently
    # regardless of character used - straight ' (ASCII) survives encode/ignore, curly '/'
    # (non-ASCII) gets silently dropped by it - that asymmetry alone turned "fool's" into
    # "fool s" from one source and "fools" from the other, breaking every match depending on
    # it. Found via create_ytm_playlist.py's pilot run; fixed there and mirrored here since
    # this script's own sp_only/yt_only split has exactly the same exposure.
    s = (s or "").replace("'", "").replace("’", "").replace("‘", "")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    return s


# --- pull every YT Music playlist's tracks ---
playlists = yt.get_library_playlists(limit=100)
yt_tracks = {}  # normalized "title|artist" -> (title, artist, playlist_names set)
yt_playlist_summary = []

for pl in playlists:
    pid = pl.get("playlistId")
    title = pl.get("title")
    if pid in ("SE",):  # Episodes for Later - podcasts, not music, skip
        continue
    try:
        if pid == "LM":
            # the built-in Liked Music pseudo-playlist paginates differently -
            # get_playlist("LM") silently caps at 100, confirmed empirically
            # (real count was 1407); get_liked_songs() is the correct call.
            detail = yt.get_liked_songs(limit=5000)
        else:
            detail = yt.get_playlist(pid, limit=5000)
    except Exception as e:
        print(f"Could not fetch {title} ({pid}): {e}", file=sys.stderr)
        continue
    tracks = detail.get("tracks", [])
    yt_playlist_summary.append((title, pid, len(tracks)))
    for t in tracks:
        name = t.get("title", "")
        artists = ", ".join(a["name"] for a in t.get("artists", []) if a.get("name"))
        key = norm(name) + "|" + norm(artists.split(",")[0] if artists else "")
        if key not in yt_tracks:
            yt_tracks[key] = (name, artists, set())
        yt_tracks[key][2].add(title)
    print(f"{title}: {len(tracks)} tracks fetched", file=sys.stderr)

# --- load Spotify's combined list ---
sp_tracks = {}  # same normalized key -> (title, artist, sources)
with open(OUT_DIR / "all-tracks.csv", newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        title = row["title"]
        artist = row["artists"].split(",")[0] if row["artists"] else ""
        key = norm(title) + "|" + norm(artist)
        sp_tracks[key] = (title, row["artists"], row["sources"])

sp_keys = set(sp_tracks)
yt_keys = set(yt_tracks)
both = sp_keys & yt_keys
sp_only = sp_keys - yt_keys
yt_only = yt_keys - sp_keys

# --- write report ---
today = datetime.date.today().isoformat()
out_path = OUT_DIR / f"ytmusic-comparison-{today}.md"
lines = [
    f"# Spotify vs YouTube Music comparison — {today}",
    "",
    "Matching by normalized (title, first artist) — not an ID match (Spotify and YouTube Music",
    "tracks have no shared identifier), so this is fuzzy: different remasters/live versions/",
    "features may show as mismatches either way. Treat as a strong indicator, not exact.",
    "",
    "## YouTube Music library",
    "",
]
for title, pid, n in yt_playlist_summary:
    lines.append(f"- **{title}** (`{pid}`): {n} tracks")
lines += [
    "",
    f"- Total unique tracks across YT Music playlists (by normalized title+artist): **{len(yt_tracks)}**",
    "",
    "## Comparison against Spotify's all-tracks.csv",
    "",
    f"- Spotify unique tracks: **{len(sp_tracks)}**",
    f"- YouTube Music unique tracks: **{len(yt_tracks)}**",
    f"- **In both**: {len(both)}",
    f"- **Only in Spotify** (candidates to still transfer): {len(sp_only)}",
    f"- **Only in YouTube Music** (not from this Spotify account, or already there from elsewhere): {len(yt_only)}",
    "",
    "## Tracks only in Spotify (candidates for transfer)",
    "",
    "| Title | Artists | Spotify source |",
    "|---|---|---|",
]
for key in sorted(sp_only, key=lambda k: sp_tracks[k][0].lower()):
    title, artists, sources = sp_tracks[key]
    lines.append(f"| {title} | {artists} | {sources} |")

out_path.write_text("\n".join(lines) + "\n")

# also a clean CSV of the transfer candidates, for scripted consumption (create_ytm_playlist.py)
transfer_csv = OUT_DIR / "transfer-candidates.csv"
with open(transfer_csv, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["title", "artists", "spotify_source"])
    for key in sorted(sp_only, key=lambda k: sp_tracks[k][0].lower()):
        title, artists, sources = sp_tracks[key]
        w.writerow([title, artists, sources])

print(f"\nWrote {out_path}", file=sys.stderr)
print(f"Wrote {transfer_csv} ({len(sp_only)} rows)", file=sys.stderr)
print(f"In both: {len(both)}, only Spotify: {len(sp_only)}, only YTM: {len(yt_only)}", file=sys.stderr)
