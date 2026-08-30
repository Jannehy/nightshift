"""Sync registry: playlists the nightly job keeps up to date."""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import navidrome
from .config import cfg


def _registry_path() -> Path:
    p = Path(cfg.sync.registry_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def sync_enabled() -> bool:
    """Sync feature globally on/off (admin setting)."""
    return bool(cfg.sync.enabled)


def _load() -> list[dict]:
    try:
        with open(_registry_path()) as f:
            return json.load(f)
    except Exception:
        return []


def _save(entries: list[dict]):
    path = _registry_path()
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def add(url: str, source: str, name: str,
        owner: str | None = None, public: bool = True,
        folder: str | None = None) -> bool:
    entries = _load()
    for e in entries:
        if e.get("url") == url:
            if folder and not e.get("folder"):
                e["folder"] = folder  # backfill for older entries
                _save(entries)
            return False  # already registered
    entries.append({"url": url, "source": source, "name": name,
                    "owner": owner, "public": bool(public),
                    "folder": folder})
    _save(entries)
    return True


def _sanitize_folder(title: str) -> str:
    import re
    return re.sub(r"[/\\\x00]", "_", title).strip() or "playlist"


def _claimed_by_other(path: str | None, owner_id: str | None,
                      ownership: tuple[dict[str, str], set[str]] | None) -> bool:
    """Whether an existing playlist file belongs to a different account.

    That a file exists says nothing on its own: someone refreshing their own
    playlist has to keep its name, or every repeat download would pile up
    another "(2)". Navidrome knows who owns the playlist imported from the
    file, and that is the question that matters. A download nobody was named
    for is public and lives in the admin's space, so an admin-owned playlist
    counts as its own — a family member's never does.
    """
    if not path or not ownership or not os.path.exists(path):
        return False
    owners, admins = ownership
    owner = owners.get(path)
    if not owner:
        return False
    return owner not in admins if owner_id is None else owner != owner_id


def _resolve_unique(title: str, url: str, sources: tuple[str, ...],
                    path_for=None, owner_id: str | None = None) -> str:
    """Unique on-disk name for a playlist within one source group.

    Personalized playlists ("Your Mix 1", "Discover Weekly") share the same
    title across accounts. The same URL always maps to the same name; a
    different URL claiming a taken title gets a numeric suffix. The pretty
    name still reaches the media server via the m3u8 #PLAYLIST directive.

    A name counts as taken when another sync entry uses it *or* when the file
    it would write already carries someone else's playlist — a one-off
    download is never registered, so the registry alone does not see it. That
    gap once let one account's "Daily Mix 3" overwrite another's.
    """
    safe = _sanitize_folder(title)
    entries = [e for e in _load() if e.get("source") in sources]
    for e in entries:
        if e.get("url") == url:
            return e.get("folder") or _sanitize_folder(e.get("name") or title)
    taken = {e.get("folder") or _sanitize_folder(e.get("name") or "")
             for e in entries if e.get("url") != url}
    ownership = navidrome.playlist_ownership() if path_for else None

    def free(name: str) -> bool:
        if name in taken:
            return False
        return not _claimed_by_other(path_for(name) if path_for else None,
                                     owner_id, ownership)

    if free(safe):
        return safe
    n = 2
    while not free(f"{safe} ({n})"):
        n += 1
    return f"{safe} ({n})"


def resolve_set_folder(title: str, url: str, base_dir: str | None = None,
                       owner_id: str | None = None) -> str:
    """Folder name for a SoundCloud/YouTube set."""
    path_for = (lambda n: f"{base_dir}/{n}/{n}.m3u8") if base_dir else None
    return _resolve_unique(title, url, ("soundcloud", "youtube"),
                           path_for, owner_id)


def resolve_playlist_name(title: str, url: str,
                          owner_id: str | None = None) -> str:
    """Base name for a Spotify playlist's m3u8 and .spotdl sync file."""
    root = cfg.library.music_root.rstrip("/")
    return _resolve_unique(title, url, ("spotify",),
                           lambda n: f"{root}/{n}.m3u8", owner_id)


def remove(url: str) -> bool:
    entries = _load()
    remaining = [e for e in entries if e.get("url") != url]
    if len(remaining) == len(entries):
        return False
    _save(remaining)
    return True


def all_entries() -> list[dict]:
    return _load()


# ---------------------------------------------------------------------
# Combined view: registry entries (SoundCloud/YouTube) + spotDL sync files
# ---------------------------------------------------------------------

def _spotdl_items() -> list[dict]:
    """Spotify playlists kept in sync via .spotdl files in the sync dir.

    These exist independently of the registry (spotDL manages them), so the
    sync page reads the directory directly instead of duplicating state.
    """
    items = []
    sync_dir = Path(cfg.nightly.spotdl_sync_dir)
    if not sync_dir.is_dir():
        return items
    for f in sorted(sync_dir.glob("*.spotdl")):
        url = ""
        try:
            with open(f) as fh:
                data = json.load(fh)
            query = data.get("query")
            if isinstance(query, list) and query:
                url = str(query[0])
            elif isinstance(query, str):
                url = query
        except Exception:
            pass
        items.append({
            "url": url,
            "source": "spotify",
            "name": f.stem,
            "file": f.name,
        })
    return items


def all_sync_items() -> list[dict]:
    """Everything the nightly job keeps up to date, deduplicated by URL.

    Registry entries matching a .spotdl file contribute their metadata
    (owner/public) to that item instead of appearing twice.
    """
    items = _spotdl_items()
    by_url = {i["url"]: i for i in items if i["url"]}
    for e in _load():
        url = e.get("url") or ""
        if url and url in by_url:
            by_url[url]["owner"] = e.get("owner")
            by_url[url]["public"] = e.get("public", True)
            continue
        items.append({**e, "file": None})
    for i in items:
        i.setdefault("owner", None)
        i.setdefault("public", True)  # legacy entries: visible to everyone
    return items


def visible_items_for(username: str, is_admin: bool) -> list[dict]:
    """Items the given user may see, each with a can_remove flag.

    Admins see everything. Regular users see public playlists plus their
    own; they may remove only their own.
    """
    items = all_sync_items()
    out = []
    for i in items:
        mine = bool(username) and i.get("owner") == username
        if not (is_admin or i.get("public", True) or mine):
            continue
        out.append({**i, "can_remove": is_admin or mine})
    return out


def set_meta(url: str = "", filename: str = "",
             owner: str | None = None, public: bool = True) -> bool:
    """Admin edit: set owner/public for a sync item.

    Registry entries are updated in place. For .spotdl files without a
    registry entry (migrated playlists), one is created so the metadata
    has a home — the registry doubles as the metadata store.
    """
    entries = _load()
    for e in entries:
        if url and e.get("url") == url:
            e["owner"] = owner or None
            e["public"] = bool(public)
            _save(entries)
            return True
    if filename:
        for item in _spotdl_items():
            if item["file"] == filename:
                entries.append({"url": item["url"], "source": "spotify",
                                "name": item["name"],
                                "owner": owner or None,
                                "public": bool(public)})
                _save(entries)
                return True
    return False


def display_name_of(url: str = "", filename: str = "") -> str | None:
    """The playlist's real title — the name it carries in the media server."""
    for i in all_sync_items():
        if (url and i.get("url") == url) or (filename and i.get("file") == filename):
            return i.get("name")
    return None


def owner_of(url: str = "", filename: str = "") -> str | None:
    for i in all_sync_items():
        if (url and i.get("url") == url) or (filename and i.get("file") == filename):
            return i.get("owner")
    return None


def file_path_of(url: str = "", filename: str = "") -> str | None:
    """The playlist file behind a sync item — its unambiguous key.

    Names repeat across accounts, so anything acting on a single playlist
    (visibility, owner) has to say which file it means. Registry entries know
    their on-disk name; a .spotdl file without an entry only knows its own
    stem, which is good enough for playlists whose title needs no sanitizing.
    """
    root = cfg.library.music_root.rstrip("/")
    entry = None
    if url:
        entry = next((e for e in _load() if e.get("url") == url), None)
    if entry is None and filename:
        item = next((i for i in _spotdl_items() if i["file"] == filename), None)
        if item is None:
            return None
        entry = (next((e for e in _load() if e.get("url") == item["url"]), None)
                 if item["url"] else None) or {"source": "spotify",
                                               "name": item["name"]}
    if entry is None:
        return None
    name = entry.get("folder") or _sanitize_folder(entry.get("name") or "")
    if not name:
        return None
    source = entry.get("source")
    if source == "soundcloud":
        return f"{root}/{cfg.library.soundcloud_dir}/{name}/{name}.m3u8"
    if source == "youtube":
        return f"{root}/{cfg.library.youtube_dir}/{name}/{name}.m3u8"
    return f"{root}/{name}.m3u8"


def remove_item(url: str = "", filename: str = "") -> bool:
    """Remove a sync entry: registry entry, .spotdl file, or both."""
    removed = False
    if filename:
        target = Path(cfg.nightly.spotdl_sync_dir) / Path(filename).name
        if target.is_file() and target.suffix == ".spotdl":
            target.unlink()
            removed = True
    if url and remove(url):
        removed = True
    return removed
