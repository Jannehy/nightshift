"""Synced lyrics for the library: an .lrc next to the file, and USLT inside it.

Two forms are wanted, because players disagree about where lyrics live: a
sidecar .lrc with timestamps, which is what Navidrome reads for its scrolling
view, and a plain USLT frame in the file itself for everything that only knows
ID3. A track is asked about only when one of the two is missing.

Providers are tried in order and the first synced hit wins; if nothing synced
exists, plain text still fills the USLT frame. A miss leaves the file alone —
half a lyric is worse than none.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable, Iterable

try:
    import syncedlyrics
except ImportError:                                   # pragma: no cover
    syncedlyrics = None

try:
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3, USLT
    from mutagen.mp3 import MP3
except ImportError:                                   # pragma: no cover
    EasyID3 = ID3 = USLT = MP3 = None

from .config import cfg

PROVIDERS = ["Lrclib", "Musixmatch", "NetEase"]

# The providers are free services; a short pause keeps a library-wide run from
# looking like an attack.
PAUSE_SECONDS = 0.5

LRC_LINE = re.compile(r"^\[\d+:\d+(?:\.\d+)?\]\s*")


def available() -> bool:
    """Whether lyrics can be fetched at all in this installation."""
    return syncedlyrics is not None and ID3 is not None


def _plain(lrc: str) -> str:
    """The same lyric without its timestamps, for the USLT frame."""
    lines = [LRC_LINE.sub("", line) for line in lrc.splitlines()]
    return "\n".join(line for line in lines if line.strip())


def _is_synced(text: str) -> bool:
    return bool(LRC_LINE.search(text or ""))


def _has_uslt(path: Path) -> bool:
    try:
        return any(key.startswith("USLT") for key in ID3(path).keys())
    except Exception:
        return False


def _embed(path: Path, text: str) -> None:
    try:
        audio = MP3(path, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.delall("USLT")
        audio.tags.add(USLT(encoding=3, lang="eng", desc="", text=text))
        # v2.3 on purpose: the rest of the library is written that way, and
        # older players ignore v2.4 frames.
        audio.save(v2_version=3)
    except Exception:
        pass


def _artist_title(path: Path) -> tuple[str, str]:
    try:
        tags = EasyID3(path)
        return (tags.get("artist", [""])[0] or "",
                tags.get("title", [""])[0] or "")
    except Exception:
        return "", ""


def fetch(paths: Iterable[str] | None = None, *, force: bool = False,
          progress: Callable[[str], None] | None = None) -> dict:
    """Fetches lyrics for [paths], or for the whole library when none is given.

    [force] asks again for tracks that already have lyrics and replaces them
    when something is found; a miss keeps what is there. Returns the counts.
    """
    result = {"considered": 0, "synced": 0, "plain": 0, "missing": 0}
    if not available():
        return result

    if paths is None:
        root = Path(cfg.library.music_root)
        candidates = sorted(root.rglob("*.mp3"))
    else:
        candidates = [Path(p) for p in paths
                      if str(p).lower().endswith(".mp3") and Path(p).exists()]

    todo = []
    for path in candidates:
        needs_lrc = force or not path.with_suffix(".lrc").exists()
        needs_uslt = force or not _has_uslt(path)
        if needs_lrc or needs_uslt:
            todo.append((path, needs_lrc, needs_uslt))

    result["considered"] = len(todo)
    for path, needs_lrc, needs_uslt in todo:
        artist, title = _artist_title(path)
        if not artist or not title:
            result["missing"] += 1
            continue
        query = f"{title} {artist}"
        try:
            found = syncedlyrics.search(query, synced_only=True,
                                        providers=PROVIDERS)
            if found and _is_synced(found):
                if needs_lrc:
                    path.with_suffix(".lrc").write_text(found, encoding="utf-8")
                if needs_uslt:
                    _embed(path, _plain(found))
                result["synced"] += 1
            elif needs_uslt:
                found = syncedlyrics.search(query, plain_only=True,
                                            providers=PROVIDERS)
                if found:
                    _embed(path, found)
                    result["plain"] += 1
                else:
                    result["missing"] += 1
            else:
                result["missing"] += 1
        except Exception:
            # One unreachable provider must not end a library-wide run.
            result["missing"] += 1
        if progress:
            progress(f"{artist} - {title}")
        time.sleep(PAUSE_SECONDS)
    return result
