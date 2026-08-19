"""Central log handling – all paths come from the config."""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

from .config import cfg


def _log_dir() -> Path:
    d = Path(cfg.logging.dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _user_slug(user: str | None) -> str | None:
    """File-name-safe form of a user name.

    User names are compared case-insensitively when accounts are created, so
    the slug lowercases too. Sanitising can map two different names onto the
    same file ("a/b" and "a_b"), which would leak one user's log to the other –
    a short digest keeps them apart whenever anything had to be replaced.
    """
    if not user:
        return None
    name = user.strip().lower()
    if not name:
        return None
    slug = re.sub(r"[^a-z0-9._-]", "_", name)
    if slug != name:
        slug = f"{slug}-{hashlib.sha1(name.encode()).hexdigest()[:8]}"
    return slug[:64]


def download_log_path(user: str | None = None) -> Path:
    """Live download log of one user.

    Downloads run one at a time, but the log outlives the job: with a single
    shared file the next person's download truncates the previous one's log
    and everybody sees a run that is not theirs. Each user therefore keeps
    their own file. Jobs without a user fall back to the legacy path.
    """
    slug = _user_slug(user)
    return _log_dir() / (f"download-live-{slug}.log" if slug else "download-live.log")


def remove_download_log(user: str) -> None:
    """Drop a user's log file – called when the account is deleted."""
    try:
        download_log_path(user).unlink(missing_ok=True)
    except OSError:
        pass


def nightly_log_path() -> Path:
    return _log_dir() / "nightly-live.log"


class LiveLog:
    """Live log file with start/end markers (enables UI restore after reload)."""

    def __init__(self, path: Path):
        self.path = path

    def start(self, *header_lines: str):
        with open(self.path, "w") as f:
            f.write(f"=== Start: {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            for line in header_lines:
                f.write(f"=== {line} ===\n")

    def write(self, line: str):
        try:
            with open(self.path, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def finish(self, message: str = ""):
        self.write(f"=== DONE: {message} ===")

    def fail(self, message: str = ""):
        self.write(f"=== FAILED: {message} ===")

    def child(self) -> "SubLog":
        """A view on the same file for a sub-job (e.g. one album track).

        Sub-jobs must not truncate the file or stamp the DONE/FAILED markers –
        those tell the UI whether the *whole* run is still going.
        """
        return SubLog(self.path)

    def read_state(self) -> dict:
        content = ""
        mtime = 0.0
        if self.path.exists():
            content = self.path.read_text()
            mtime = os.path.getmtime(self.path)
        finished = "=== DONE" in content or "=== FERTIG" in content
        failed = "=== FAILED" in content or "=== FEHLER" in content
        return {
            "log": content,
            "mtime": mtime,
            "finished": finished,
            "failed": failed,
        }


class SubLog(LiveLog):
    """Log of a sub-job: writes into the parent's file, without the markers."""

    def start(self, *header_lines: str):
        for line in header_lines:
            self.write(f"--- {line} ---")

    def finish(self, message: str = ""):
        if message:
            self.write(message)

    def fail(self, message: str = ""):
        if message:
            self.write(f"! {message}")
