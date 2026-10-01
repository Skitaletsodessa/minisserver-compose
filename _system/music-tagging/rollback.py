"""Rollback for Task 19 step 3 - restores every tag field from the step-1 before-JSON
snapshot, for one file or every file it covers.

The snapshot never holds real cover-art bytes (diagnose.py wrote "<binary, omitted>"
as a placeholder, deliberately, to keep the JSON small) - step 3 never touches cover
art either, so rollback leaves the binary picture field alone rather than writing the
placeholder string into it.

Usage: rollback.py <before-json-path> [relative/path/to/one/file.opus]
       (omit the second argument to roll back every file the snapshot covers)
"""
import json
import sys
from pathlib import Path

from mutagen.oggopus import OggOpus
from mutagen.mp4 import MP4

LIBRARY_DIR = Path("/srv/library/music")

BINARY_KEYS = {"metadata_block_picture", "covr"}


def restore_one(rel_path: str, snapshot_tags: dict) -> bool:
    full = LIBRARY_DIR / rel_path
    if not full.exists():
        print(f"SKIP (gone): {rel_path}", file=sys.stderr)
        return False

    suffix = full.suffix.lower()
    if suffix == ".opus":
        audio = OggOpus(full)
    elif suffix == ".m4a":
        audio = MP4(full)
    else:
        print(f"SKIP (unsupported): {rel_path}", file=sys.stderr)
        return False

    # Remove every current text key except the binary picture, then restore exactly
    # what the snapshot had (also excluding the binary placeholder).
    for k in list(audio.tags.keys()) if audio.tags else []:
        if k not in BINARY_KEYS:
            del audio.tags[k]

    for k, v in snapshot_tags.items():
        if k in BINARY_KEYS:
            continue
        audio.tags[k] = v

    audio.save()
    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: rollback.py <before-json-path> [relative/path/to/file]", file=sys.stderr)
        sys.exit(1)

    json_path = Path(sys.argv[1])
    only_path = sys.argv[2] if len(sys.argv) > 2 else None

    with open(json_path, encoding="utf-8") as f:
        snapshot = json.load(f)

    by_path = {entry["path"]: entry["tags"] for entry in snapshot}

    targets = [only_path] if only_path else list(by_path.keys())

    restored = skipped = 0
    for rel_path in targets:
        if rel_path not in by_path:
            print(f"NOT IN SNAPSHOT: {rel_path}", file=sys.stderr)
            skipped += 1
            continue
        ok = restore_one(rel_path, by_path[rel_path])
        if ok:
            restored += 1
        else:
            skipped += 1

    print(f"Restored {restored}, skipped {skipped}")


if __name__ == "__main__":
    main()
