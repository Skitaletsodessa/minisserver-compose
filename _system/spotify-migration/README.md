# spotify-migration (Task 14)

One-shot tool, not a running service: moves Spotify playlist *lists* (titles/artists/albums,
never audio) to YouTube Music, and exports them to CSV in `/srv/data/playlists/` so the lists
outlive the Spotify subscription. Nothing here runs unattended or on a timer.

**Hard rule: no audio is ever downloaded.** `spotify_to_ytmusic` only ever reads playlist
metadata from Spotify's API and creates matching playlists on YouTube Music by search+match. If
that ever changes, stop and don't use whatever mode does it.

## Tool and pinned versions

[`spotify_to_ytmusic`](https://github.com/sigma67/spotify_to_ytmusic) 0.8.0 (sigma67, MIT,
1500+ stars, not archived) — the only actively-maintained open-source tool for this specific
job. Its own last tagged release is April 2025 (over a year old at the time of writing) but the
repo isn't archived and its dependency, `ytmusicapi`, is actively maintained by the same author
(latest release 2026-09-16). Pinned in `requirements.txt`:

```
spotify_to_ytmusic==0.8.0
ytmusicapi==1.12.3
spotipy==2.26.0
platformdirs==4.12.2
```

## Setup (venv, no secrets needed for this part)

```bash
cd /srv/compose/_system/spotify-migration
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/spotify_to_ytmusic --help   # sanity check
```

`venv/` is git-ignored (rebuild from `requirements.txt`, don't commit it).

## Credentials — Ivan's part, over `ssh minis`, never in chat or git

Two accounts, two developer apps, one interactive one-time authorization each. `settings.ini`
is git-ignored (mode 600); `settings.ini.example` is the committed template.

1. **Spotify**: https://developer.spotify.com/dashboard → Create app. Redirect URI:
   `https://127.0.0.1` (exact match, the OAuth flow below needs it). Read-only scopes are all
   this needs (`playlist-read-private`, `playlist-read-collaborative`, `user-library-read`) —
   the tool requests them itself.
2. **YouTube Music**: Google Cloud Console → a project → APIs & Services → Credentials →
   Create OAuth client ID → application type **"TVs and Limited Input devices"** (this matters —
   other types won't work with `ytmusicapi`'s device-code flow). No redirect URI needed for this
   type.
3. `cp settings.ini.example settings.ini && chmod 600 settings.ini`, then either:
   - **fill `client_id`/`client_secret` for both `[youtube]` and `[spotify]` directly in
     `settings.ini`**, then run `venv/bin/spotify_to_ytmusic setup` and pass `--file settings.ini`
     when the tool asks, choosing option 4 (both) — it will still walk through both interactive
     authorization steps (see below) using the values already in the file; **or**
   - just run `venv/bin/spotify_to_ytmusic setup`, choose (4), and paste the client id/secret
     values when prompted — same result, it writes the same file.
4. **YouTube's OAuth step is a device-code flow** (Google's "TV and limited input" flow) — the
   tool prints a URL and a short code; open the URL on *any* device already logged into the
   target Google account and enter the code. No browser needed on the server itself.
5. **Spotify's OAuth step is a redirect-and-paste flow** — the tool prints a URL; open it, log
   in, approve, and you'll land on `https://127.0.0.1/?code=...` (the page itself won't load,
   nothing is listening there — that's expected). Copy the full URL from the address bar and
   paste it back into the terminal when asked.

Both of these are interactive prompts on a real terminal — run `setup` directly over
`ssh minis`, not through anything that relays the prompts elsewhere. The `code=` value in that
Spotify redirect URL is short-lived and directly exchangeable for a token; treat it like a
credential, not something to paste into chat.

## Running it

```bash
venv/bin/spotify_to_ytmusic --file settings.ini create <spotify-playlist-url>   # one playlist
venv/bin/spotify_to_ytmusic --file settings.ini all <spotify-user-id>           # all public playlists
venv/bin/spotify_to_ytmusic --file settings.ini liked                          # Liked Songs (needs OAuth)
```

Unmatched tracks land in `noresults_youtube.txt` in the current directory per run — copy/rename
into `/srv/data/playlists/unmatched-YYYY-MM-DD.md` per the task, don't leave it as the tool's
default scratch file.

## What's git-ignored here

`venv/`, `settings.ini`, `*.cache` (spotipy's token cache), `noresults_youtube.txt`.
