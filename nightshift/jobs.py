"""Shared job registry (SSE streaming) + serial download queue.

All downloads and nightly runs go through ONE worker thread. This prevents:
- interleaved live logs (single log file per pipeline)
- misattributed playlists (newest-m3u8 lookup racing between jobs)
- concurrent beets imports on the same SQLite database
- parallel spotDL/yt-dlp runs amplifying rate limits
"""
from __future__ import annotations

import queue
import threading
import uuid

jobs: dict[str, "queue.Queue"] = {}

_task_q: "queue.Queue" = queue.Queue()
_pending: list[dict] = []
_current: dict | None = None
_state_lock = threading.Lock()
_worker_started = False
_worker_lock = threading.Lock()


def new_job() -> tuple[str, "queue.Queue"]:
    job_id = uuid.uuid4().hex
    q = queue.Queue()
    jobs[job_id] = q
    return job_id, q


def emit(q, type_: str, **data):
    q.put({"type": type_, **data})


def enqueue(job_id: str | None, target, *args, label: str = "",
            kwargs: dict | None = None) -> int:
    """Add a task to the serial queue. Returns the queue position
    (0 = will start immediately, 1 = one task ahead, ...)."""
    task = {"job_id": job_id, "target": target, "args": args,
            "kwargs": kwargs or {}, "label": label}
    with _state_lock:
        position = len(_pending) + (1 if _current else 0)
        _pending.append(task)
    _task_q.put(task)
    _ensure_worker()
    return position


def queue_status() -> dict:
    with _state_lock:
        return {
            "running": ({"job_id": _current["job_id"],
                         "label": _current["label"]} if _current else None),
            "pending": [{"job_id": t["job_id"], "label": t["label"]}
                        for t in _pending],
        }


def _ensure_worker():
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(target=_worker, daemon=True).start()
        _worker_started = True


def _worker():
    global _current
    while True:
        task = _task_q.get()
        with _state_lock:
            if task in _pending:
                _pending.remove(task)
            _current = task
        try:
            task["target"](task["job_id"], *task["args"], **task["kwargs"])
        except Exception:
            pass
        finally:
            with _state_lock:
                _current = None


def start_thread(target, *args) -> None:
    """Run outside the queue (used for non-download background work)."""
    threading.Thread(target=target, args=args, daemon=True).start()
