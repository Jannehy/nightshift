"""Nightly sync job.

Steps:
  1. spotDL sync of all .spotdl files (retry loop, timeout per playlist)
  2. Beets tagging of new Spotify tracks (SC/YT folders excluded)
  2.5 Lyrics for new tracks (optional)
  2.7 SoundCloud/YouTube sync registry: re-download URLs (DRM-tolerant),
      rewrite m3u8 per set folder
  3. Save timestamp, per-source statistics (Spotify vs. SC/YT)
"""
from __future__ import annotations

import json
import os
from collections import deque
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import syncreg
from .config import cfg
from .downloader import AUDIO_EXTS, build_ytdlp_cmd, write_m3u_for
from .spotify import _ensure_playlist_directive, looks_unresolved
from .logs import LiveLog, nightly_log_path

_lock = threading.Lock()
_running = False


def is_running() -> bool:
    return _running


def _timestamp_file() -> Path:
    return Path(cfg.sync.registry_file).parent / "nightly-last-run"


def _log_line(log: LiveLog, emit_fn, line: str):
    stamped = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}"
    log.write(stamped)
    if emit_fn:
        emit_fn(stamped)


def _count_registry_audio() -> int:
    n = 0
    for base in (cfg.soundcloud_path, cfg.youtube_path):
        p = Path(base)
        if p.exists():
            n += sum(1 for f in p.rglob("*") if f.suffix.lower() in AUDIO_EXTS)
    return n


def run_nightly(emit_fn=None) -> bool:
    """Full nightly run. emit_fn(line) is optional for live streaming.

    Returns True on success (including tolerated partial failures).
    """
    global _running
    if not _lock.acquire(blocking=False):
        return False
    _running = True
    log = LiveLog(nightly_log_path())
    try:
        log.start("Nightly Sync")
        _log_line(log, emit_fn, "═══ Nightly music sync starting ═══")

        ts_file = _timestamp_file()
        prev_ts = time.time() - 24 * 3600
        if ts_file.exists():
            try:
                prev_ts = float(ts_file.read_text().strip())
            except ValueError:
                pass
        _log_line(log, emit_fn,
                  f"Looking for files newer than "
                  f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(prev_ts))}")

        # --- Step 1: spotDL Sync ---
        _log_line(log, emit_fn, "→ Step 1: spotDL Sync")
        playlists_synced, total_failed, new_tracks = _spotdl_sync_step(log, emit_fn)

        # --- Steps 2 + 2.5: beets + lyrics for new Spotify tracks ---
        new_files = _beets_step(log, emit_fn, prev_ts, new_tracks)
        _lyrics_step(log, emit_fn, new_files)

        # --- Step 2.7: SoundCloud/YouTube sync registry ---
        reg_synced, reg_new = _registry_sync_step(log, emit_fn)

        # --- Step 3: timestamp + statistics ---
        ts_file.parent.mkdir(parents=True, exist_ok=True)
        ts_file.write_text(str(time.time()))

        _log_line(log, emit_fn, "═══ Done ═══")
        _log_line(log, emit_fn, f"  Spotify playlists:    {playlists_synced}")
        _log_line(log, emit_fn, f"  SC/YT playlists:      {reg_synced}")
        _log_line(log, emit_fn, f"  Playlists failed:     {total_failed}")
        _log_line(log, emit_fn, f"  New tracks (Spotify): {new_tracks}")
        _log_line(log, emit_fn, f"  New tracks (SC/YT):   {reg_new}")

        ok = total_failed == 0
        if ok:
            log.finish("Nightly sync completed")
        else:
            log.fail(f"{total_failed} playlist(s) failed")
        return ok
    except Exception as e:
        log.fail(str(e))
        return False
    finally:
        _running = False
        _lock.release()


# Lines that carry no diagnostic value (progress spam, banners)
_NOISE = ("Processed", "Skipping", "AudioProviderError:",
          "Downloading:", "\rDownloaded")


def _is_spotdl_noise(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    return any(stripped.startswith(n) for n in ("Processed ",))


def _spotdl_sync_step(log, emit_fn) -> tuple[int, int, int]:
    sync_dir = Path(cfg.nightly.spotdl_sync_dir)
    synced, failed, new_total = 0, 0, 0
    if not sync_dir.is_dir():
        _log_line(log, emit_fn, "  No sync directory - skipped")
        return synced, failed, new_total

    music_root = cfg.library.music_root
    spotify_ext = f".{cfg.downloads.spotify_format}"

    def count_spotify() -> int:
        p = Path(cfg.spotify_path)
        return sum(1 for _ in p.rglob(f"*{spotify_ext}")) if p.exists() else 0

    for sync_file in sorted(sync_dir.glob("*.spotdl")):
        name = sync_file.stem
        if looks_unresolved(name):
            # Leftover from a download where the playlist name could not be
            # resolved — syncing it would recreate a bogus playlist nightly.
            _log_line(log, emit_fn,
                      f"  Skipping {sync_file.name}: unresolved playlist name")
            continue
        _log_line(log, emit_fn, f"  Syncing: {name}")
        before = count_spotify()
        success = False

        for attempt in range(1, int(cfg.nightly.max_attempts) + 1):
            _log_line(log, emit_fn,
                      f"    Attempt {attempt}/{cfg.nightly.max_attempts}")
            cmd = ["spotdl", "sync", str(sync_file),
                   "--output", cfg.library.spotify_output_template,
                   "--format", cfg.downloads.spotify_format,
                   "--bitrate", cfg.downloads.spotify_bitrate,
                   "--threads", str(cfg.downloads.spotify_threads),
                   "--save-errors", "errors.txt",
                   "--m3u", f"{name}.m3u8"]
            if cfg.nightly.keep_removed_tracks:
                cmd.append("--sync-without-deleting")
            cookie = cfg.downloads.youtube_cookie_file
            if cookie and os.path.exists(cookie):
                cmd += ["--cookie-file", cookie]
            try:
                # Stream spotDL output live: on a timeout the captured
                # output would be lost, leaving no clue what went wrong.
                proc = subprocess.Popen(
                    cmd, cwd=music_root,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                    start_new_session=True,   # own process group
                )

                def _kill_group(p=proc):
                    # Kill children too (ffmpeg keeps the pipe open and
                    # would stall the reader long past the timeout).
                    try:
                        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                    except Exception:
                        p.kill()

                killer = threading.Timer(
                    int(cfg.nightly.sync_timeout_seconds), _kill_group)
                killer.start()
                # Collect output quietly; it is shown only on failure so
                # the normal nightly log stays short and readable.
                tail = deque(maxlen=15)
                try:
                    for line in proc.stdout:
                        line = line.rstrip()
                        if line and not _is_spotdl_noise(line):
                            tail.append(line)
                    rc = proc.wait()
                finally:
                    timed_out = not killer.is_alive()
                    killer.cancel()
                if rc == 0:
                    success = True
                    break
                if timed_out or rc in (-9, 137):
                    _log_line(log, emit_fn, "    Last output before the timeout:")
                    for t in list(tail)[-8:]:
                        _log_line(log, emit_fn, f"      | {t}")
                    raise subprocess.TimeoutExpired(
                        cmd, int(cfg.nightly.sync_timeout_seconds))
                _log_line(log, emit_fn, f"    ⚠ Attempt {attempt} failed (exit {rc})")
                for t in list(tail)[-8:]:
                    _log_line(log, emit_fn, f"      | {t}")
            except subprocess.TimeoutExpired:
                _log_line(log, emit_fn,
                          f"    ⚠ Attempt {attempt} timeout "
                          f"({cfg.nightly.sync_timeout_seconds}s) - aborted")
            if attempt < int(cfg.nightly.max_attempts):
                wait = attempt * 10
                _log_line(log, emit_fn, f"    Waiting {wait}s before retry...")
                time.sleep(wait)

        if success:
            synced += 1
            new = count_spotify() - before
            new_total += max(new, 0)
            _log_line(log, emit_fn, f"  ✓ {name}: {max(new, 0)} new tracks")
            # spotDL rewrote the m3u8 without the #PLAYLIST directive:
            # re-add it so media servers keep showing the original title.
            m3u_path = str(Path(music_root) / f"{name}.m3u8")
            if os.path.exists(m3u_path):
                display = name
                for e in syncreg.all_entries():
                    if e.get("source") == "spotify" and (
                            e.get("folder") == name
                            or (not e.get("folder") and e.get("name") == name)):
                        display = e.get("name") or name
                        break
                _ensure_playlist_directive(m3u_path, display)
        else:
            failed += 1
            _log_line(log, emit_fn, f"  ✗ {name} sync failed")

    _log_line(log, emit_fn, f"  Total new tracks: {new_total}")
    return synced, failed, new_total


def _beets_step(log, emit_fn, prev_ts: float, new_tracks: int) -> list[str]:
    _log_line(log, emit_fn, "→ Step 2: Tagging new tracks with Beets")
    if new_tracks <= 0:
        _log_line(log, emit_fn, "  No new tracks, skipping beets")
        return []
    if not (cfg.beets.enabled and shutil.which("beet")):
        _log_line(log, emit_fn, "  Beets disabled or not installed - skipped")
        return []

    exclude = (Path(cfg.soundcloud_path), Path(cfg.youtube_path))
    spotify_ext = f".{cfg.downloads.spotify_format}"
    new_files = []
    for p in Path(cfg.library.music_root).rglob(f"*{spotify_ext}"):
        if any(str(p).startswith(str(ex)) for ex in exclude):
            continue
        try:
            if p.stat().st_mtime > prev_ts:
                new_files.append(str(p))
        except FileNotFoundError:
            pass

    _log_line(log, emit_fn, f"  Found {len(new_files)} new files for tagging")
    if not new_files:
        return []

    beet_cmd = ["beet"]
    if cfg.beets.config_file:
        beet_cmd += ["-c", cfg.beets.config_file]

    new_dirs = sorted({str(Path(f).parent) for f in new_files})
    _log_line(log, emit_fn, f"  Importing {len(new_dirs)} album folder(s)")
    for d in new_dirs:
        _log_line(log, emit_fn, f"    → {d}")
        r = subprocess.run(beet_cmd + ["import", "-A", d],
                           capture_output=True, text=True)
        if r.returncode != 0:
            _log_line(log, emit_fn, "    ⚠ import error")

    _log_line(log, emit_fn, "  Running beet write...")
    r = subprocess.run(beet_cmd + ["write", "mb_trackid:^.+$"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        subprocess.run(beet_cmd + ["write"], capture_output=True, text=True)
    return new_files


def _lyrics_step(log, emit_fn, new_files: list[str]):
    if not new_files:
        return
    if not (cfg.nightly.fetch_lyrics and shutil.which("sync-lyrics-backfill")):
        return
    _log_line(log, emit_fn, "→ Step 2.5: Fetching lyrics for new tracks")
    r = subprocess.run(["sync-lyrics-backfill"] + new_files,
                       capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        _log_line(log, emit_fn, "  ⚠ Lyrics fetch had errors")


def _registry_sync_step(log, emit_fn) -> tuple[int, int]:
    _log_line(log, emit_fn, "→ Step 2.7: SoundCloud/YouTube sync registry")
    entries = [e for e in syncreg.all_entries()
               if e.get("source") in ("soundcloud", "youtube")]
    if not entries:
        _log_line(log, emit_fn, "  No sync registry entries")
        return 0, 0

    before = _count_registry_audio()
    synced = 0
    for e in entries:
        url, source_key = e["url"], e["source"]
        _log_line(log, emit_fn, f"  Sync ({source_key}): {url}")
        base = cfg.soundcloud_path if source_key == "soundcloud" else cfg.youtube_path
        source = "SoundCloud" if source_key == "soundcloud" else "YouTube"
        # Legacy entries have no pinned folder: use the existing directory if
        # it matches the name, otherwise let yt-dlp decide as before.
        folder = e.get("folder")
        if not folder:
            candidate = syncreg._sanitize_folder(e.get("name") or "")
            folder = candidate if (Path(base) / candidate).is_dir() else None
        folder = folder or "%(playlist_title)s"
        template = f"{base}/{folder}/%(playlist_index)02d - %(title)s.%(ext)s"
        cookie_args = []
        cookie = (cfg.downloads.soundcloud_cookie_file if source_key == "soundcloud"
                  else cfg.downloads.youtube_cookie_file)
        if cookie and os.path.exists(cookie):
            cookie_args = ["--cookies", cookie]
        cmd = build_ytdlp_cmd(url, source, template, cookie_args)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=int(cfg.nightly.sync_timeout_seconds))
            if r.returncode != 0:
                # DRM tracks etc. are expected – partial success, not an error
                _log_line(log, emit_fn,
                          "    ℹ Sync ok, some tracks skipped (e.g. DRM)")
        except subprocess.TimeoutExpired:
            _log_line(log, emit_fn, "    ⚠ Timeout - will continue next night")
        synced += 1

        # rewrite the set folder's m3u8
        set_dir = Path(base) / (e.get("folder") or e.get("name") or "")
        if set_dir.is_dir():
            tracks = [str(f) for f in set_dir.iterdir()
                      if f.suffix.lower() in AUDIO_EXTS]
            if tracks:
                for m3u in write_m3u_for(tracks, display_name=e.get("name")):
                    _log_line(log, emit_fn,
                              f"    m3u8 updated: {Path(m3u).name}")

    reg_new = max(_count_registry_audio() - before, 0)
    _log_line(log, emit_fn,
              f"  Registry sync done ({synced} playlists, {reg_new} new tracks)")
    return synced, reg_new
