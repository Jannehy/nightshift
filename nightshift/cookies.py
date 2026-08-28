"""Cookie files: whether they still sign in, and how long they claim to last.

Cookies do not fail loudly. Downloads that need a signed-in session come back
censored or not at all, and the run still reports success. Worse, the expiry
dates in the file are close to useless as a warning: a file whose login cookie
claimed 294 days left was already being turned away after six. Google rotates
and revokes sessions long before the stamps run out.

So the honest check is to use the cookies and see whether the site still knows
us. That costs one request, which is why the result is cached and refreshed by
the nightly run and whenever a new file is uploaded.
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import time
import urllib.request
from pathlib import Path

from .config import cfg

# Which cookie carries the session differs per site, but the names that matter
# all look alike: Google's SID family, LOGIN_INFO, an oauth token. The pattern
# is deliberately narrow - a first attempt matched "TOKEN" as well and picked
# up __Secure-ROLLOUT_TOKEN, a rollout flag with a much shorter life.
SESSION_NAMES = re.compile(r"SID|LOGIN|OAUTH|SESSION", re.IGNORECASE)

KINDS = {
    "youtube": ("downloads", "youtube_cookie_file"),
    "soundcloud": ("downloads", "soundcloud_cookie_file"),
}

# One page per site, and the string that only appears for a signed-in visitor.
PROBES = {
    "youtube": ("https://www.youtube.com/", '"LOGGED_IN":true'),
    "soundcloud": ("https://soundcloud.com/", '"logged_in_user"'),
}

CACHE = Path("/config/cookie-check.json")
CACHE_MAX_AGE = 6 * 3600


def _expiries(path: Path) -> tuple[list[float], list[float]]:
    """All expiry stamps in the file, and those of the session cookies."""
    every, session = [], []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        try:
            stamp = float(fields[4])
        except ValueError:
            continue
        if stamp <= 0:          # a session cookie, gone when the browser closes
            continue
        every.append(stamp)
        if SESSION_NAMES.search(fields[5]):
            session.append(stamp)
    return every, session


def live_check(kind: str, path: str) -> bool | None:
    """True if the site still recognises the session, None if unanswerable."""
    probe = PROBES.get(kind)
    if not probe or not path:
        return None
    url, marker = probe
    jar = http.cookiejar.MozillaCookieJar()
    try:
        jar.load(path, ignore_discard=True, ignore_expires=True)
    except Exception:
        return None
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", "Mozilla/5.0")]
    try:
        with opener.open(url, timeout=20) as response:
            body = response.read(400_000).decode("utf-8", "replace")
    except Exception:
        return None
    return marker in body


def _cache() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def refresh(kind: str | None = None) -> dict:
    """Runs the live check and remembers the answer."""
    data = _cache()
    for name in ([kind] if kind else list(KINDS)):
        section, key = KINDS[name]
        path = cfg.path(f"{section}.{key}") or ""
        data[name] = {"signed_in": live_check(name, path), "checked_at": time.time()}
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
    return data


def status(kind: str, warn_days: int | None = None) -> dict:
    """State of one cookie file, worst finding first."""
    section, key = KINDS[kind]
    warn_days = warn_days if warn_days is not None else int(
        getattr(cfg.notifications, "cookie_warn_days", 14) or 14)
    configured = cfg.path(f"{section}.{key}") or ""
    result = {"kind": kind, "path": configured, "state": "unset",
              "expires_at": None, "days_left": None, "cookies": 0,
              "signed_in": None, "checked_at": None}
    if not configured:
        return result

    path = Path(configured)
    if not path.is_file():
        result["state"] = "missing"
        return result

    every, session = _expiries(path)
    result["cookies"] = len(every)

    cached = _cache().get(kind) or {}
    result["signed_in"] = cached.get("signed_in")
    result["checked_at"] = cached.get("checked_at")

    relevant = session or every
    if relevant:
        soonest = min(relevant)
        result["expires_at"] = soonest
        result["days_left"] = round((soonest - time.time()) / 86400, 1)

    # A session the site no longer honours beats any date in the file.
    if result["signed_in"] is False:
        result["state"] = "signed_out"
    elif result["days_left"] is not None and result["days_left"] <= 0:
        result["state"] = "expired"
    elif result["days_left"] is not None and result["days_left"] <= warn_days:
        result["state"] = "soon"
    else:
        result["state"] = "ok"
    return result


# How long a standing problem stays quiet before it says so again. Nightly is
# nightly: without this, one dead cookie file would send the same message every
# night until it is fixed, and the fourth one is already noise.
REMIND_AFTER = 7 * 86400


def should_notify(entry: dict) -> bool:
    """Whether this finding is new enough to be worth a push message."""
    remembered = (_cache().get(entry["kind"]) or {})
    if remembered.get("notified_state") != entry["state"]:
        return True
    last = remembered.get("notified_at") or 0
    return (time.time() - last) > REMIND_AFTER


def mark_notified(entry: dict) -> None:
    data = _cache()
    record = data.setdefault(entry["kind"], {})
    record["notified_state"] = entry["state"]
    record["notified_at"] = time.time()
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def recovered(entry: dict) -> bool:
    """True when a file that was reported broken works again."""
    remembered = (_cache().get(entry["kind"]) or {})
    was = remembered.get("notified_state")
    return entry["state"] == "ok" and was not in (None, "ok")


def all_status(warn_days: int | None = None) -> list[dict]:
    return [status(kind, warn_days) for kind in KINDS]


def needs_attention(warn_days: int | None = None) -> list[dict]:
    """Only the entries a user should be told about."""
    return [s for s in all_status(warn_days)
            if s["state"] in ("signed_out", "soon", "expired", "missing")]
