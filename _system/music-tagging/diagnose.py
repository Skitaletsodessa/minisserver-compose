"""Step 1 (Task 19) - read-only tag dump and diagnosis. No network access.

Dumps every file's full tag set to /srv/data/music-tags/before-<date>.json and prints
summary counts (files with/without Album/Title/Artist/Date/Track), plus a check for
Album data hiding under an unexpected key/case or in a non-Vorbis-comment container.
"""
import json
import sys
from datetime import date
from pathlib import Path

from mutagen.oggopus import OggOpus
from mutagen.mp4 import MP4
from mutagen import File as MutagenFile

LIBRARY_DIR = Path("/srv/library/music")
OUT_DIR = Path("/srv/data/music-tags")


def read_tags(path: Path):
    """Returns (tag_dict, container_kind) using the format-specific class so we see
    every raw key as mutagen parsed it, plus a generic File() pass as a cross-check
    for anything the specific class might not surface (e.g. a stray ID3 frame)."""
    tags = {}
    try:
        if path.suffix.lower() == ".opus":
            audio = OggOpus(path)
        elif path.suffix.lower() == ".m4a":
            audio = MP4(path)
        else:
            return None, "unsupported"
        if audio.tags:
            for k, v in audio.tags.items():
                if k == "metadata_block_picture" or k == "covr":
                    tags[k] = "<binary, omitted>"
                else:
                    tags[k] = list(v) if isinstance(v, list) else v
    except Exception as e:
        return None, f"error: {e}"

    # Cross-check with mutagen's generic auto-detector in case a different
    # container (e.g. leftover ID3) is present alongside the native one.
    other_containers = []
    try:
        generic = MutagenFile(path)
        if generic is not None and type(generic) not in (OggOpus, MP4):
            other_containers.append(type(generic).__name__)
    except Exception:
        pass

    return tags, other_containers


ALBUM_LIKE_KEYS_OPUS = {"album", "albumname", "talb"}
ALBUM_LIKE_KEYS_M4A = {"\xa9alb"}


def has_album(tags, suffix):
    if not tags:
        return False
    keys = ALBUM_LIKE_KEYS_OPUS if suffix == ".opus" else ALBUM_LIKE_KEYS_M4A
    for k in tags:
        if k.lower() in keys and tags[k]:
            return True
    return False


def has_any_key_matching(tags, substr):
    """Catch album data under an unexpected key name/case."""
    hits = []
    for k, v in (tags or {}).items():
        if "alb" in k.lower() and k.lower() not in ALBUM_LIKE_KEYS_OPUS and k.lower() not in ALBUM_LIKE_KEYS_M4A:
            hits.append((k, v))
    return hits


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    out_path = OUT_DIR / f"before-{today}.json"

    import os
    files = []
    for root, dirs, filenames in os.walk(LIBRARY_DIR):
        if "_playlists" in root:
            continue
        for fn in filenames:
            if fn.lower().endswith((".opus", ".m4a")):
                files.append(Path(root) / fn)

    files.sort()
    print(f"Files to inspect: {len(files)}", file=sys.stderr)

    dump = []
    counts = {
        "total": 0,
        "has_album": 0, "no_album": 0,
        "has_title": 0, "no_title": 0,
        "has_artist": 0, "no_artist": 0,
        "has_date": 0, "no_date": 0,
        "has_track": 0, "no_track": 0,
    }
    weird_album_keys = []
    other_container_hits = []

    for i, path in enumerate(files, 1):
        tags, other_containers = read_tags(path)
        suffix = path.suffix.lower()
        rel = str(path.relative_to(LIBRARY_DIR))

        dump.append({"path": rel, "tags": tags or {}, "other_containers": other_containers if isinstance(other_containers, list) else str(other_containers)})

        counts["total"] += 1
        album_present = has_album(tags, suffix)
        counts["has_album" if album_present else "no_album"] += 1

        title_key = "title" if suffix == ".opus" else "\xa9nam"
        artist_key = "artist" if suffix == ".opus" else "\xa9art"
        date_key = "date" if suffix == ".opus" else "\xa9day"
        track_key = "tracknumber" if suffix == ".opus" else "trkn"

        counts["has_title" if (tags or {}).get(title_key) else "no_title"] += 1
        counts["has_artist" if (tags or {}).get(artist_key) else "no_artist"] += 1
        counts["has_date" if (tags or {}).get(date_key) else "no_date"] += 1
        counts["has_track" if (tags or {}).get(track_key) else "no_track"] += 1

        weird = has_any_key_matching(tags, "alb")
        if weird:
            weird_album_keys.append((rel, weird))
        if other_containers and isinstance(other_containers, list) and other_containers:
            other_container_hits.append((rel, other_containers))

        if i % 200 == 0:
            print(f"  ...{i}/{len(files)}", file=sys.stderr)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(dump, f, ensure_ascii=False, indent=1)

    print(f"\nWrote {out_path} ({len(dump)} entries)")
    print("\n=== counts ===")
    for k, v in counts.items():
        print(f"{k}: {v}")

    print(f"\nFiles with an unexpected 'alb*'-like key (not the standard field): {len(weird_album_keys)}")
    for rel, weird in weird_album_keys[:20]:
        print(f"  {rel}: {weird}")

    print(f"\nFiles where mutagen's generic detector found a different container type: {len(other_container_hits)}")
    for rel, oc in other_container_hits[:20]:
        print(f"  {rel}: {oc}")


if __name__ == "__main__":
    main()
