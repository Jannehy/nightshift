# Architecture

Nightshift is a Flask application that orchestrates established tools
(spotDL, yt-dlp, beets) behind one web UI and one config file.

## Modules (`nightshift/`)

| Module | Responsibility |
|---|---|
| `config.py` | Central config: defaults → `config.yaml` → env overrides (`NIGHTSHIFT_<SECTION>_<KEY>`). Derived paths for all pipelines. |
| `app.py` | Flask app: access gate (setup → login → app), routes, SSE job streaming, config API. |
| `auth.py` | Users in `/config/users.json` (scrypt hashes), roles `admin`/`user`, session secret. |
| `spotify.py` | Spotify pipeline via spotDL: retry loop with block detection, noise filtering, post-processing. |
| `downloader.py` | SoundCloud/YouTube pipeline via yt-dlp: DRM tolerance, m3u8 per set folder. |
| `nightly.py` | Nightly job: spotDL sync per `.spotdl` file (timeout per playlist), beets tagging, lyrics, SC/YT registry re-sync, per-source stats. |
| `scheduler.py` | APScheduler cron trigger for the nightly job (no system cron needed). |
| `navidrome.py` | Optional: set downloaded playlists public or assign an owner via Navidrome's internal API. |
| `syncreg.py` | Sync state: registry file for SoundCloud/YouTube playlists plus a read-through view of spotDL's `.spotdl` files, so the sync page shows everything the nightly job actually keeps updated. |
| `search.py` | iTunes catalog search + track/album download endpoints. |
| `jobs.py` / `logs.py` | Serial worker queue (all downloads and nightly runs execute one at a time; `/api/queue` exposes positions) and live log files with finish/fail markers (UI restore after reload). |

## Data locations (Docker)

- `/config` – `config.yaml`, `users.json`, cookies, sync registry, spotdl
  sync files, logs, beets DB (`BEETSDIR`)
- `/music` – the user's library; never chown'd recursively

## Design notes

- **Serial execution**: every download and nightly run goes through one
  worker thread. This prevents interleaved logs, misattributed playlists,
  concurrent beets imports and parallel runs amplifying rate limits.
  Users see their queue position in the UI.

- **One yt-dlp** (pip) serves both Nightshift and spotDL – no version drift.
- **deno** is baked into the image; it must be on the PATH of the process
  running yt-dlp, or some YouTube downloads fail with
  "Requested format is not available".
- Spotify uses a configurable spotDL output template; SC/YT use a fixed
  `<folder>/<set name>/<index> - <title>` scheme because playlist metadata
  from those platforms is unreliable.
- Backend logs and API errors are English; the UI is translated via
  `static/i18n/*.json` (`data-i18n` attributes).
