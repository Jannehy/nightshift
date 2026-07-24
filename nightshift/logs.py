"""Central log handling – all paths come from the config."""
from __future__ import annotations

import os
import time
from pathlib import Path

from .config import cfg


def _log_dir() -> Path:
    d = Path(cfg.logging.dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def download_log_path() -> Path:
    return _log_dir() / "download-live.log"


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
