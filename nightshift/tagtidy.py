"""Tidies the tags yt-dlp writes for SoundCloud and YouTube downloads.

Those platforms have no notion of an album, so yt-dlp fills the gaps with
whatever the page offers – and for a set that is the playlist owner, not a
musician. A library ends up with an "artist" called SoundCloud holding a few
hundred tracks, titles that repeat the artist name, and promo prefixes.

Three corrections, all of them conservative:

* the album artist is taken from the music, not from the platform: a folder
  whose tracks all name the same artist gets that artist, everything else is
  a compilation and gets Various Artists. Whatever stood there before was the
  owner of the playlist – "SoundCloud", ".", or a person like "Ethan Lewis"
  who merely put the set together,
* a leading "<artist> - " is removed from the title, but only when the prefix
  really is the artist tag, so "The Boy Is Mine feat. Rosalie - James Mac"
  stays as it is,
* several artists in one tag are separated the way ID3v2.3 wants it, with a
  slash. SoundCloud hands them over as "A， B" or "A • B", which every player
  reads as one long artist name; "A/B" is the form Navidrome and the Spotify
  downloads already agree on. The album artist keeps commas, because it is a
  label rather than a list.

Spotify downloads never pass through here. spotDL gets its metadata from
Spotify itself, where it is already correct.
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import mutagen
except ImportError:                                   # pragma: no cover
    mutagen = None

AUDIO_SUFFIXES = {".mp3", ".m4a", ".opus", ".flac", ".ogg", ".wav"}

VARIOUS = "Various Artists"

# "HMWL Premiere: ", "[Free Download] ", "PREMIERE | " and relatives.
PROMO = re.compile(
    r"^\s*(?:[\[(]\s*)?[^\[\](]{0,40}?"
    r"(?:premiere|free\s*download|exclusive|out\s*now|forthcoming)"
    r"\s*(?:[\])]\s*)?[:|\-–]\s*",
    re.IGNORECASE,
)

# Everything the platforms use between two artists. The full-width forms come
# from their own sanitising, since a file name cannot hold the plain ones.
SEPARATORS = ("，", "、", "•", "/", ",")


def artist_parts(value: str) -> list[str]:
    """The individual names inside one artist tag."""
    for separator in SEPARATORS:
        value = value.replace(separator, ",")
    return [part.strip() for part in value.split(",") if part.strip()]


def artist_tag(value: str) -> str:
    """How several artists belong in the tag: separated by a slash."""
    return "/".join(artist_parts(value))


def artist_label(value: str) -> str:
    """How several artists read as one line, for the album artist."""
    return ", ".join(artist_parts(value))


def strip_artist_prefix(title: str, artist: str) -> str:
    """Removes a leading "<artist> - " – but only when it really is one."""
    if not artist:
        return title
    for name in [artist_label(artist), *artist_parts(artist)]:
        if not name:
            continue
        prefix = name.lower() + " - "
        if title.lower().startswith(prefix) and len(title) > len(prefix) + 2:
            return title[len(prefix):].strip()
    return title


def proposal(tags: dict[str, str], folder_artist: str = "") -> dict[str, str]:
    """The tidied values for one file.

    [folder_artist] is the one artist behind the whole folder, or empty when
    the folder holds several – see [folder_artist_of].
    """
    raw = tags.get("artist", "")
    title = tags.get("title", "")
    cleaned = strip_artist_prefix(PROMO.sub("", title).strip(), raw)
    return {
        "artist": artist_tag(raw),
        "title": cleaned or title,
        "albumartist": artist_label(folder_artist) if folder_artist else VARIOUS,
    }


def _read(path: Path):
    audio = mutagen.File(path, easy=True)
    if audio is None:
        return None, {}

    def one(key: str) -> str:
        value = audio.get(key)
        if isinstance(value, list):
            value = value[0] if value else ""
        return value or ""

    return audio, {key: one(key) for key in ("artist", "title", "albumartist")}


def folder_artist_of(folder: Path) -> str:
    """The single artist behind a folder, or "" when there are several.

    A set is a compilation unless everything in it is by the same person, and
    that question can only be answered by looking at the folder as a whole.
    """
    artists: set[str] = set()
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return ""
    for path in entries:
        if path.suffix.lower() not in AUDIO_SUFFIXES or not path.is_file():
            continue
        try:
            _, tags = _read(path)
        except Exception:
            continue
        name = artist_tag(tags.get("artist", ""))
        if name:
            artists.add(name)
        if len(artists) > 1:
            return ""
    return artists.pop() if len(artists) == 1 else ""


def tidy(paths: list[str]) -> int:
    """Cleans up the given files in place. Returns how many were changed."""
    if mutagen is None:
        return 0
    changed = 0
    # One look per folder, however many files of it were handed over.
    folder_artists: dict[Path, str] = {}
    for name in paths:
        path = Path(name)
        if path.suffix.lower() not in AUDIO_SUFFIXES or not path.is_file():
            continue
        try:
            audio, tags = _read(path)
            if audio is None:
                continue
            if path.parent not in folder_artists:
                folder_artists[path.parent] = folder_artist_of(path.parent)
            wanted = {key: value
                      for key, value in proposal(tags,
                                                 folder_artists[path.parent]).items()
                      if value and value != tags.get(key, "")}
            if not wanted:
                continue
            for key, value in wanted.items():
                audio[key] = value
            audio.save()
            changed += 1
        except Exception:
            # A single unreadable file must not stop a whole download.
            continue
    return changed
