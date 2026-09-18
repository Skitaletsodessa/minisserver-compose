#!/usr/bin/env python3
"""Sort finished content from /srv/library/downloads/{movies,shows,cartoons}
into the Jellyfin-facing /srv/library/video/ structure via hardlinks.

Everything this script does to a file under downloads/ is additive: it only
ever creates hardlinks (into video/ or into _unsorted/), never moves or
deletes anything. qBittorrent keeps seeding the original in downloads/
untouched regardless of what happens here - including files that turn out
to be RAR archives or unparseable, which get a *link* placed in _unsorted/
for a human to deal with rather than being relocated out of qBittorrent's
download directory.
"""
import argparse
import json
import logging
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from guessit import guessit

DOWNLOADS = Path("/srv/library/downloads")
VIDEO = Path("/srv/library/video")
UNSORTED = DOWNLOADS / "_unsorted"
CATEGORIES = ("movies", "shows", "cartoons")

STATE_DIR = Path("/var/lib/library-sort")
STATE_FILE = STATE_DIR / "seen-sizes.json"

ENV_FILE = Path("/srv/compose/scrutiny/.env")

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov"}
SUBTITLE_EXT = {".srt", ".ass"}
RAR_EXT = {".rar"}
JUNK_NAME_RE_PARTS = ("sample", "trailer")
JUNK_EXT = {".nfo", ".txt", ".jpg", ".jpeg", ".png", ".sfv", ".idx", ".sub"}
MIN_VIDEO_BYTES = 50 * 1024 * 1024  # 50 MB - below this, treat as a sample/junk, not real content
INCOMPLETE_SUFFIX = ".!qb"  # qBittorrent's incomplete-file marker, compared case-insensitively

log = logging.getLogger("library-sort")


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state):
    # Not gated on --dry-run: recording "this file's size was X as of this run" is
    # bookkeeping, not a library change, and --dry-run's only real promise is that it
    # never touches downloads/ or video/. Skipping this under --dry-run would mean two
    # dry-run passes in a row could never demonstrate the stable-size path at all.
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def is_rar(p: Path) -> bool:
    if p.suffix.lower() in RAR_EXT:
        return True
    # .r00, .r01, ... and .part1.rar, .part01.rar style parts
    name = p.name.lower()
    if len(p.suffix) == 4 and p.suffix[1] == "r" and p.suffix[2:].isdigit():
        return True
    return "part" in name and ".rar" in name


def is_junk(p: Path) -> bool:
    name_lower = p.name.lower()
    if p.suffix.lower() in JUNK_EXT:
        return True
    if any(part in name_lower for part in JUNK_NAME_RE_PARTS):
        return True
    return False


def is_incomplete(p: Path) -> bool:
    return p.name.lower().endswith(INCOMPLETE_SUFFIX)


def telegram_alert(message: str, dry_run: bool):
    log.warning("ALERT: %s", message)
    if dry_run:
        return
    if not ENV_FILE.exists():
        log.error("cannot send Telegram alert: %s not found", ENV_FILE)
        return
    creds = {}
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            creds[k.strip()] = v.strip()
    token = creds.get("TELEGRAM_BOT_TOKEN")
    chat_id = creds.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.error("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing from %s", ENV_FILE)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": f"minisserver library-sort: {message}"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    except Exception as e:  # noqa: BLE001 - alerting must never crash the run
        log.error("Telegram send failed: %s", e)


def target_for_movie(info, ext) -> Path:
    title, year = info["title"], info["year"]
    folder = f"{title} ({year})"
    return VIDEO / "movies" / folder / f"{folder}{ext}"


def target_for_episode(info, ext, category) -> Path:
    title = info["title"]
    season = info["season"]
    episode = info["episode"]
    show_folder = title  # no year available from guessit for most TV releases; use title alone
    season_folder = f"Season {season:02d}"
    filename = f"{title} - S{season:02d}E{episode:02d}{ext}"
    return VIDEO / category / show_folder / season_folder / filename


def confident_guess(rel_hint: str, filename: str):
    """Returns (kind, info) where kind is 'movie'/'episode', or (None, None) if not confident enough."""
    info = guessit(f"{rel_hint}/{filename}" if rel_hint else filename)
    info = dict(info)
    if info.get("type") == "movie" and info.get("title") and info.get("year"):
        return "movie", info
    if info.get("type") == "episode" and info.get("title") and info.get("season") is not None and info.get("episode") is not None:
        return "episode", info
    return None, None


def link_into_unsorted(src: Path, reason: str, dry_run: bool):
    UNSORTED.mkdir(parents=True, exist_ok=True)
    dest = UNSORTED / src.name
    if dest.exists():
        try:
            if dest.stat().st_ino == src.stat().st_ino:
                log.info("already in _unsorted, skip: %s", src)
                return
        except OSError:
            pass
        log.warning("name collision in _unsorted, not overwriting: %s (existing: %s)", src, dest)
        return
    log.info("[UNSORTED] %s -> %s (%s)", src, dest, reason)
    if not dry_run:
        os.link(src, dest)
    telegram_alert(f"{reason}: {src.relative_to(DOWNLOADS)} -> _unsorted/{dest.name}", dry_run)


def link_media(src: Path, dest: Path, dry_run: bool):
    if dest.exists():
        try:
            if dest.stat().st_ino == src.stat().st_ino:
                log.debug("already linked, skip: %s", src)
                return True
        except OSError:
            pass
        log.warning("target already exists and is a different file, not overwriting: %s", dest)
        return False
    log.info("[LINK] %s -> %s", src, dest)
    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.link(src, dest)
    return True


def carry_subtitles(src: Path, dest: Path, dry_run: bool):
    stem = src.name[: -len(src.suffix)] if src.suffix else src.name
    dest_stem = dest.name[: -len(dest.suffix)] if dest.suffix else dest.name
    for sib in src.parent.iterdir():
        if sib == src or not sib.is_file():
            continue
        if sib.suffix.lower() not in SUBTITLE_EXT:
            continue
        if not sib.name.startswith(stem):
            continue
        suffix = sib.name[len(stem):]  # e.g. ".ru.srt" or ".srt"
        sub_dest = dest.parent / f"{dest_stem}{suffix}"
        link_media(sib, sub_dest, dry_run)


def is_stable(path: Path, state: dict) -> bool:
    key = str(path)
    try:
        size = path.stat().st_size
    except OSError:
        return False
    prev = state.get(key)
    state[key] = size
    return prev is not None and prev == size


def process_video_file(src: Path, category: str, rel_hint: str, state: dict, dry_run: bool):
    # is_rar/JUNK_EXT-driven skips and the .!qB incomplete marker are all filtered by
    # the caller (process_item) before this is reached; this only ever sees a
    # plausible, complete, non-junk video file.
    if is_incomplete(src):
        log.debug("incomplete marker present, skip: %s", src)
        return
    if src.suffix.lower() not in VIDEO_EXT:
        return
    try:
        if src.stat().st_size < MIN_VIDEO_BYTES:
            log.debug("implausibly small for video, skip: %s", src)
            return
    except OSError:
        return

    kind, info = confident_guess(rel_hint, src.name)
    if kind is None:
        link_into_unsorted(src, "could not confidently parse title/year or title/season/episode", dry_run)
        return

    dest = target_for_movie(info, src.suffix) if kind == "movie" else target_for_episode(info, src.suffix, category)

    # fast path: already correctly linked from a previous run, nothing to do
    if dest.exists():
        try:
            if dest.stat().st_ino == src.stat().st_ino:
                return
        except OSError:
            pass

    if not is_stable(src, state):
        log.debug("not yet size-stable across two checks, skip for now: %s", src)
        return

    if link_media(src, dest, dry_run):
        carry_subtitles(src, dest, dry_run)


def process_item(item: Path, category: str, state: dict, dry_run: bool):
    if item.is_file():
        if is_rar(item):
            link_into_unsorted(item, "RAR archive, not unpacked", dry_run)
            return
        process_video_file(item, category, "", state, dry_run)
        return

    # directory: season pack / multi-file torrent - walk it
    for root, _dirs, files in os.walk(item):
        root_path = Path(root)
        rel_hint = str(root_path.relative_to(item.parent))
        for fname in files:
            fpath = root_path / fname
            if is_junk(fpath) or is_incomplete(fpath):
                continue
            if is_rar(fpath):
                link_into_unsorted(fpath, "RAR archive, not unpacked", dry_run)
                continue
            if fpath.suffix.lower() in VIDEO_EXT:
                process_video_file(fpath, category, rel_hint, state, dry_run)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="log intended actions, make no filesystem changes")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )

    state = load_state()
    # prune entries for paths that no longer exist
    state = {k: v for k, v in state.items() if os.path.exists(k)}

    for category in CATEGORIES:
        cat_dir = DOWNLOADS / category
        if not cat_dir.is_dir():
            continue
        for item in sorted(cat_dir.iterdir()):
            process_item(item, category, state, args.dry_run)

    save_state(state)


if __name__ == "__main__":
    main()
