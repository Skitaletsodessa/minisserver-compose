# ytdlp

Keeps `/srv/library/music` in sync with Ivan's YouTube Music library (Liked Music + every
playlist) — downloads audio for anything new, on a timer, every 3 days.
`ytmusic-sync.service`/`.timer` (symlinked into systemd like every other `_system/` timer) is
the running piece; `sync_from_ytmusic.py` is what it runs.

**This is a deliberate, explicit exception to `tasks/task-14.md`'s own hard rule ("no audio is
ever downloaded from anywhere")** — Ivan asked for it directly and was shown the conflict before
confirming he wanted to override it. Don't treat this as a standing precedent for other tasks —
that rule stays the default everywhere else unless overridden the same way, explicitly, again.

**History:** started life as a Task 14 side-tool downloading only the tracks matched during the
one-time Spotify → YouTube Music transfer, tagged from Spotify's clean metadata
(`_system/spotify-migration/`). Ivan then decided (2026-09-30) to drop Spotify from the ongoing
picture entirely and just mirror YouTube Music directly — `download_tracks.py` (Spotify-driven,
one-shot) is kept for the record; `sync_from_ytmusic.py` (YouTube-Music-driven, the thing the
timer actually runs) is what's live now.

## Setup

```bash
cd /srv/compose/_system/ytdlp
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

`venv/` is git-ignored, rebuild from `requirements.txt`. Needs host-level `ffmpeg` (`apt install
ffmpeg`) and reads `browser.json` (git-ignored, mode 600 — the same YouTube Music browser-auth
credential `_system/spotify-migration/` uses; see that directory's README for how it's obtained).

**Known gap, not fixed:** YouTube's extraction increasingly needs a JS runtime yt-dlp doesn't
consider `node` compatible with (only `deno`/`bun`/`quickjs`) — this is the confirmed cause of
most of the real-run failures (~5%, mostly `HTTP 403 Forbidden`; a couple were legitimately
Music-Premium-only videos, nothing to fix there). Check with `yt-dlp --simulate -v <url>` and
look for the `JS Challenge Providers` line. Installing `deno` needs its own install script (not
in Debian's repos) — ask before running anything that pipes to a shell. Failed tracks are
automatically retried on the next scheduled run (not marked "done"), so this mostly self-heals
for transient failures but won't for a systemic JS-runtime gap.

## Running it

Normally nothing to do — the timer handles it. To run by hand:

```bash
cd /srv/compose/_system/ytdlp

venv/bin/python sync_from_ytmusic.py --dry-run           # see what it would do
venv/bin/python sync_from_ytmusic.py                     # download everything new
venv/bin/python sync_from_ytmusic.py --limit 20           # just the next N
venv/bin/python sync_from_ytmusic.py --format opus         # different audio format (default: best)

# or via systemd, same as the timer does it:
sudo systemctl start ytmusic-sync.service
sudo journalctl -u ytmusic-sync.service -f
```

Pulls Liked Music + every YouTube Music playlist, dedupes by video ID, skips anything already
downloaded (tracked in `download-progress.csv`, git-ignored — pure runtime cache, already
covered by the blanket `path /srv/compose` backup-set line, doesn't need its own), writes to
`/srv/library/music/<Artist>/<Title>.<ext>` tagged from YouTube Music's own title/artist via
explicit ffmpeg postprocessor args.

**An unattended run that fails on literally every track exits non-zero** (not a quiet 0/0 that
looks identical to "nothing new today") — `OnFailure=backup-notify@%n.service` sends the same
Telegram alert every other `_system/` timer uses on failure.

## What's git-ignored here

`venv/`, `browser.json`, `download-progress.csv`.
