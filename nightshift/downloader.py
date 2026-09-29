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

from . import navidrome, syncreg, tagtidy
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


def track_homes(root: Path) -> dict[str, Path]:
    """Where each track actually lives, one entry per file name.

    A track that sits in several playlists is kept once - in the folder that
    fetched it first, which is the oldest file of that name.
    """
    homes: dict[str, Path] = {}
    for f in root.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in AUDIO_EXTS:
            continue
        seen = homes.get(f.name)
        if seen is None or f.stat().st_mtime < seen.stat().st_mtime:
            homes[f.name] = f
    return homes


def drop_redundant_copies(new_files: list[str],
                          root: Path) -> tuple[list[str], list[str]]:
    """Removes a fresh download that another playlist already holds.

    Playlists are downloaded into their own folder, so a track in two sets
    arrives twice. The second copy is deleted right away and the playlist
    points at the first one instead - the way the Spotify half has always
    worked, where one file is referenced by up to eleven playlists.

    Only files from this run are considered, and only when an older file of
    the same name exists elsewhere, so nothing that was already in place can
    be removed by this.

    Returns (files still there, names that were dropped). The names matter:
    the playlist still contains those tracks, and after the file is gone the
    folder can no longer say so.
    """
    homes = track_homes(root)
    kept, dropped = [], []
    for path in new_files:
        p = Path(path)
        home = homes.get(p.name)
        if home is not None and home != p:
            try:
                p.unlink()
                dropped.append(p.name)
                continue
            except OSError:
                pass
        kept.append(path)
    return kept, dropped


def write_m3u_for(new_files: list[str],
                  display_name: str | None = None,
                  root: Path | None = None) -> list[str]:
    """Convenience wrapper: the set folders are the parents of these files."""
    return write_m3u_for_dirs(sorted({Path(f).parent for f in new_files}),
                              display_name, root)


def write_m3u_for_dirs(dirs: list[Path],
                       display_name: str | None = None,
                       root: Path | None = None,
                       extra_members: list[str] | None = None) -> list[str]:
    """Writes one m3u8 per set folder listing everything in that playlist.

    An entry may point outside the folder. Membership therefore cannot be
    read off the folder any more - it is carried over from the previous
    m3u8 and extended by whatever is in the folder now. Each name is then
    resolved to wherever that file actually lives.

    display_name goes into the #PLAYLIST directive, so media servers show
    the original playlist title even when the folder carries a
    disambiguation suffix like "Your Mix 1 (2)".
    """
    created = []
    for d in dirs:
        base = root or d.parent
        homes = track_homes(base)
        m3u = d / f"{d.name}.m3u8"

        members: list[str] = []
        if m3u.exists():
            for line in m3u.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    members.append(Path(line).name)
        members += [f.name for f in d.iterdir()
                    if f.is_file() and f.suffix.lower() in AUDIO_EXTS]
        members += extra_members or []

        tracks = sorted({m for m in members if m in homes})
        if not tracks:
            continue
        with open(m3u, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            f.write(f"#PLAYLIST:{display_name or d.name}\n")
            for t in tracks:
                home = homes[t]
                f.write((t if home.parent == d
                         else os.path.relpath(home, d)) + "\n")
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


def _archive_args(target_dir: str) -> list[str]:
    """Remembers what has been fetched, by track id rather than by name.

    A file name can change without the track changing - a renamed set, a
    title edited by the uploader - and every such change used to buy a
    second copy. The archive fills itself on the first run: a track whose
    file is already there is recorded as downloaded rather than fetched.

    One archive per set, not one for everything. A set has to see its own
    members once to know it has them; a shared archive would skip a track
    another playlist already holds, and the new set would never learn that
    it contains it. The redundant copy is dropped right after the download
    instead - see drop_redundant_copies.
    """
    return ["--download-archive", str(Path(target_dir) / ".ytdlp-archive")]


def build_ytdlp_cmd(url: str, source: str, template: str,
                    cookie_args: list[str],
                    archive_dir: str | None = None) -> list[str]:
    fmt_args = ["-x"]
    if source == "YouTube":
        fmt_args = ["-f", "bestaudio/best", "-x",
                    "--audio-format", "mp3", "--audio-quality", "0"]
    return (["yt-dlp"] + fmt_args
            + ["--embed-thumbnail", "--embed-metadata"]
            + (_archive_args(archive_dir) if archive_dir else [])
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
            set_folder = syncreg.resolve_set_folder(title or "playlist", url,
                                                    base_dir, owner_id)
            # No playlist index in the name. It is a position, not an
            # identity: SoundCloud reorders its own mixes constantly, so a
            # track that moved got a new file name, the "already
            # downloaded" check found nothing and fetched it again. One set
            # held the same track five times. The m3u8 sorts by name either
            # way, so the number never carried an order to begin with.
            template = f"{base_dir}/{set_folder}/%(title)s.%(ext)s"
            msg = f"Playlist/set detected: {title} ({total} tracks)"
            if set_folder != (title or ""):
                msg += f" -> folder: {set_folder}"
        else:
            template = f"{base_dir}/%(artist,uploader)s/%(title)s.%(ext)s"
            msg = f"Track: {title}" if title else "Single track"
        emit(q, "status", message=msg, total=total, progress=5)
        log.write(msg)

        archive_dir = f"{base_dir}/{set_folder}" if set_folder else base_dir
        cmd = build_ytdlp_cmd(url, source, template, cookie_args, archive_dir)
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

        dropped: list[str] = []
        if new_files and is_set:
            new_files, dropped = drop_redundant_copies(new_files,
                                                       Path(base_dir))
        if new_files or dropped:
            if is_set:
                for m3u in write_m3u_for_dirs(
                        [Path(base_dir) / set_folder],
                        display_name=title, root=Path(base_dir),
                        extra_members=dropped):
                    emit(q, "log", line=f"Playlist created: {Path(m3u).name}")
                    log.write(f"Playlist created: {m3u}")

            # SoundCloud and YouTube deliver whatever the page says; the
            # Spotify path never comes through here and needs no repair.
            tidied = tagtidy.tidy(new_files)
            if tidied:
                tm = f"Tags tidied: {tidied} file(s)"
                emit(q, "log", line=tm)
                log.write(tm)

            _beets_import(q, log, new_files)

            if is_set and title:
                emit(q, "status",
                     message="Navidrome: applying playlist visibility ...",
                     progress=95)
                ok, m = navidrome.apply_playlist_settings(
                    title, owner_id,
                    path=f"{base_dir}/{set_folder}/{set_folder}.m3u8")
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
