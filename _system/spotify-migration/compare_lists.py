#!/usr/bin/env python3
"""Task 14 follow-up (Ivan's request): compare the three exported lists (Best, First, Liked
Songs) and merge into one list, showing which source(s) each track came from - not just a
flat dedup. Reads the CSVs already written by export_csv.py; writes no new API calls.
"""
import csv
from pathlib import Path

OUT_DIR = Path("/srv/data/playlists")
SOURCES = [
    ("Best", OUT_DIR / "playlist-Best.csv"),
    ("First", OUT_DIR / "playlist-First.csv"),
    ("Liked Songs", OUT_DIR / "liked-songs.csv"),
]

tracks = {}  # spotify_track_id -> {row fields..., "sources": set()}

for label, path in SOURCES:
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tid = row["spotify_track_id"]
            if not tid:
                continue
            if tid not in tracks:
                tracks[tid] = {
                    "title": row["title"],
                    "artists": row["artists"],
                    "album": row["album"],
                    "duration_ms": row["duration_ms"],
                    "isrc": row["isrc"],
                    "spotify_track_id": tid,
                    "sources": set(),
                }
            tracks[tid]["sources"].add(label)

rows = sorted(tracks.values(), key=lambda r: r["title"].lower())

out_path = OUT_DIR / "all-tracks.csv"
with open(out_path, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["title", "artists", "album", "duration_ms", "isrc", "spotify_track_id", "sources"])
    for r in rows:
        w.writerow(
            [r["title"], r["artists"], r["album"], r["duration_ms"], r["isrc"], r["spotify_track_id"],
             " + ".join(sorted(r["sources"]))]
        )

# --- overlap summary ---
from itertools import combinations

label_sets = {label: set() for label, _ in SOURCES}
for tid, r in tracks.items():
    for s in r["sources"]:
        label_sets[s].add(tid)

print(f"Total unique tracks across all three: {len(tracks)}")
print()
for label, _ in SOURCES:
    print(f"  {label}: {len(label_sets[label])} tracks")
print()
print("Overlaps:")
labels = [label for label, _ in SOURCES]
for a, b in combinations(labels, 2):
    both = label_sets[a] & label_sets[b]
    print(f"  {a} ∩ {b}: {len(both)}")
all_three = label_sets[labels[0]] & label_sets[labels[1]] & label_sets[labels[2]]
print(f"  {labels[0]} ∩ {labels[1]} ∩ {labels[2]}: {len(all_three)}")
print()
only = {}
for label in labels:
    others = set().union(*[label_sets[o] for o in labels if o != label])
    only[label] = label_sets[label] - others
    print(f"  Only in {label}: {len(only[label])}")

print()
print(f"Wrote {out_path} ({len(rows)} rows, with a 'sources' column)")
