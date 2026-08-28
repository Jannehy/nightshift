"""Push messages over ntfy - opt-in, and quiet when it is not configured.

Nightshift runs unattended, so the things it notices at three in the morning
have to reach someone. ntfy is a self-hostable topic service: one URL, no
account, and nothing leaves the machine unless a URL is set.
"""
from __future__ import annotations

import urllib.request

from .config import cfg


def enabled() -> bool:
    return bool(getattr(cfg.notifications, "ntfy_url", "") or "")


def send(title: str, message: str, priority: str = "default",
         tags: str = "") -> bool:
    """Sends one message. Never raises - a failed notification is not a reason
    to fail the job it was reporting on."""
    url = (getattr(cfg.notifications, "ntfy_url", "") or "").strip()
    if not url:
        return False
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    token = (getattr(cfg.notifications, "ntfy_token", "") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        request = urllib.request.Request(
            url, data=message.encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=10):
            return True
    except Exception:
        return False
