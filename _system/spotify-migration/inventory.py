#!/usr/bin/env python3
"""Task 14 §1 - read-only Spotify inventory. Writes /srv/data/playlists/inventory-YYYY-MM-DD.md.
Makes no changes on either service; only reads. First run will need interactive OAuth
(a URL to open, then paste back the redirect URL) if no cached token exists yet.
"""
import configparser
import datetime
import sys
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
print(f"Authenticated as: {me['id']} ({me.get('display_name', '')})", file=sys.stderr)

# --- playlists (owned + followed) ---
# current_user_playlists can return None items, or items missing "tracks", for playlists the
# API can enumerate but not fully describe (seen: a followed playlist whose owner deleted it, or
# made it unavailable in some other way). Don't crash on that - record it as a problem instead
# of silently losing it or losing the whole inventory to one bad entry.
raw_playlists = []
offset = 0
while True:
    page = sp.current_user_playlists(limit=50, offset=offset)
    raw_playlists.extend(page["items"])
    if page["next"] is None:
        break
    offset += 50

rows = []
problems = []
total_tracks_sum = 0
for pl in raw_playlists:
    if pl is None:
        problems.append("(null playlist entry - Spotify returned no data for it at all)")
        continue
    name = pl.get("name", "(no name)")
    pid = pl.get("id", "(no id)")
    owner = (pl.get("owner") or {}).get("id", "(unknown owner)")
    mine = owner == me["id"]
    # Spotify's current Web API response uses "items" here, not "tracks" (older docs/tutorials
    # still say "tracks" - checked directly against a live response, both playlists in this
    # account had "items" and no "tracks" key at all). Accept either, prefer "items".
    tracks_obj = pl.get("items") or pl.get("tracks")
    if tracks_obj is None or "total" not in tracks_obj:
        problems.append(
            f"'{name}' (id={pid}, owner={owner}) - no track count in the API response, "
            "likely unavailable/removed on the owner's side; excluded from totals below"
        )
        rows.append(
            {
                "name": name, "id": pid, "owner": owner, "mine": mine,
                "public": pl.get("public"), "collaborative": pl.get("collaborative"),
                "track_count": None,
            }
        )
        continue
    track_count = tracks_obj["total"]
    rows.append(
        {
            "name": name, "id": pid, "owner": owner, "mine": mine,
            "public": pl.get("public"), "collaborative": pl.get("collaborative"),
            "track_count": track_count,
        }
    )
    total_tracks_sum += track_count

# --- liked songs count ---
liked = sp.current_user_saved_tracks(limit=1)
liked_total = liked["total"]

# --- unique track count across everything (by track id) ---
# Field-name trap, confirmed directly against a live response, not assumed from older docs:
# /playlists/{id}/items nests each track under "item" (not "track" - that field doesn't exist
# in this endpoint's response at all), and the "fields" partial-response filter needs
# parentheses for the nesting: "items(item(id)),next". /me/tracks (Liked Songs, below) is a
# different endpoint and still uses "track" - the two are not interchangeable, checked separately.
unique_ids = set()
for r in rows:
    if r["track_count"] is None:
        continue
    offset = 0
    while True:
        page = sp.playlist_items(
            r["id"], fields="items(item(id)),next", additional_types=["track"], offset=offset, limit=100
        )
        for entry in page["items"]:
            t = entry.get("item")
            if t and t.get("id"):
                unique_ids.add(t["id"])
        if page["next"] is None:
            break
        offset += 100

offset = 0
while True:
    page = sp.current_user_saved_tracks(limit=50, offset=offset)
    for item in page["items"]:
        t = item.get("track")
        if t and t.get("id"):
            unique_ids.add(t["id"])
    if page["next"] is None:
        break
    offset += 50

# --- write inventory ---
OUT_DIR.mkdir(parents=True, exist_ok=True)
today = datetime.date.today().isoformat()
out_path = OUT_DIR / f"inventory-{today}.md"

lines = [
    f"# Spotify inventory — {today}",
    "",
    f"Account: `{me['id']}`",
    "",
    f"- Playlists (owned + followed): **{len(rows)}**" + (f" ({len(problems)} with problems, see below)" if problems else ""),
    f"- Liked Songs: **{liked_total}**",
    f"- Sum of playlist track counts (with duplicates across playlists, excludes playlists with unknown count): {total_tracks_sum}",
    f"- **Unique tracks total (playlists + Liked Songs, deduplicated by Spotify track id): {len(unique_ids)}**",
    "",
]

if problems:
    lines += ["## Problems encountered (not silently dropped)", ""]
    for p in problems:
        lines.append(f"- {p}")
    lines.append("")

lines += [
    "## Playlists",
    "",
    "| Name | Owner | Mine | Public | Collaborative | Tracks |",
    "|---|---|---|---|---|---|",
]
for r in sorted(rows, key=lambda r: r["name"].lower()):
    tc = r["track_count"] if r["track_count"] is not None else "?"
    lines.append(
        f"| {r['name']} | {r['owner']} | {'yes' if r['mine'] else 'no'} | "
        f"{'yes' if r['public'] else 'no'} | {'yes' if r['collaborative'] else 'no'} | {tc} |"
    )

out_path.write_text("\n".join(lines) + "\n")
print(f"Wrote {out_path}", file=sys.stderr)
print(f"{len(rows)} playlists ({len(problems)} problems), {liked_total} liked, {len(unique_ids)} unique tracks total", file=sys.stderr)
