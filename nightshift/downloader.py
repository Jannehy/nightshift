"""SoundCloud and YouTube downloads via yt-dlp.

Battle-tested behavior baked in:
- DRM tolerance: a non-zero exit with files present counts as partial success
- "already downloaded" lines count as processed files (re-sync case)
- m3u8 creation per set folder
- optional Navidrome post-processing and sync registration
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import navidrome, syncreg
from .config import cfg
from .jobs import emit, jobs
from .logs import LiveLog, download_log_path

AUDIO_EXTS = (".m4a", ".mp3", ".opus", ".ogg")

NOISE_PREFIXES = (
    "WARNING:",
    "[generic]", "[redirect]", "[soundcloud]", "[youtube",
    "[info]", "[hlsnative]", "[FixupM4a]", "[ExtractAudio]",
    "[Metadata]", "[download] Downloading item",
    "Deleting original file",
)


def _source_for(url: str) -> tuple[str, str]:
    """(target directory, source name) for a URL."""
    if "soundcloud.com" in url:
        return cfg.soundcloud_path, "SoundCloud"
    return cfg.youtube_path, "YouTube"


def _cookie_args(source: str) -> list[str]:
    if source == "YouTube":
        cookie = cfg.downloads.youtube_cookie_file
    else:
        cookie = cfg.downloads.soundcloud_cookie_file
    if cookie and os.path.exists(cookie):
        return ["--cookies", cookie]
    return []


def _find_new_audio(root: str, since: float) -> list[str]:
    out = []
    p = Path(root)
    if not p.exists():
        return out
    for f in p.rglob("*"):
        if f.suffix.lower() in AUDIO_EXTS:
            try:
                if f.stat().st_mtime > since:
                    out.append(str(f))
            except FileNotFoundError:
                pass
    return out


def write_m3u_for(new_files: list[str],
                  display_name: str | None = None) -> list[str]:
    """Writes one m3u8 per set folder containing all of the folder's tracks.

    display_name goes into the #PLAYLIST directive, so media servers show
    the original playlist title even when the folder carries a
    disambiguation suffix like "Your Mix 1 (2)".
    """
    dirs = sorted({Path(f).parent for f in new_files})
    created = []
    for d in dirs:
        tracks = sorted(
            f.name for f in d.iterdir()
            if f.suffix.lower() in AUDIO_EXTS
        )
        if not tracks:
            continue
        m3u = d / f"{d.name}.m3u8"
        with open(m3u, "w") as f:
            f.write("#EXTM3U\n")
            f.write(f"#PLAYLIST:{display_name or d.name}\n")
            for t in tracks:
                f.write(t + "\n")
        created.append(str(m3u))
    return created


def probe_url(url: str, cookie_args: list[str]) -> tuple[bool, str, int]:
    """Determine (is_set, title, total) via a yt-dlp probe."""
    is_set, title, total = False, "", 1
    probe = subprocess.run(
        ["yt-dlp", "--flat-playlist", "-J", "--no-warnings"]
        + cookie_args + [url],
        capture_output=True, text=True, timeout=120,
    )
    if probe.returncode == 0:
        try:
            info = json.loads(probe.stdout)
            title = info.get("title") or ""
            if info.get("_type") == "playlist":
                is_set = True
                total = len(info.get("entries") or []) or 1
        except Exception:
            pass
    return is_set, title, total


def build_ytdlp_cmd(url: str, source: str, template: str,
                    cookie_args: list[str]) -> list[str]:
    fmt_args = ["-x"]
    if source == "YouTube":
        fmt_args = ["-f", "bestaudio/best", "-x",
                    "--audio-format", "mp3", "--audio-quality", "0"]
    return (["yt-dlp"] + fmt_args
            + ["--embed-thumbnail", "--embed-metadata"]
            + cookie_args + ["-o", template, url])


def run_ytdlp_download(job_id: str, url: str,
                       owner_id: str | None = None, sync: bool = False,
                       requested_by: str | None = None,
                       sync_public: bool = True):
    """Main entry: SoundCloud/YouTube download streaming live into the job."""
    q = jobs[job_id]
    log = LiveLog(download_log_path(requested_by))
    try:
        base_dir, source = _source_for(url)
        log.start(f"{source}-URL: {url}")

        emit(q, "status", message=f"Checking {source} URL ...", progress=2)
        start_time = time.time() - 2
        cookie_args = _cookie_args(source)

        is_set, title, total = probe_url(url, cookie_args)

        set_folder = None
        if is_set:
            set_folder = syncreg.resolve_set_folder(title or "playlist", url)
            template = f"{base_dir}/{set_folder}/%(playlist_index)02d - %(title)s.%(ext)s"
            msg = f"Playlist/set detected: {title} ({total} tracks)"
            if set_folder != (title or ""):
                msg += f" -> folder: {set_folder}"
        else:
            template = f"{base_dir}/%(artist,uploader)s/%(title)s.%(ext)s"
            msg = f"Track: {title}" if title else "Single track"
        emit(q, "status", message=msg, total=total, progress=5)
        log.write(msg)

        cmd = build_ytdlp_cmd(url, source, template, cookie_args)
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )

        done = 0
        seen_files: list[str] = []
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            if "[download]" in line and "% of" in line:
                continue
            if "has already been downloaded" in line:
                fpath = line.split("] ", 1)[-1]
                fpath = fpath.replace(" has already been downloaded", "").strip()
                if fpath:
                    seen_files.append(fpath)
                continue
            if line.startswith(NOISE_PREFIXES):
                continue
            if line.startswith("[download] Destination:"):
                track = Path(line.split("Destination:", 1)[1].strip()).stem
                emit(q, "log", line=f"\u266a Downloading: {track}")
                log.write(f"Downloading: {track}")
                continue
            if line.startswith("[EmbedThumbnail]"):
                done += 1
                m = re.search(r'"([^"]+)"', line)
                track = Path(m.group(1)).stem if m else ""
                if m:
                    seen_files.append(m.group(1))
                progress = int(5 + (done / total) * 80) if total else 50
                emit(q, "progress", current=done, total=total,
                     track=track, progress=progress,
                     line=f"\u2713 {track}")
                log.write(f"\u2713 {track}")
                continue
            emit(q, "log", line=line)
            log.write(line)

        proc.wait()
        new_files = _find_new_audio(base_dir, start_time)
        seen_ok = [f for f in seen_files if Path(f).exists()]
        new_files = sorted(set(new_files) | set(seen_ok))

        if proc.returncode != 0 and not new_files:
            err = f"yt-dlp failed (exit {proc.returncode})"
            log.fail(err)
            emit(q, "error", message=err)
            return
        if proc.returncode != 0:
            warn = ("\u26a0 Some tracks skipped (e.g. DRM-protected) "
                    "- processing the rest")
            emit(q, "log", line=warn)
            log.write(warn)

        emit(q, "log", line=f"-> {len(new_files)} new files")
        log.write(f"-> {len(new_files)} new files")

        if new_files:
            if is_set:
                for m3u in write_m3u_for(new_files, display_name=title):
                    emit(q, "log", line=f"Playlist created: {Path(m3u).name}")
                    log.write(f"Playlist created: {m3u}")

            _beets_import(q, log, new_files)

            if is_set and title:
                emit(q, "status",
                     message="Navidrome: applying playlist visibility ...",
                     progress=95)
                ok, m = navidrome.apply_playlist_settings(title, owner_id)
                emit(q, "log", line=m)
                log.write(m)

                if sync:
                    src = "soundcloud" if "soundcloud.com" in url else "youtube"
                    if syncreg.add(url, src, title,
                                   owner=requested_by, public=sync_public,
                                   folder=set_folder):
                        sm = f"Sync enabled: '{title}' will be updated nightly"
                    else:
                        sm = f"Sync was already enabled for '{title}'"
                    emit(q, "log", line=sm)
                    log.write(sm)

        n = done or len(new_files)
        done_msg = f"Done! {n} {source} tracks processed."
        log.finish(done_msg)
        emit(q, "done", message=done_msg, progress=100, total_tracks=n)

    except Exception as e:
        log.fail(str(e))
        emit(q, "error", message=str(e))


def _beets_import(q, log: LiveLog, new_files: list[str]):
    """Beets import without autotagging (only when enabled and available)."""
    import shutil
    if not (cfg.beets.enabled and shutil.which("beet")):
        return
    new_dirs = sorted({str(Path(f).parent) for f in new_files})
    msg = f"Beets: importing {len(new_dirs)} folder(s) (no autotagging) ..."
    emit(q, "status", message=msg, progress=90)
    log.write(msg)
    beet_cmd = ["beet"]
    if cfg.beets.config_file:
        beet_cmd += ["-c", cfg.beets.config_file]
    for d in new_dirs:
        subprocess.run(beet_cmd + ["import", "-A", d],
                       capture_output=True, text=True)
