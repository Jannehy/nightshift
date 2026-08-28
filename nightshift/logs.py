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


# Same rule the web interface and both apps use, so a run does not describe
# itself differently depending on where it is watched. A track the catalogue
# does not have is an outcome, not a failure.
_MISSING = re.compile(r"no results found|lookuperror|could not be downloaded",
                      re.IGNORECASE)
_PROBLEM = re.compile(r"✗|⚠|error|failed", re.IGNORECASE)


# Output spotDL passes through from a failed child process: a framed Python
# traceback, prefixed with "|" and drawn with box characters. Twelve lines of
# it say no more than the one line naming the exception.
_PASSTHROUGH = re.compile(r"^\||[│╰╭┌└─❱]")
# An attempt that failed and was retried is not an outcome. The verdict comes
# later as "✓ <name>: n new tracks" or "✗ <name> sync failed", and listing the
# intermediate states would report a run as broken that repaired itself.
_ATTEMPT = re.compile(r"attempt \d+ (failed|/)", re.IGNORECASE)
# The closing statistics: "Playlists failed:     0" is a count, not a failure,
# and reading it as one turned a clean run into a run with one error.
_COUNT = re.compile(r"^[^:]{1,40}:\s*\d+\s*$")


def problem_kind(line: str) -> str | None:
    """"error", "missing" or None."""
    stripped = line.strip()
    # Skip what a summary is made of, or summarising would feed on itself:
    # the headings above a list and the bullet points under it.
    if stripped.startswith("•") or stripped.endswith(":"):
        return None
    # The timestamp prefix has to come off before either pattern can match.
    body = re.sub(r"^\[[^\]]+\]\s*", "", stripped)
    if _PASSTHROUGH.search(body) or _ATTEMPT.search(body) or _COUNT.match(body):
        return None
    if line.startswith("=== DONE") or line.startswith("✓"):
        return None
    if _MISSING.search(line):
        return "missing"
    if line.startswith("=== FAILED") or _PROBLEM.search(line):
        return "error"
    return None


class LiveLog:
    """Live log file with start/end markers (enables UI restore after reload)."""

    def __init__(self, path: Path):
        self.path = path
        # Collected as they go by, so the end of a long run can repeat them
        # together. Hunting for three red lines in six hundred is the kind of
        # thing a log should do for you.
        self.problems: list[str] = []
        self.missing: list[str] = []
        # finish() and fail() summarise as well, so a caller that wants the
        # block in front of a live viewer can ask for it without printing it
        # twice.
        self._summarised = False

    def start(self, *header_lines: str):
        with open(self.path, "w") as f:
            f.write(f"=== Start: {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            for line in header_lines:
                f.write(f"=== {line} ===\n")

    def write(self, line: str):
        kind = problem_kind(line)
        if kind == "error" and line not in self.problems:
            self.problems.append(line)
        elif kind == "missing" and line not in self.missing:
            self.missing.append(line)
        try:
            with open(self.path, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def summarise(self, emit_fn=None) -> None:
        """Repeats everything that went wrong, in one block, at the end.

        On a playlist with two hundred tracks the failures are scattered over
        hundreds of lines; by the time the run is done nobody scrolls back for
        them.
        """
        if self._summarised:
            return
        self._summarised = True

        def say(text: str) -> None:
            self.write(text)
            if emit_fn:
                emit_fn(text)

        if self.problems:
            say("")
            say(f"⚠ Errors in this run ({len(self.problems)}):")
            for entry in self.problems:
                say(f"  • {entry.strip()}")
        if self.missing:
            say("")
            say(f"Not found ({len(self.missing)}):")
            for entry in self.missing:
                say(f"  • {entry.strip()}")

    def finish(self, message: str = ""):
        self.summarise()
        self.write(f"=== DONE: {message} ===")

    def fail(self, message: str = ""):
        self.summarise()
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
