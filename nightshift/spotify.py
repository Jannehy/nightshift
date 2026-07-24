"""Spotify downloads via spotDL.

Keeps the full battle-tested behavior: retry loop with block detection,
noise filtering, beets/lyrics post-processing, m3u8 for playlists,
Navidrome integration and sync registration.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import time
from os.path import basename, dirname, getmtime, splitext
from pathlib import Path

from . import navidrome, syncreg
from .config import cfg
from .jobs import emit, jobs
from .logs import LiveLog, download_log_path

NOISE_PATTERNS = [
    "you might be blocked",
    "use a vpn",
    "other audio providers",
    "some youtube downloads require deno",
    "yt-dlp failed for",
    "trying next url",
    "error: yt-dlp download error",
    "audioprovidererror",
    "downloadererror",
    "traceback",
    "an error occurred",
    "requesterror",
    "albumerror",
    "songerror",
    "playlisterror",
    "in entry_point",
    "in get_simple_songs",
    "in get_metadata",
    "in from_url",
    "in album",
    "in get_album_info",
    "in build_request",
    "in wrapper",
    "in post",
    "in get",
    "raise ",
    "failed to complete request",
    "could not get",
    "keyerror",
    "valueerror",
    "typeerror",
]

BLOCK_KEYWORDS = ["sign in to confirm", "http error 429", "rate limit"]


def _spotdl_base_cmd() -> list[str]:
    cmd = ["spotdl"]
    return cmd


def _download_cmd(url: str) -> list[str]:
    cmd = _spotdl_base_cmd() + ["download", url]
    if cfg.downloads.youtube_cookie_file and os.path.exists(cfg.downloads.youtube_cookie_file):
        cmd += ["--cookie-file", cfg.downloads.youtube_cookie_file]
    cmd += [
        "--output", cfg.library.spotify_output_template,
        "--format", cfg.downloads.spotify_format,
        "--bitrate", cfg.downloads.spotify_bitrate,
        "--threads", str(cfg.downloads.spotify_threads),
    ]
    if "playlist" in url:
        cmd += ["--m3u", "{list-name}.m3u8"]
    return cmd


def find_new_tracks(since_timestamp: float) -> list[str]:
    """New files in the Spotify target folder since the given timestamp."""
    ext = f".{cfg.downloads.spotify_format}"
    new_files = []
    for p in Path(cfg.spotify_path).rglob(f"*{ext}"):
        try:
            if p.stat().st_mtime > since_timestamp:
                new_files.append(str(p))
        except FileNotFoundError:
            pass
    return new_files


def run_spotify_download(job_id: str, url: str,
                         owner_id: str | None = None, sync: bool = False,
                         requested_by: str | None = None,
                         sync_public: bool = True):
    """Main entry: Spotify download (URL or search query) via spotDL."""
    q = jobs[job_id]
    log = LiveLog(download_log_path())

    try:
        log.start(f"URL: {url}")
        emit(q, "status", message="Initializing spotDL …", progress=2)
        log.write("Initializing spotDL …")

        start_time = time.time() - 2
        # Snapshot existing playlist files: spotDL writes "{list-name}.m3u8"
        # and would silently overwrite another account's same-named playlist
        # before we get the chance to rename ours (see _handle_playlist).
        prev_m3us = {}
        for _m in glob.glob(f"{cfg.library.music_root}/*.m3u8"):
            try:
                with open(_m) as _f:
                    prev_m3us[basename(_m)] = _f.read()
            except OSError:
                pass
        max_attempts = int(cfg.downloads.max_attempts)
        wait_between = int(cfg.downloads.retry_wait_seconds)

        success = False
        total = 0
        done = 0
        failed_tracks: list[str] = []

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                msg = f"Attempt {attempt}/{max_attempts} – waiting {wait_between}s …"
                emit(q, "status", message=msg, progress=2)
                log.write(msg)
                time.sleep(wait_between)

            header = f"━━━ Attempt {attempt}/{max_attempts} ━━━"
            emit(q, "log", line=header)
            log.write(header)
            emit(q, "status",
                 message=f"spotDL running (attempt {attempt}/{max_attempts}) …",
                 progress=3)

            proc = subprocess.Popen(
                _download_cmd(url), cwd=cfg.library.music_root,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )

            attempt_done = 0
            had_block_error = False

            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue

                if any(kw in line.lower() for kw in BLOCK_KEYWORDS):
                    had_block_error = True

                m = re.search(r"Found (\d+) songs?", line)
                if m and total == 0:
                    total = int(m.group(1))
                    found_msg = f"{total} tracks found"
                    emit(q, "status", message=found_msg, total=total, progress=5)
                    log.write(found_msg)
                    continue

                if ("Downloaded" in line or "Skipping" in line
                        or ('"' in line and "found" in line.lower())):
                    attempt_done += 1
                    done = max(done, attempt_done)
                    m = re.search(r'"([^"]+)"', line)
                    track = m.group(1) if m else "..."
                    progress = int(5 + (done / total) * 80) if total else 50
                    emit(q, "progress",
                         current=done, total=total, track=track,
                         progress=progress, line=line)
                    log.write(line)
                    continue

                m_fail = re.search(r"failed to get metadata for: (.+?)$", line)
                if m_fail:
                    track_name = m_fail.group(1).strip()
                    if track_name not in failed_tracks:
                        failed_tracks.append(track_name)
                    continue

                if any(p in line.lower() for p in NOISE_PATTERNS):
                    continue
                if (line.strip().startswith(("│", "╭", "╰", "❱"))
                        or line.strip() == "│" * len(line.strip())):
                    continue

                emit(q, "log", line=line)
                log.write(line)

            proc.wait()
            if proc.returncode == 0:
                success = True
                break

            fail_msg = (f"⚠ Attempt {attempt} failed "
                        f"(exit {proc.returncode}, block={had_block_error})")
            emit(q, "log", line=fail_msg)
            log.write(fail_msg)

        if not success:
            err_msg = f"No success after {max_attempts} attempts"
            log.fail(err_msg)
            emit(q, "error", message=err_msg)
            return

        new_files = find_new_tracks(start_time)
        emit(q, "log", line=f"→ {len(new_files)} new files detected")
        log.write(f"→ {len(new_files)} new files detected")

        if new_files:
            _post_process(q, log, new_files)
            if "playlist" in url:
                _handle_playlist(q, log, url, owner_id, sync, start_time,
                                 requested_by, sync_public, prev_m3us)
        else:
            skip = "→ No new tracks – skipping beets & lyrics"
            emit(q, "log", line=skip)
            log.write(skip)

        if failed_tracks:
            emit(q, "log", line="")
            header = f"⚠ {len(failed_tracks)} track(s) could not be downloaded:"
            emit(q, "log", line=header)
            log.write(header)
            for t in failed_tracks:
                emit(q, "log", line=f"  • {t}")
                log.write(f"  • {t}")

        done_msg = f"Done! {done} tracks processed."
        if failed_tracks:
            done_msg += f" ({len(failed_tracks)} failed)"
        log.finish(done_msg)
        emit(q, "done", message=done_msg, progress=100, total_tracks=done)

    except Exception as e:
        log.fail(str(e))
        emit(q, "error", message=str(e))


def _post_process(q, log: LiveLog, new_files: list[str]):
    """Beets import, beet write and optional lyrics for new Spotify files."""
    if cfg.beets.enabled and shutil.which("beet"):
        new_dirs = sorted({dirname(f) for f in new_files})
        msg = f"Beets: importing {len(new_dirs)} album folder(s) …"
        emit(q, "status", message=msg, progress=88)
        log.write(msg)
        beet_cmd = ["beet"]
        if cfg.beets.config_file:
            beet_cmd += ["-c", cfg.beets.config_file]
        for d in new_dirs:
            subprocess.run(beet_cmd + ["import", "-A", d],
                           capture_output=True, text=True)
        emit(q, "status",
             message=f"Beets: writing tags ({len(new_files)} tracks) …",
             progress=93)
        subprocess.run(beet_cmd + ["write"] + new_files,
                       capture_output=True, text=True, timeout=300)

    if cfg.nightly.fetch_lyrics and shutil.which("sync-lyrics-backfill"):
        emit(q, "status",
             message=f"Fetching lyrics ({len(new_files)} tracks) …",
             progress=96)
        log.write(f"Fetching lyrics ({len(new_files)} tracks) …")
        subprocess.run(["sync-lyrics-backfill"] + new_files,
                       capture_output=True, text=True, timeout=1800)


def _ensure_playlist_directive(m3u_path: str, title: str):
    """Add #PLAYLIST:<title> so media servers show the original name."""
    try:
        with open(m3u_path) as f:
            lines = f.read().splitlines()
    except OSError:
        return
    if any(l.startswith("#PLAYLIST:") for l in lines):
        return
    if lines and lines[0].startswith("#EXTM3U"):
        lines.insert(1, f"#PLAYLIST:{title}")
    else:
        lines = ["#EXTM3U", f"#PLAYLIST:{title}"] + lines
    try:
        with open(m3u_path, "w") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def _handle_playlist(q, log: LiveLog, url: str,
                     owner_id: str | None, sync: bool, since: float,
                     requested_by: str | None = None,
                     sync_public: bool = True,
                     prev_m3us: dict | None = None):
    """Navidrome visibility + optional sync registration for playlists.

    Only considers m3u8 files written during THIS job (mtime > since),
    so playlists can never be attributed to the wrong download.
    """
    m3us = [m for m in glob.glob(f"{cfg.library.music_root}/*.m3u8")
            if getmtime(m) > since]
    if not m3us:
        return
    newest = max(m3us, key=getmtime)
    pl_title = splitext(basename(newest))[0]

    # Personalized playlists ("Discover Weekly") collide across accounts:
    # give this one a unique file name and keep the real title in #PLAYLIST.
    unique = syncreg.resolve_playlist_name(pl_title, url)
    if unique != pl_title:
        target = f"{cfg.library.music_root}/{unique}.m3u8"
        try:
            clobbered = basename(newest)
            os.replace(newest, target)
            # spotDL already overwrote the other account's file before we
            # could rename ours — restore its previous content.
            if prev_m3us and clobbered in prev_m3us:
                with open(f"{cfg.library.music_root}/{clobbered}", "w") as f:
                    f.write(prev_m3us[clobbered])
            newest = target
            msg = f"Playlist file renamed to avoid a name clash: {unique}.m3u8"
            emit(q, "log", line=msg)
            log.write(msg)
        except OSError:
            unique = pl_title
    _ensure_playlist_directive(newest, pl_title)

    emit(q, "status", message="Navidrome: applying playlist visibility …", progress=98)
    ok, m = navidrome.apply_playlist_settings(pl_title, owner_id)
    emit(q, "log", line=m)
    log.write(m)

    if sync:
        sync_dir = cfg.nightly.spotdl_sync_dir
        os.makedirs(sync_dir, exist_ok=True)
        safe = re.sub(r"[^\w\- ()]", "_", unique).strip()
        spotdl_file = f"{sync_dir}/{safe}.spotdl"
        cmd = _spotdl_base_cmd() + ["sync", url, "--save-file", spotdl_file]
        if cfg.downloads.youtube_cookie_file and os.path.exists(cfg.downloads.youtube_cookie_file):
            cmd += ["--cookie-file", cfg.downloads.youtube_cookie_file]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            syncreg.add(url, "spotify", pl_title,
                        owner=requested_by, public=sync_public,
                        folder=unique)
            sm = f"Sync enabled: created {safe}.spotdl"
        else:
            sm = "Could not create sync file"
        emit(q, "log", line=sm)
        log.write(sm)
