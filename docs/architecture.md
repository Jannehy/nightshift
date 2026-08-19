# Architecture

Nightshift is a Flask application that orchestrates established tools
(spotDL, yt-dlp, beets) behind one web UI and one config file.

## Modules (`nightshift/`)

| Module | Responsibility |
|---|---|
| `config.py` | Central config: defaults → `config.yaml` → env overrides (`NIGHTSHIFT_<SECTION>_<KEY>`). Derived paths for all pipelines. |
| `app.py` | Flask app: access gate (setup → login → app), routes, SSE job streaming, config API. `/health` and `/api/version` stay open so a client can identify the server before it has a session. |
| `auth.py` | Users in `/config/users.json` (scrypt hashes), roles `admin`/`user`, session secret. |
| `spotify.py` | Spotify pipeline via spotDL: retry loop with block detection, noise filtering, post-processing. |
| `downloader.py` | SoundCloud/YouTube pipeline via yt-dlp: DRM tolerance, m3u8 per set folder. |
| `nightly.py` | Nightly job: spotDL sync per `.spotdl` file (timeout per playlist), beets tagging, lyrics, SC/YT registry re-sync, per-source stats. |
| `scheduler.py` | APScheduler cron trigger for the nightly job (no system cron needed). |
| `navidrome.py` | Optional: set downloaded playlists public or assign an owner via Navidrome's internal API. |
| `syncreg.py` | Sync state: registry file for SoundCloud/YouTube playlists plus a read-through view of spotDL's `.spotdl` files, so the sync page shows everything the nightly job actually keeps updated. |
| `search.py` | iTunes catalog search + track/album download endpoints. |
| `jobs.py` / `logs.py` | Serial worker queue (all downloads and nightly runs execute one at a time; `/api/queue` exposes positions) and live log files with finish/fail markers (UI restore after reload). The download log is per user; the nightly log is shared, because the nightly job belongs to the whole instance. |

## Data locations (Docker)

- `/config` – `config.yaml`, `users.json`, cookies, sync registry, spotdl
  sync files, logs, beets DB (`BEETSDIR`)
- `/music` – the user's library; never chown'd recursively

## Design notes

- **Serial execution**: every download and nightly run goes through one
  worker thread. This prevents interleaved logs, misattributed playlists,
  concurrent beets imports and parallel runs amplifying rate limits.
  Users see their queue position in the UI.

- **One download log per user** (`download-live-<user>.log`). Runs are serial,
  but a log outlives its run: with a single shared file the next person's
  download truncated the previous one's log, so everyone ended up watching a
  run that was not theirs — and `/download-log` reported it as "running" to
  all of them. The file name is a sanitised, lowercased user name; if
  sanitising had to replace anything, a short digest is appended so two
  different names can never land in the same file. Jobs without a user fall
  back to the legacy `download-live.log`.

- **Album downloads own their log**: the album run opens it once and passes a
  `SubLog` to each track, which appends but writes no start/finish markers.
  Otherwise every track truncated the file and the first finished track
  marked the whole album as done.

- **One yt-dlp** (pip) serves both Nightshift and spotDL – no version drift.
- **deno** is baked into the image; it must be on the PATH of the process
  running yt-dlp, or some YouTube downloads fail with
  "Requested format is not available".
- Spotify uses a configurable spotDL output template; SC/YT use a fixed
  `<folder>/<set name>/<index> - <title>` scheme because playlist metadata
  from those platforms is unreliable.
- The nightly Spotify sync runs with `--sync-without-deleting` by default
  (`nightly.keep_removed_tracks`). spotDL would otherwise delete local files
  once a track leaves the playlist — destructive for rotating playlists and
  for tracks shared between several synced playlists.
- Spotify playlist names are resolved via Spotify's public oEmbed endpoint
  before the download, so the m3u8 gets its final, collision-free name
  immediately. If the lookup fails and spotDL leaves its `{list-name}`
  placeholder unsubstituted, the playlist file is discarded rather than
  registered — the tracks themselves are kept.
- Playlist visibility set on the sync page is mirrored to Navidrome;
  owner changes are Nightshift-local, because reassigning an already
  imported playlist to a different Navidrome user is not reliable through
  the internal API.
- Backend logs and API errors are English; the UI is translated via
  `static/i18n/*.json` (`data-i18n` attributes).
