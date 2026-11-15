"""Cookie files: whether they still sign in, and how long they claim to last.

Cookies do not fail loudly. Downloads that need a signed-in session come back
censored or not at all, and the run still reports success. Worse, the expiry
dates in the file are close to useless as a warning: a file whose login cookie
claimed 294 days left was already being turned away after six. Google rotates
and revokes sessions long before the stamps run out.

So the honest check is to use the cookies and see whether the site still knows
us. That costs one request, which is why the result is cached and refreshed by
the nightly run and whenever a new file is uploaded.

That turned out not to be enough. On 15.11.2026 a cookie file still answered
`"LOGGED_IN":true` on youtube.com while every download had been failing with
HTTP 403 for ten days: Google had stopped serving media to that session but
still recognised it on the website. The check reported green, the nightly runs
reported "0 new tracks", and nothing pointed at the cookies.

Being known and being served are two different permissions, so the check now
asks for both. `media_check` fetches real audio through yt-dlp, the way a
download would, and it does so twice: once with the cookie file and once
without. Only the difference between the two is about the cookies.

That second run matters more than it looks. A fresh cookie file exported the
same evening was refused exactly like the old one - the problem was never the
age of the cookies but that this server asks for media while signed in at all.
A check that only ever ran "with cookies" would have called that a dead
session and sent the user off to export another one, which is precisely the
loop this is meant to end.
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import shutil
import subprocess
import tempfile
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

# What to fetch for the media check. Picking a fixed video turned out to be a
# trap: on 15.11.2026 a refused session still served "Me at the zoo" and the
# official "Never Gonna Give You Up" perfectly well, while every ordinary
# music track came back 403. A probe that happens to pick a survivor reports
# green for a server that cannot download a single song.
#
# So the probe is not a fixed URL. spotDL resolves one the same way it
# resolves everything it downloads, which makes the probe a real sample of the
# production path and leaves no video id to rot. The query is a catalogue song
# that exists in every region; if it ever stops resolving, the check says
# nothing rather than something wrong.
PROBE_QUERY = "Coldplay Yellow"

# SoundCloud goes through yt-dlp too, but spotDL cannot resolve a probe for it
# and every track there can be taken down by its uploader. It stays on the
# sign-in check alone - better no answer than a nightly false alarm about a
# deleted song.
MEDIA_KINDS = ("youtube",)

# What a refusal looks like in yt-dlp's output. A refused session is a fact
# worth reporting; anything else (no network, video gone, geo-block) is not an
# answer about the cookies and must stay silent.
REFUSAL_MARKERS = (
    "http error 403",
    "http error 401",
    "sign in to confirm",
    "this video is only available to",
    "account has been terminated",
)

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


def _probe_url() -> str | None:
    """A music URL of the kind the downloads actually use, resolved now."""
    if not shutil.which("spotdl"):
        return None
    try:
        proc = subprocess.run(["spotdl", "url", PROBE_QUERY],
                              capture_output=True, text=True, timeout=120)
    except Exception:
        return None
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("http"):
            return line
    return None


def _fetch(url: str, cookie_file: str | None) -> tuple[bool, str]:
    """Downloads the probe once. Returns (did a file arrive, what was said)."""
    cmd = ["yt-dlp"]
    if cookie_file:
        cmd += ["--cookies", cookie_file]
    with tempfile.TemporaryDirectory(prefix="cookie-probe-") as tmp:
        cmd += ["--no-playlist", "--no-warnings", "--no-progress",
                "-f", "bestaudio/worst", "-o", str(Path(tmp) / "probe.%(ext)s"),
                url]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=180)
        except Exception:
            return False, ""
        # The file is the proof. yt-dlp can exit 0 having skipped everything,
        # and it can exit non-zero after a post-processing hiccup on a file
        # that arrived perfectly well.
        arrived = any(Path(tmp).iterdir())
    return arrived, f"{proc.stdout or ''}{proc.stderr or ''}".lower()


def media_check(kind: str, path: str) -> bool | None:
    """True if the cookies still get us media, False if they are in the way.

    The question is deliberately comparative. "Can this server download" and
    "can it download *with these cookies*" are different questions, and only
    the second one is about the cookie file. So the probe runs twice, with the
    cookies and without, and the verdict is the difference between the two:

        with  without  verdict
        ----  -------  -------------------------------------------------
        yes   -        True   the cookies are fine, nothing else to ask
        no    yes      False  the cookies are what is blocking downloads
        no    no       None   something else is broken - not the cookies
        no    -        None   no probe, no network, no yt-dlp: no answer

    Returning None on anything inconclusive is the point. A nightly check that
    cries wolf on a flaky connection teaches people to ignore it.
    """
    if kind not in MEDIA_KINDS or not path or not Path(path).is_file():
        return None
    if not shutil.which("yt-dlp"):
        return None
    url = _probe_url()
    if not url:
        return None

    with_cookies, said = _fetch(url, path)
    if with_cookies:
        return True
    if not any(marker in said for marker in REFUSAL_MARKERS):
        return None          # it did not refuse us, it just did not work

    without_cookies, _ = _fetch(url, None)
    return False if without_cookies else None


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
        # Keep what was remembered about notifications - this only replaces
        # the findings.
        record = data.setdefault(name, {})
        record["signed_in"] = live_check(name, path)
        record["media_ok"] = media_check(name, path)
        record["checked_at"] = time.time()
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
              "signed_in": None, "media_ok": None, "checked_at": None}
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
    result["media_ok"] = cached.get("media_ok")
    result["checked_at"] = cached.get("checked_at")

    relevant = session or every
    if relevant:
        soonest = min(relevant)
        result["expires_at"] = soonest
        result["days_left"] = round((soonest - time.time()) / 86400, 1)

    # Refused media beats everything else: it is the only finding that says
    # downloads are broken *right now*. A session can be refused while the
    # website still greets us by name, which is exactly how ten days of empty
    # nightly runs went unnoticed in November 2026.
    if result["media_ok"] is False:
        result["state"] = "refused"
    # A session the site no longer honours beats any date in the file.
    elif result["signed_in"] is False:
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
            if s["state"] in ("refused", "signed_out", "soon", "expired",
                              "missing")]
