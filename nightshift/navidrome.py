"""Navidrome integration: playlist visibility and owner assignment.

Optional – only active when cfg.navidrome.enabled is True.
Uses Navidrome's internal REST API (the same one the web UI uses).
Note: the playlist API's name query parameter raises "ambiguous column
name" in some versions – hence the client-side filtering.
"""
from __future__ import annotations

import json
import time
import urllib.request

from .config import cfg


def enabled() -> bool:
    return bool(cfg.navidrome.enabled and cfg.navidrome.username and cfg.navidrome.password)


def _req(method: str, path: str, token: str | None = None, data=None):
    url = cfg.navidrome.url.rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    if token:
        headers["x-nd-authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


def login() -> str:
    if not enabled():
        raise RuntimeError("Navidrome integration not configured")
    res = _req("POST", "/auth/login",
               data={"username": cfg.navidrome.username,
                     "password": cfg.navidrome.password})
    return res["token"]


def list_users(token: str) -> list[dict]:
    """All Navidrome users (excluding the service account) for the dropdown."""
    try:
        res = _req("GET", "/api/user?_start=0&_end=100&_sort=name", token=token)
        service_user = (cfg.navidrome.username or "").lower()
        return [{"id": u["id"],
                 "userName": u.get("userName", ""),
                 "name": u.get("name", u.get("userName", ""))}
                for u in res
                if u.get("userName", "").lower() != service_user]
    except Exception:
        return []


def find_playlist_by_name(token: str, name: str):
    """Find a playlist by name – filtered client-side (API bug workaround)."""
    res = _req("GET", "/api/playlist?_start=0&_end=200&_sort=updatedAt&_order=DESC",
               token=token)
    matches = [pl for pl in res if pl.get("name") == name]
    return matches[0] if matches else None


def set_public(token: str, playlist_id: str, public: bool = True):
    return _req("PUT", f"/api/playlist/{playlist_id}",
                token=token, data={"public": public})


def set_owner(token: str, playlist_id: str, owner_id: str):
    return _req("PUT", f"/api/playlist/{playlist_id}",
                token=token, data={"ownerId": owner_id, "public": False})


def set_visibility(name: str, public: bool) -> tuple[bool, str]:
    """Apply a visibility change to an already-imported playlist.

    Used by the sync page editor. Unlike apply_playlist_settings this does
    not wait for an import — the playlist is expected to exist already, so
    a single lookup is enough.
    """
    if not enabled():
        return True, "Navidrome integration disabled - skipped"
    try:
        token = login()
    except Exception as e:
        return False, f"Navidrome login failed: {e}"
    try:
        pl = find_playlist_by_name(token, name)
    except Exception as e:
        return False, f"Navidrome request failed: {e}"
    if not pl:
        return False, f"Playlist '{name}' not found in Navidrome"
    try:
        set_public(token, pl["id"], public)
    except Exception as e:
        return False, f"Could not update playlist '{name}': {e}"
    return True, (f"Playlist '{name}' set to public in Navidrome" if public
                  else f"Playlist '{name}' set to private in Navidrome")


def apply_playlist_settings(name: str, owner_id: str | None = None) -> tuple[bool, str]:
    """Waits for the playlist import, then sets it public or assigns an owner.

    No-op with a success message when the integration is disabled.
    """
    if not enabled():
        return True, "Navidrome integration disabled - skipped"

    try:
        token = login()
    except Exception as e:
        return False, f"Navidrome login failed: {e}"

    pl = None
    for _ in range(int(cfg.navidrome.import_retries)):
        try:
            pl = find_playlist_by_name(token, name)
        except Exception:
            pl = None
        if pl:
            break
        time.sleep(int(cfg.navidrome.import_retry_delay))

    if not pl:
        return False, f"Playlist '{name}' not found in Navidrome"

    try:
        if owner_id:
            set_owner(token, pl["id"], owner_id)
            return True, f"Playlist '{name}': owner assigned"
        if cfg.navidrome.default_public:
            set_public(token, pl["id"], True)
            return True, f"Playlist '{name}' set to public"
        return True, f"Playlist '{name}' imported (private, default)"
    except Exception as e:
        return False, f"Could not update playlist '{name}': {e}"
