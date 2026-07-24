"""User management: login, roles (admin/user), session secret.

Users live in /config/users.json (passwords stored as scrypt hashes via
werkzeug). The first account is created by the setup wizard and is an admin.
"""
from __future__ import annotations

import json
import os
import secrets
from functools import wraps
from pathlib import Path

from flask import jsonify, redirect, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from .config import cfg


def _users_path() -> Path:
    p = Path(os.environ.get("NIGHTSHIFT_CONFIG", "/config/config.yaml")).parent / "users.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _secret_path() -> Path:
    return _users_path().parent / ".session-secret"


def session_secret() -> str:
    """Persistent Flask session key – survives container restarts."""
    p = _secret_path()
    if p.exists():
        return p.read_text().strip()
    secret = secrets.token_hex(32)
    p.write_text(secret)
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return secret


def _load() -> list[dict]:
    try:
        with open(_users_path()) as f:
            return json.load(f)
    except Exception:
        return []


def _save(users: list[dict]):
    path = _users_path()
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def has_users() -> bool:
    return bool(_load())


def list_users() -> list[dict]:
    """Without password hashes – for the management UI."""
    return [{"username": u["username"], "role": u.get("role", "user")}
            for u in _load()]


def create_user(username: str, password: str, role: str = "user") -> tuple[bool, str]:
    username = (username or "").strip()
    if not username or not password:
        return False, "Username and password required"
    if len(password) < 4:
        return False, "Password too short (min. 4 characters)"
    if role not in ("admin", "user"):
        role = "user"
    users = _load()
    if any(u["username"].lower() == username.lower() for u in users):
        return False, "Username already exists"
    users.append({
        "username": username,
        "password_hash": generate_password_hash(password),
        "role": role,
    })
    _save(users)
    return True, "User created"


def delete_user(username: str) -> tuple[bool, str]:
    users = _load()
    target = next((u for u in users
                   if u["username"].lower() == username.lower()), None)
    if not target:
        return False, "User not found"
    if target.get("role") == "admin":
        admins = [u for u in users if u.get("role") == "admin"]
        if len(admins) <= 1:
            return False, "The last admin cannot be deleted"
    _save([u for u in users if u is not target])
    return True, "User deleted"


def change_password(username: str, new_password: str) -> tuple[bool, str]:
    if len(new_password or "") < 4:
        return False, "Password too short (min. 4 characters)"
    users = _load()
    for u in users:
        if u["username"].lower() == username.lower():
            u["password_hash"] = generate_password_hash(new_password)
            _save(users)
            return True, "Password changed"
    return False, "User not found"


def verify(username: str, password: str) -> dict | None:
    for u in _load():
        if (u["username"].lower() == (username or "").lower()
                and check_password_hash(u["password_hash"], password or "")):
            return {"username": u["username"], "role": u.get("role", "user")}
    return None


# ------------------------------------------------------------------
# Session helpers and decorators
# ------------------------------------------------------------------

def current_user() -> dict | None:
    if "user" in session:
        return session["user"]
    return None


def is_admin() -> bool:
    u = current_user()
    return bool(u and u.get("role") == "admin")


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            if request.path.startswith("/api/") or request.method != "GET":
                return jsonify({"error": "Not signed in"}), 401
            return redirect("/login")
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            if request.path.startswith("/api/") or request.method != "GET":
                return jsonify({"error": "Not signed in"}), 401
            return redirect("/login")
        if not is_admin():
            return jsonify({"error": "Administrators only"}), 403
        return fn(*args, **kwargs)
    return wrapper
