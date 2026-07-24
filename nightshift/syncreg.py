"""Sync registry: playlists the nightly job keeps up to date."""
from __future__ import annotations

import json
import os
from pathlib import Path

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


def _resolve_unique(title: str, url: str, sources: tuple[str, ...]) -> str:
    """Unique on-disk name for a playlist within one source group.

    Personalized playlists ("Your Mix 1", "Discover Weekly") share the same
    title across accounts. The same URL always maps to the same name; a
    different URL claiming a taken title gets a numeric suffix. The pretty
    name still reaches the media server via the m3u8 #PLAYLIST directive.
    """
    safe = _sanitize_folder(title)
    entries = [e for e in _load() if e.get("source") in sources]
    for e in entries:
        if e.get("url") == url:
            return e.get("folder") or _sanitize_folder(e.get("name") or title)
    taken = {e.get("folder") or _sanitize_folder(e.get("name") or "")
             for e in entries if e.get("url") != url}
    if safe not in taken:
        return safe
    n = 2
    while f"{safe} ({n})" in taken:
        n += 1
    return f"{safe} ({n})"


def resolve_set_folder(title: str, url: str) -> str:
    """Folder name for a SoundCloud/YouTube set."""
    return _resolve_unique(title, url, ("soundcloud", "youtube"))


def resolve_playlist_name(title: str, url: str) -> str:
    """Base name for a Spotify playlist's m3u8 and .spotdl sync file."""
    return _resolve_unique(title, url, ("spotify",))


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


def owner_of(url: str = "", filename: str = "") -> str | None:
    for i in all_sync_items():
        if (url and i.get("url") == url) or (filename and i.get("file") == filename):
            return i.get("owner")
    return None


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
