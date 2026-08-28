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
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import syncreg
from .config import cfg
from . import cookies, lyrics, notify, tagtidy
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

        _cookie_step(log, emit_fn)

        _log_line(log, emit_fn, "═══ Done ═══")
        _log_line(log, emit_fn, f"  Spotify playlists:    {playlists_synced}")
        _log_line(log, emit_fn, f"  SC/YT playlists:      {reg_synced}")
        _log_line(log, emit_fn, f"  Playlists failed:     {total_failed}")
        _log_line(log, emit_fn, f"  New tracks (Spotify): {new_tracks}")
        _log_line(log, emit_fn, f"  New tracks (SC/YT):   {reg_new}")

        ok = total_failed == 0
        # summarise() also runs inside finish()/fail(); calling it here with the
        # emitter is what puts the block in front of someone watching live.
        log.summarise(emit_fn)
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


# spotDL prints a framed Python traceback when a request fails. Twelve lines of
# box drawing say no more than the one line naming the exception, and in a log
# that is read at a glance they bury everything around them.
_FRAME = re.compile(r"^[│╭╰╯┌└─┃┏┗❱|]|^\s*\d+\s*│")
_EXCEPTION = re.compile(r"^[A-Za-z_.]*(Error|Exception)\b.*")


def _is_spotdl_noise(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if _FRAME.search(stripped):
        return True
    return any(stripped.startswith(n) for n in ("Processed ",))


def _failure_reason(lines) -> str:
    """The one line worth showing from a failed attempt.

    spotDL ends its traceback with the exception and its message - that is the
    sentence a person can act on. Everything above it is the frame it happened
    in, which matters to whoever debugs spotDL, not to whoever runs it.
    """
    for line in reversed(list(lines)):
        stripped = line.strip()
        if _EXCEPTION.match(stripped):
            return stripped[:160]
    for line in reversed(list(lines)):
        stripped = line.strip()
        if stripped:
            return stripped[:160]
    return ""


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
                    reason = _failure_reason(tail)
                    if reason:
                        _log_line(log, emit_fn, f"    Last output: {reason}")
                    raise subprocess.TimeoutExpired(
                        cmd, int(cfg.nightly.sync_timeout_seconds))
                reason = _failure_reason(tail)
                _log_line(log, emit_fn,
                          f"    ⚠ Attempt {attempt} failed (exit {rc})"
                          + (f": {reason}" if reason else ""))
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


def _cookie_step(log, emit_fn) -> None:
    """Checks whether the cookie files still sign in, and says so once.

    The expiry stamps in a cookie file are close to useless as a warning:
    a file whose login cookie claimed 294 days left was already being turned
    away after six, because Google revokes sessions long before the dates run
    out. So the check actually uses the cookies and asks the site. The dates
    stay as a secondary signal - some services really do age out on schedule.

    A push message goes out when the state changes, and again after a week if
    nothing was done. A nightly job that repeats the same warning every night
    teaches people to ignore it.
    """
    try:
        cookies.refresh()          # one request per site, once a night
        entries = cookies.all_status()
    except Exception:
        return

    pending = [e for e in entries
               if e["state"] in ("signed_out", "expired", "missing", "soon")]
    for entry in pending:
        if entry["state"] == "signed_out":
            text = f"{entry['kind']}: cookies are no longer signed in"
        elif entry["state"] == "expired":
            text = f"{entry['kind']}: cookies expired"
        elif entry["state"] == "missing":
            text = f"{entry['kind']}: cookie file not found"
        else:
            text = (f"{entry['kind']}: cookies expire in "
                    f"{entry['days_left']:.0f} day(s)")
        _log_line(log, emit_fn, f"  ⚠ {text}")

        if (getattr(cfg.notifications, "notify_cookies", True)
                and notify.enabled() and cookies.should_notify(entry)):
            if notify.send("Nightshift: cookies", text,
                           priority="high", tags="warning"):
                cookies.mark_notified(entry)

    # Say so when a replacement worked, otherwise the last word on the subject
    # stays a warning that is no longer true.
    for entry in entries:
        if cookies.recovered(entry):
            text = f"{entry['kind']}: cookies work again"
            _log_line(log, emit_fn, f"  {text}")
            if (getattr(cfg.notifications, "notify_cookies", True)
                    and notify.enabled()):
                notify.send("Nightshift: cookies", text, tags="white_check_mark")
            cookies.mark_notified(entry)


def _last_line(text: str) -> str:
    """The last line worth showing from a command's output."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line:
            return line[:200]
    return ""


def _modified_after(path: Path, stamp: float) -> bool:
    try:
        return path.stat().st_mtime > stamp
    except OSError:
        return False


def _known_to_beets(beet_cmd: list[str]) -> set[str] | None:
    """Every file path the beets library holds, or None if it cannot be read."""
    r = subprocess.run(beet_cmd + ["ls", "-f", "$path"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return {line.strip() for line in r.stdout.splitlines() if line.strip()}


def _beets_step(log, emit_fn, prev_ts: float, new_tracks: int) -> list[str]:
    _log_line(log, emit_fn, "→ Step 2: Tagging new tracks with Beets")
    if new_tracks <= 0:
        _log_line(log, emit_fn, "  No new tracks, skipping beets")
        return []
    if not (cfg.beets.enabled and shutil.which("beet")):
        _log_line(log, emit_fn, "  Beets disabled or not installed - skipped")
        return []

    beet_cmd = ["beet"]
    if cfg.beets.config_file:
        beet_cmd += ["-c", cfg.beets.config_file]

    exclude = (Path(cfg.soundcloud_path), Path(cfg.youtube_path))
    spotify_ext = f".{cfg.downloads.spotify_format}"
    on_disk = [p for p in Path(cfg.library.music_root).rglob(f"*{spotify_ext}")
               if not any(str(p).startswith(str(ex)) for ex in exclude)]

    # What beets has not seen is what needs importing. The modification time
    # used to decide this, which made every mass tag rewrite - a genre pass, a
    # beet write - look like a library full of new files and sent the whole
    # collection through the importer again. Beets knows exactly what it holds;
    # asking it costs one query and cannot drift.
    known = _known_to_beets(beet_cmd)
    if known is None:
        _log_line(log, emit_fn, "  Beets library unreadable - falling back to timestamps")
        new_files = [str(p) for p in on_disk
                     if _modified_after(p, prev_ts)]
    else:
        new_files = [str(p) for p in on_disk if str(p) not in known]

    _log_line(log, emit_fn, f"  Found {len(new_files)} new files for tagging")
    if not new_files:
        return []

    new_dirs = sorted({str(Path(f).parent) for f in new_files})
    _log_line(log, emit_fn, f"  Importing {len(new_dirs)} album folder(s)")
    for d in new_dirs:
        _log_line(log, emit_fn, f"    → {d}")
        r = subprocess.run(beet_cmd + ["import", "-A", d],
                           capture_output=True, text=True)
        if r.returncode != 0:
            # Say what beets said. "import error" on its own hid a duplicate
            # prompt for weeks: the run failed every night and the log never
            # named a reason anyone could act on.
            detail = _last_line(r.stderr) or _last_line(r.stdout)
            _log_line(log, emit_fn,
                      f"    ⚠ import error: {detail}" if detail
                      else "    ⚠ import error")

    _log_line(log, emit_fn, "  Running beet write...")
    r = subprocess.run(beet_cmd + ["write", "mb_trackid:^.+$"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        subprocess.run(beet_cmd + ["write"], capture_output=True, text=True)

    _spelling_step(log, emit_fn, beet_cmd)
    return new_files


def _spelling_step(log, emit_fn, beet_cmd: list[str]) -> None:
    """Puts the genres back into the spelling the whitelist itself uses.

    lastgenre writes last.fm's own tag with .title() applied, and the whitelist
    it checks against is lower-cased on load: it decides whether a genre is
    allowed, never how it is written. "edm" therefore arrives as "Edm" and
    "drum and bass" as "Drum And Bass", however the whitelist spells them.

    Running over the whole library rather than only the new files costs one
    query and repairs anything an earlier night wrote as well.
    """
    table = tagtidy.whitelist()
    if not table:
        return
    listing = subprocess.run(beet_cmd + ["ls", "-f", "$id\t$genres"],
                             capture_output=True, text=True)
    if listing.returncode != 0:
        return

    fixed = 0
    for line in listing.stdout.splitlines():
        item_id, _, genre = line.partition("\t")
        genre = genre.strip()
        if not genre or not item_id.strip().isdigit():
            continue
        wanted = table.get(tagtidy.simplify(genre))
        if not wanted or wanted == genre:
            continue
        subprocess.run(beet_cmd + ["modify", "-y", f"id:{item_id}",
                                   f"genres={wanted}"],
                       capture_output=True, text=True)
        subprocess.run(beet_cmd + ["write", f"id:{item_id}"],
                       capture_output=True, text=True)
        fixed += 1
    if fixed:
        _log_line(log, emit_fn, f"  Genre spelling corrected: {fixed} track(s)")


def _lyrics_step(log, emit_fn, new_files: list[str]):
    if not new_files or not cfg.nightly.fetch_lyrics:
        return
    if not lyrics.available():
        _log_line(log, emit_fn,
                  "→ Step 2.5: Lyrics skipped - syncedlyrics not installed")
        return
    _log_line(log, emit_fn, "→ Step 2.5: Fetching lyrics for new tracks")
    counts = lyrics.fetch(new_files)
    _log_line(log, emit_fn,
              f"  {counts['synced']} synced, {counts['plain']} plain, "
              f"{counts['missing']} without lyrics")


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
            tidied = tagtidy.tidy(tracks)
            if tidied:
                _log_line(log, emit_fn, f"    Tags tidied: {tidied} file(s)")
            if tracks:
                for m3u in write_m3u_for(tracks, display_name=e.get("name")):
                    _log_line(log, emit_fn,
                              f"    m3u8 updated: {Path(m3u).name}")

    reg_new = max(_count_registry_audio() - before, 0)
    _log_line(log, emit_fn,
              f"  Registry sync done ({synced} playlists, {reg_new} new tracks)")
    return synced, reg_new
