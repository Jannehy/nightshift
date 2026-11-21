"""Spotify downloads via spotDL.

Keeps the full battle-tested behavior: retry loop with block detection,
noise filtering, beets/lyrics post-processing, m3u8 for playlists,
Navidrome integration and sync registration.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from os.path import basename, dirname, getmtime, splitext
from pathlib import Path

from . import lyrics, navidrome, notify, syncreg
from mutagen.id3 import ID3, TPOS

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

# How spotDL reports a track whose audio it could not fetch. It hard-wraps at
# eighty columns, so the message arrives in two pieces:
#
#     AudioProviderError: YT-DLP download error -
#     https://music.youtube.com/watch?v=9qnqYL0eNNI
#
# Both used to disappear - the first into NOISE_PATTERNS, the second because a
# bare URL looks like nothing worth printing. On 15.11.2026 a run in which all
# 55 remaining tracks failed this way therefore read as a quiet list of links
# and finished with "Done! 7 tracks processed", those seven being duplicates
# it had skipped. Whatever else is noise, this line is the outcome.
PROVIDER_FAILURE = ("audioprovidererror", "yt-dlp download error")


def playlist_title(url: str) -> str | None:
    """The playlist's real title, via Spotify's public oEmbed endpoint.

    spotDL's {list-name} placeholder is not always resolved (it stays
    literal for some link types), which used to produce playlists named
    "{list-name}". Fetching the title ourselves makes the file name
    deterministic. Returns None if the lookup fails — callers fall back
    to the placeholder.
    """
    try:
        api = ("https://open.spotify.com/oembed?url="
               + urllib.parse.quote(url, safe=""))
        with urllib.request.urlopen(api, timeout=10) as r:
            data = json.loads(r.read().decode())
        title = (data.get("title") or "").strip()
        return title or None
    except Exception:
        return None


def visible_name(title: str, url: str = "") -> str:
    """A playlist name a media server will actually see.

    A name beginning with a dot makes the file a hidden one on Unix, and every
    media server skips those: the tracks arrive, the playlist never appears and
    nothing in the log says why. Spotify allows such names - a playlist called
    "." is what brought this to light - so the leading dots come off here, and
    a name that is nothing but dots falls back to the playlist's own id.
    """
    cleaned = (title or "").strip().lstrip(".").strip()
    if cleaned:
        return cleaned
    ident = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    return f"Playlist {ident}" if ident else "Playlist"


def looks_unresolved(name: str) -> bool:
    """True for names that are an unsubstituted template placeholder."""
    return ("{" in name or "}" in name
            or name.strip("_ ").lower() in ("list-name", "list name"))


# YouTube ties the media URLs of some clients to a session PO token. Since
# 2026.10 yt-dlp asks the token-bound "tv_samsung" client first, and its URLs
# are the ones that come back as HTTP 403 for a random part of the tracks.
# Measured on 16.11.2026 against the same eight tracks: 20 of 24 downloads
# with the default client list, 16 of 16 with this one client excluded.
# An unknown name only produces a warning ("Skipping unsupported client"),
# so this stays harmless once the client disappears upstream.
YTDLP_ARGS = [
    "--yt-dlp-args=--extractor-args youtube:player_client=default,-tv_samsung",
]


def _spotdl_base_cmd() -> list[str]:
    cmd = ["spotdl"]
    return cmd


def _download_cmd(url: str, m3u_name: str | None = None,
                  use_cookies: bool = True) -> list[str]:
    cmd = _spotdl_base_cmd() + ["download", url]
    if (use_cookies and cfg.downloads.youtube_cookie_file
            and os.path.exists(cfg.downloads.youtube_cookie_file)):
        cmd += ["--cookie-file", cfg.downloads.youtube_cookie_file]
    cmd += [
        "--output", cfg.library.spotify_output_template,
        "--format", cfg.downloads.spotify_format,
        "--bitrate", cfg.downloads.spotify_bitrate,
        "--threads", str(cfg.downloads.spotify_threads),
    ] + YTDLP_ARGS
    if m3u_name:
        cmd += ["--m3u", f"{m3u_name}.m3u8"]
    elif "playlist" in url:
        # Fallback when the title lookup failed; spotDL substitutes this
        # itself — but not reliably, hence the check in _handle_playlist.
        cmd += ["--m3u", "{list-name}.m3u8"]
    return cmd


def _cookie_file_in_use() -> bool:
    """Whether a YouTube cookie file is configured and actually there."""
    configured = cfg.downloads.youtube_cookie_file
    return bool(configured and os.path.exists(configured))


def _spotify_client():
    """The one spotDL Spotify client, created on first use.

    spotDL's client is a singleton: a second init raises SpotifyError. Two
    places in here need it, so whoever got there first used to leave the
    other one with an exception and an empty result.
    """
    from spotdl.utils.config import DEFAULT_CONFIG
    from spotdl.utils.spotify import SpotifyClient
    try:
        return SpotifyClient()
    except Exception:
        SpotifyClient.init(client_id=DEFAULT_CONFIG["client_id"],
                           client_secret=DEFAULT_CONFIG["client_secret"],
                           user_auth=False, cache_path=None, no_cache=True)
        return SpotifyClient()


def expected_tracks(url: str) -> list[tuple[str, str]]:
    """(track id, "Artist - Title") for every track Spotify lists at `url`.

    Only asked when a run came back short. Naming what is absent needs a
    reference list, and the downloader's own output only ever mentions what
    it managed to fetch.
    """
    try:
        spotify = _spotify_client()
    except Exception:
        return []

    def named(item):
        artists = item.get("artists") or []
        who = artists[0].get("name", "?") if artists else "?"
        return item.get("id") or "", f"{who} - {item.get('name', '?')}"

    out = []
    try:
        if "/album/" in url:
            album_id = url.split("/album/")[-1].split("?")[0]
            offset = 0
            while True:
                page = spotify.album_tracks(album_id, limit=50, offset=offset)
                out += [named(i) for i in page.get("items", [])]
                if not page.get("next"):
                    break
                offset += 50
        elif "/track/" in url:
            out.append(named(spotify.track(url)))
    except Exception:
        return []
    return [(i, n) for i, n in out if i]


def _track_ids_on_disk(folders: set) -> set:
    """Spotify track ids of the files lying in these folders.

    spotDL writes the track's Spotify URL into the WOAS frame, which makes a
    file recognisable without trusting its name. The name is the one thing
    not to trust here: spotDL hard-wraps its console output at eighty
    columns, so a long "Downloaded ..." line arrives in pieces.
    """
    ids = set()
    for folder in folders:
        try:
            entries = list(Path(folder).iterdir())
        except OSError:
            continue
        for path in entries:
            if not path.is_file():
                continue
            try:
                source = ID3(path).get("WOAS")
            except Exception:
                continue
            link = str(source.url) if source is not None else ""
            if "/track/" in link:
                ids.add(link.split("/track/")[-1].split("?")[0])
    return ids


def missing_tracks(url: str, total: int, done: int,
                   new_files: list[str]) -> tuple[int, list[str]]:
    """How far a run came up short, and which tracks are absent by name.

    For an album or a single track the answer comes from Spotify's own track
    list against the files on disk, and that answer is the one that counts -
    it names what is absent and it does not miscount a line spotDL phrased
    unexpectedly.

    Everything else falls back on counting: spotDL's "Found N songs" against
    the tracks it reported on, downloads and skipped duplicates alike. A
    playlist cannot be checked against its folder, because it scatters its
    tracks over as many folders as it has albums, and a track skipped
    because it already sat in a folder nobody looked at would be named as
    missing although it is right there.
    """
    counted = total - done if total and done < total else 0
    if "/album/" in url or "/track/" in url:
        expected = expected_tracks(url)
        folders = {str(Path(f).parent) for f in new_files}
        present = _track_ids_on_disk(folders) if folders else set()
        if expected and present:
            missing = [name for tid, name in expected if tid not in present]
            return len(missing), missing
    return counted, []


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
                         sync_public: bool = True,
                         live_log: LiveLog | None = None):
    """Main entry: Spotify download (URL or search query) via spotDL.

    `live_log` is handed in by album downloads so every track appends to the
    same file instead of truncating it once per track; on its own the run
    opens the requesting user's log.
    """
    q = jobs[job_id]
    log = live_log or LiveLog(download_log_path(requested_by))

    try:
        log.start(f"URL: {url}")
        emit(q, "status", message="Initializing spotDL …", progress=2)
        log.write("Initializing spotDL …")

        start_time = time.time() - 2
        # Snapshot existing playlist files: spotDL writes "{list-name}.m3u8"
        # and would silently overwrite another account's same-named playlist
        # before we get the chance to rename ours (see _handle_playlist).
        # Resolve the playlist name up front so the m3u8 is written with
        # the correct, collision-free file name right away.
        pl_title = pl_unique = None
        if "playlist" in url:
            pl_title = playlist_title(url)
            if pl_title:
                usable = visible_name(pl_title, url)
                if usable != pl_title:
                    notice = (f"Playlist name '{pl_title}' would make a hidden "
                               f"file; using '{usable}' instead")
                    emit(q, "log", line=notice)
                    log.write(notice)
                    pl_title = usable
                pl_unique = syncreg.resolve_playlist_name(pl_title, url, owner_id)

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
        refused_urls: list[str] = []
        # Cookies are a help until they are the problem. YouTube can refuse
        # media to a signed-in session while serving the same track to an
        # anonymous one, and then every single download fails. A fresh cookie
        # file does not help - being signed in is what is refused.
        use_cookies = True

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
                _download_cmd(url, pl_unique, use_cookies),
                cwd=cfg.library.music_root,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )

            attempt_done = 0
            attempt_downloaded = 0
            attempt_refused = 0
            had_block_error = False
            awaiting_url = False

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
                    awaiting_url = False
                    if "Downloaded" in line:
                        attempt_downloaded += 1
                    done = max(done, attempt_done)
                    m = re.search(r'"([^"]+)"', line)
                    track = m.group(1) if m else "..."
                    progress = int(5 + (done / total) * 80) if total else 50
                    emit(q, "progress",
                         current=done, total=total, track=track,
                         progress=progress, line=line)
                    log.write(line)
                    continue

                if any(p in line.lower() for p in PROVIDER_FAILURE):
                    # Counted, not announced: on a playlist where every track
                    # is refused this would be fifty-five identical lines,
                    # each one counted as an error of its own. The attempt
                    # says it once, below, when it knows how it ended.
                    attempt_refused += 1
                    awaiting_url = True
                    continue

                # The URL on the line after a refusal is the only trace of
                # which track it was, so it is kept instead of printed on its
                # own. A bare link in the log says nothing to anybody.
                if awaiting_url and line.startswith("http"):
                    if line not in refused_urls:
                        refused_urls.append(line)
                    awaiting_url = False
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

            # A run that refused every track and downloaded none is not a
            # success, whatever spotDL's exit code says - and on 15.11.2026 it
            # said 0. If cookies were in play, they are the first suspect:
            # dropping them costs one more attempt and fixed every track that
            # evening. Only worth doing while there is an attempt left.
            if (use_cookies and attempt_refused and not attempt_downloaded
                    and _cookie_file_in_use() and attempt < max_attempts):
                use_cookies = False
                note = (f"⚠ {attempt_refused} track(s) refused while signed in "
                        f"– retrying without cookies")
                emit(q, "log", line=note)
                log.write(note)
                continue

            if proc.returncode == 0:
                success = True
                break

            fail_msg = (f"⚠ Attempt {attempt} failed "
                        f"(exit {proc.returncode}, block={had_block_error})")
            emit(q, "log", line=fail_msg)
            log.write(fail_msg)

        # Said once, at the end, and only when the retry did not save it.
        if attempt_refused and not attempt_downloaded:
            refused_msg = (f"✗ {attempt_refused} track(s) refused by the "
                           f"server - no audio was served for them")
            emit(q, "log", line=refused_msg)
            log.write(refused_msg)

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
                                 requested_by, sync_public, prev_m3us,
                                 pl_title, pl_unique)
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

        # Until 21.11.2026 a run that fetched most of an album ended with
        # "Done!" and nothing else. The refusal lines were counted and then
        # dropped, and that count was only ever announced when *every* track
        # had failed - so one age-restricted track out of 34 left no trace
        # but a bare YouTube URL in the log (GTA VI album, 20.11.2026). What
        # Spotify lists is the yardstick, and it is asked whenever the run
        # came back short.
        short_by, missing = missing_tracks(url, total, done, new_files)
        if short_by:
            _report_shortfall(q, log, url, short_by, total, missing,
                              refused_urls)

        log.summarise(lambda line: emit(q, "log", line=line))

        done_msg = f"Done! {done} tracks processed."
        if failed_tracks:
            done_msg += f" ({len(failed_tracks)} failed)"
        if short_by:
            done_msg += f" ({short_by} missing)"
        log.finish(done_msg)
        emit(q, "done", message=done_msg, progress=100, total_tracks=done)

    except Exception as e:
        log.fail(str(e))
        emit(q, "error", message=str(e))


def _report_shortfall(q, log: LiveLog, url: str, short_by: int, total: int,
                      missing: list[str], refused: list[str]) -> None:
    """Puts the tracks that never arrived into the log and onto the phone."""
    head = (f"\u26a0 {short_by} of {total} tracks did not arrive"
            if total else f"\u26a0 {short_by} track(s) did not arrive")
    emit(q, "log", line="")
    emit(q, "log", line=head)
    log.write(head)
    for name in missing:
        emit(q, "log", line=f"  \u2022 {name}")
        log.write(f"  \u2022 {name}")
    for link in refused:
        line = f"  refused by the audio source: {link}"
        emit(q, "log", line=line)
        log.write(line)

    if not notify.enabled():
        return
    body = [head.replace("\u26a0 ", "")]
    body += [f"\u2022 {name}" for name in missing[:10]]
    if len(missing) > 10:
        body.append(f"\u2026 and {len(missing) - 10} more")
    if not missing:
        body += refused[:5]
    body.append(url)
    title = ("Nightshift: 1 track missing" if short_by == 1
             else f"Nightshift: {short_by} tracks missing")
    notify.send(title, "\n".join(body), priority="high", tags="warning")


def fix_disc_numbers(paths: list[str]) -> list[tuple[str, str, str]]:
    """Puts the right disc number on tracks of a multi-disc album.

    Spotify's single-track endpoint answers disc_number 1 for every track,
    whichever disc it is really on; only the album's own track list has it
    right. spotDL writes what it is handed, so a track from disc 2 arrives
    labelled 1/2 and the media server files it under the wrong disc.

    Only albums that really have more than one disc are looked up, and each
    album is fetched once - an ordinary download makes no extra request at
    all. A failed lookup leaves the file alone: a wrong disc number is better
    than a failed download.
    """
    fixed: list[tuple[str, str, str]] = []
    candidates = []
    for path in paths:
        try:
            tags = ID3(path)
        except Exception:
            continue
        position, source = tags.get("TPOS"), tags.get("WOAS")
        if position is None or source is None or not position.text:
            continue
        current = str(position.text[0])
        total = current.split("/")[-1]
        if not total.isdigit() or int(total) < 2:
            continue                     # single-disc album, nothing to check
        candidates.append((path, tags, current, total, str(source.url)))

    if not candidates:
        return fixed

    try:
        spotify = _spotify_client()
    except Exception:
        return fixed

    albums: dict[str, dict[str, int]] = {}

    def discs_of(album_id: str) -> dict[str, int]:
        if album_id not in albums:
            mapping, offset = {}, 0
            while True:
                page = spotify.album_tracks(album_id, limit=50, offset=offset)
                for item in page.get("items", []):
                    mapping[item["id"]] = item.get("disc_number", 1)
                if not page.get("next"):
                    break
                offset += 50
            albums[album_id] = mapping
        return albums[album_id]

    for path, tags, current, total, url in candidates:
        try:
            track_id = url.split("/track/")[-1].split("?")[0]
            album_id = spotify.track(url)["album"]["id"]
            correct = discs_of(album_id).get(track_id)
        except Exception:
            continue
        if not correct:
            continue
        wanted = f"{correct}/{total}"
        if wanted == current:
            continue
        tags.setall("TPOS", [TPOS(encoding=3, text=[wanted])])
        try:
            tags.save(path, v2_version=3)
        except Exception:
            continue
        fixed.append((path, current, wanted))

    return fixed


def _post_process(q, log: LiveLog, new_files: list[str]):
    """Beets import, beet write and optional lyrics for new Spotify files."""
    corrected = fix_disc_numbers(new_files)
    if corrected:
        line = f"Disc numbers corrected: {len(corrected)} track(s)"
        emit(q, "log", line=line)
        log.write(line)

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

    if cfg.nightly.fetch_lyrics and lyrics.available():
        emit(q, "status",
             message=f"Fetching lyrics ({len(new_files)} tracks) …",
             progress=96)
        log.write(f"Fetching lyrics ({len(new_files)} tracks) …")
        counts = lyrics.fetch(new_files)
        log.write(f"Lyrics: {counts['synced']} synced, {counts['plain']} plain, "
                  f"{counts['missing']} without")


def _ensure_playlist_directive(m3u_path: str, title: str, replace: bool = False):
    """Add #PLAYLIST:<title> so media servers show the original name.

    With `replace` an existing directive is overwritten - that is a rename,
    and the media server picks the new name up when it reads the file again.
    """
    try:
        with open(m3u_path) as f:
            lines = f.read().splitlines()
    except OSError:
        return
    if any(l.startswith("#PLAYLIST:") for l in lines):
        if not replace:
            return
        lines = [l for l in lines if not l.startswith("#PLAYLIST:")]
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
                     prev_m3us: dict | None = None,
                     known_title: str | None = None,
                     known_unique: str | None = None):
    """Navidrome visibility + optional sync registration for playlists.

    Only considers m3u8 files written during THIS job (mtime > since),
    so playlists can never be attributed to the wrong download.
    """
    m3us = [m for m in glob.glob(f"{cfg.library.music_root}/*.m3u8")
            if getmtime(m) > since]
    if not m3us:
        return
    newest = max(m3us, key=getmtime)
    # The title fetched before the download wins; the file name is only a
    # fallback and may still carry an unresolved spotDL placeholder.
    pl_title = visible_name(known_title or splitext(basename(newest))[0], url)

    if looks_unresolved(pl_title):
        msg = ("Could not determine the playlist name — the tracks were "
               "downloaded, but no playlist file was created")
        emit(q, "log", line=msg)
        log.write(msg)
        try:
            os.remove(newest)
        except OSError:
            pass
        return

    # Personalized playlists ("Discover Weekly") collide across accounts:
    # give this one a unique file name and keep the real title in #PLAYLIST.
    unique = known_unique or syncreg.resolve_playlist_name(pl_title, url, owner_id)
    target = f"{cfg.library.music_root}/{unique}.m3u8"
    if os.path.abspath(newest) != os.path.abspath(target):
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
    ok, m = navidrome.apply_playlist_settings(pl_title, owner_id, path=newest)
    emit(q, "log", line=m)
    log.write(m)

    if sync:
        sync_dir = cfg.nightly.spotdl_sync_dir
        os.makedirs(sync_dir, exist_ok=True)
        safe = re.sub(r"[^\w\- ()]", "_", unique).strip()
        spotdl_file = f"{sync_dir}/{safe}.spotdl"
        cmd = (_spotdl_base_cmd() + ["sync", url, "--save-file", spotdl_file]
               + YTDLP_ARGS)
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
