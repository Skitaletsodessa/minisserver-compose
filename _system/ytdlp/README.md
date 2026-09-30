# ytdlp (Task 14 follow-up)

One-shot tool, not a running service. Downloads audio for the tracks matched during the
Spotify → YouTube Music transfer (`_system/spotify-migration/`), tagged with the clean Spotify
metadata rather than YouTube's own (often decorated) titles.

**This is a deliberate, explicit exception to `tasks/task-14.md`'s own hard rule ("no audio is
ever downloaded from anywhere").** Ivan asked for it directly and was shown the conflict before
confirming he wanted to override it, specifically for this task. Don't treat this as a standing
precedent for other tasks — that rule stays the default everywhere else unless overridden the
same way, explicitly, again.

## Setup

```bash
cd /srv/compose/_system/ytdlp
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

`venv/` is git-ignored, rebuild from `requirements.txt`. Needs host-level `ffmpeg` (`apt install
ffmpeg`, already done) for audio extraction/remuxing.

**Known gap, not fixed:** YouTube's extraction increasingly needs a JS runtime yt-dlp doesn't
consider `node` compatible with (only `deno`/`bun`/`quickjs`) - basic extraction still worked in
testing without one, but if downloads start failing in bulk, that's the first thing to check
(`yt-dlp --simulate -v <url>` and look for the `JS Challenge Providers` line). Installing `deno`
needs its own install script (not in Debian's repos) - ask before running anything that pipes to
a shell.

## Running it

```bash
cd /srv/compose/_system/ytdlp

# see what it would do, no downloads
venv/bin/python download_tracks.py --dry-run

# download everything not yet downloaded
venv/bin/python download_tracks.py

# just the next N, e.g. to spot-check before committing to the full run
venv/bin/python download_tracks.py --limit 20

# pick a different audio format (default: best, i.e. whatever YouTube serves, usually opus)
venv/bin/python download_tracks.py --format opus
```

Reads `/srv/data/playlists/transfer-progress.csv` (the 352 successfully matched tracks from the
YouTube Music transfer), skips anything already downloaded (tracked in
`/srv/data/playlists/download-progress.csv`, safe to re-run/resume), and writes to
`/srv/library/music/<Artist>/<Title>.<ext>` - title and artist come from Spotify's own data, set
explicitly via ffmpeg postprocessor args, not from YouTube's title string.

## What's git-ignored here

`venv/`.
