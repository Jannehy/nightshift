"""Nightshift – Flask app: auth, routes, job streaming, config API.

Flow: no users.json  → /setup (wizard creates admin + config)
      not signed in  → /login
      signed in      → app; /settings and config/user APIs are admin-only
"""
from __future__ import annotations

import json
import os
import time
import queue
import socket
from pathlib import Path

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, session)

from . import (__version__, auth, beetsconf, cookies, navidrome, nightly,
               notify, scheduler, syncreg)
from .config import DEFAULTS, cfg
from .downloader import run_ytdlp_download
from .jobs import enqueue, jobs, new_job, queue_status
from .logs import (LiveLog, download_log_path, nightly_log_path,
                   remove_download_log)
from .search import bp as search_bp
from .spotify import run_spotify_download

SUPPORTED_URL_DOMAINS = ("spotify.com", "soundcloud.com", "youtube.com", "youtu.be")


def create_app() -> Flask:
    app = Flask(__name__, template_folder="../templates",
                static_folder="../static")
    app.secret_key = auth.session_secret()
    app.register_blueprint(search_bp)

    # ------------------------------------------------------------------
    # Access gate: setup wizard → login → app
    # ------------------------------------------------------------------
    OPEN_PATHS = ("/static/", "/health", "/api/version")
    SETUP_PATHS = ("/setup", "/api/setup")
    LOGIN_PATHS = ("/login", "/api/login")

    @app.before_request
    def _gate():
        path = request.path
        if path.startswith(OPEN_PATHS):
            return None
        configured = cfg.exists and auth.has_users()
        if not configured:
            if path.startswith(SETUP_PATHS):
                return None
            return redirect("/setup")
        if path.startswith(SETUP_PATHS):
            return redirect("/")  # setup is locked once completed
        if not auth.current_user():
            if path.startswith(LOGIN_PATHS):
                return None
            if path.startswith("/api/") or request.method != "GET":
                return jsonify({"error": "Not signed in"}), 401
            return redirect("/login")
        return None

    # ------------------------------------------------------------------
    # Health / Setup / Login
    # ------------------------------------------------------------------
    @app.route("/health")
    def health():
        return jsonify({"status": "ok",
                        "configured": cfg.exists and auth.has_users(),
                        "version": __version__})

    @app.route("/api/version")
    def api_version():
        """Open on purpose: clients check what they are talking to before they
        have a session, so they can warn about a server too old for them."""
        return jsonify({"name": "Nightshift", "version": __version__})

    @app.route("/setup")
    def setup():
        return render_template("setup.html",
                               docker=os.path.exists("/.dockerenv"))

    def _validate_music_root(updates: dict):
        """Reject library paths that do not exist in this environment.

        The most common Docker mistake is typing the HOST path here; the
        container only sees /music (wherever the volume points)."""
        root = (updates.get("library") or {}).get("music_root")
        if root is None:
            return None
        if os.path.isdir(root):
            return None
        msg = f"Music library path not found: {root}"
        if os.path.exists("/.dockerenv"):
            msg += (". In Docker, keep /music and point the volume in "
                    "docker-compose.yml at your library instead")
        return jsonify({"error": msg}), 400

    @app.route("/api/setup", methods=["POST"])
    def api_setup():
        """Initial setup: admin account + base config in one step."""
        if cfg.exists and auth.has_users():
            return jsonify({"error": "Already set up"}), 403
        data = request.json or {}
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        bad = _validate_music_root(data.get("config") or {})
        if bad:
            return bad
        ok, msg = auth.create_user(username, password, role="admin")
        if not ok:
            return jsonify({"error": msg}), 400
        cfg.save(data.get("config") or {})
        session["user"] = {"username": username, "role": "admin"}
        return jsonify({"ok": True})

    @app.route("/login")
    def login_page():
        if auth.current_user():
            return redirect("/")
        return render_template("login.html")

    @app.route("/api/login", methods=["POST"])
    def api_login():
        data = request.json or {}
        user = auth.verify(data.get("username"), data.get("password"))
        if not user:
            return jsonify({"error": "Wrong username or password"}), 401
        session["user"] = user
        session.permanent = True
        return jsonify({"ok": True, "user": user})

    @app.route("/api/logout", methods=["POST"])
    def api_logout():
        session.pop("user", None)
        return jsonify({"ok": True})

    @app.route("/api/me")
    @auth.login_required
    def api_me():
        return jsonify({"user": auth.current_user(),
                        "is_admin": auth.is_admin(),
                        "sync_enabled": syncreg.sync_enabled(),
                        "navidrome_enabled": navidrome.enabled(),
                        "nightly_schedule": cfg.nightly.schedule,
                        "language": cfg.server.language,
                        "theme": cfg.server.theme})

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------
    @app.route("/")
    @auth.login_required
    def index():
        return render_template("index.html", hostname=socket.gethostname())

    @app.route("/settings")
    @auth.login_required
    def settings():
        if not auth.is_admin():
            return redirect("/")
        return render_template("settings.html")

    @app.route("/sync")
    @auth.login_required
    def sync_page():
        return render_template("sync.html")

    # ------------------------------------------------------------------
    # Downloads
    # ------------------------------------------------------------------
    @app.route("/download", methods=["POST"])
    @auth.login_required
    def download():
        data = request.json or {}
        url = (data.get("url") or "").strip()
        owner_id = data.get("owner_id") or None
        sync = bool(data.get("sync")) and syncreg.sync_enabled()

        if not url or not any(d in url for d in SUPPORTED_URL_DOMAINS):
            return jsonify({"error":
                            "Not a valid Spotify, SoundCloud or YouTube URL"}), 400

        job_id, _ = new_job()
        target = (run_spotify_download if "spotify.com" in url
                  else run_ytdlp_download)
        source = ("Spotify" if "spotify.com" in url
                  else "SoundCloud" if "soundcloud.com" in url else "YouTube")
        requested_by = auth.current_user()["username"]
        # "Öffentlich" im Owner-Dropdown (kein owner_id) → für alle sichtbar
        sync_public = owner_id is None
        position = enqueue(job_id, target, url, owner_id, sync,
                           requested_by, sync_public, label=source)
        return jsonify({"job_id": job_id, "position": position})

    @app.route("/stream/<job_id>")
    @auth.login_required
    def stream(job_id):
        if job_id not in jobs:
            return "Job not found", 404

        def gen():
            q = jobs[job_id]
            while True:
                try:
                    event = q.get(timeout=300)
                    yield f"data: {json.dumps(event)}\n\n"
                    if event["type"] in ("done", "error"):
                        jobs.pop(job_id, None)
                        break
                except queue.Empty:
                    yield ": ping\n\n"

        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    @app.route("/download-log")
    @auth.login_required
    def download_log():
        # Everyone reads their own log: downloads run one at a time, but the
        # next person's run must not overwrite what this user sees.
        user = auth.current_user()["username"]
        state = LiveLog(download_log_path(user)).read_state()
        state["running"] = bool(state["log"]) and not (
            state["finished"] or state["failed"])
        return jsonify(state)

    # ------------------------------------------------------------------
    # Nightly
    # ------------------------------------------------------------------
    @app.route("/nightly", methods=["POST"])
    @auth.login_required
    def nightly_start():
        if nightly.is_running():
            return jsonify({"error": "Nightly sync is already running"}), 409
        job_id, q = new_job()

        def run(job_id_inner):
            qq = jobs[job_id_inner]

            def emit_line(line):
                qq.put({"type": "log", "line": line})

            ok = nightly.run_nightly(emit_fn=emit_line)
            if ok:
                qq.put({"type": "done",
                        "message": "Nightly sync completed!",
                        "progress": 100, "total_tracks": 0})
            else:
                qq.put({"type": "error", "message": "Nightly sync failed"})

        position = enqueue(job_id, run, label="Nightly")
        return jsonify({"job_id": job_id, "position": position})

    @app.route("/api/queue")
    @auth.login_required
    def api_queue():
        return jsonify(queue_status())

    @app.route("/nightly-status")
    @auth.login_required
    def nightly_status():
        return jsonify({"running": nightly.is_running()})

    @app.route("/nightly-log")
    @auth.login_required
    def nightly_log():
        state = LiveLog(nightly_log_path()).read_state()
        state["running"] = nightly.is_running()
        return jsonify(state)

    # ------------------------------------------------------------------
    # Navidrome + sync registry
    # ------------------------------------------------------------------
    @app.route("/nd-users")
    @auth.login_required
    def nd_users():
        if not navidrome.enabled():
            return jsonify({"users": [], "enabled": False})
        try:
            token = navidrome.login()
            return jsonify({"users": navidrome.list_users(token),
                            "enabled": True})
        except Exception as e:
            return jsonify({"users": [], "enabled": True, "error": str(e)})

    @app.route("/api/sync-playlists", methods=["GET"])
    @auth.login_required
    def sync_playlists():
        user = auth.current_user()
        return jsonify({"entries": syncreg.visible_items_for(
                            user["username"], auth.is_admin()),
                        "enabled": syncreg.sync_enabled()})

    @app.route("/api/sync-playlists", methods=["PATCH"])
    @auth.admin_required
    def sync_playlist_meta():
        data = request.json or {}
        url = (data.get("url") or "").strip()
        filename = (data.get("file") or "").strip()
        public = bool(data.get("public", True))
        ok = syncreg.set_meta(url, filename,
                              (data.get("owner") or "").strip() or None,
                              public)
        if not ok:
            return jsonify({"error": "Entry not found"}), 404
        # Mirror the visibility change to Navidrome, otherwise "public" would
        # only affect who sees the playlist inside Nightshift. Owner changes
        # stay local on purpose — reassigning an existing playlist to another
        # Navidrome user is not reliable through the internal API.
        name = syncreg.display_name_of(url, filename) or ""
        nd_ok, nd_msg = (True, "")
        if name:
            nd_ok, nd_msg = navidrome.set_visibility(
                name, public, syncreg.file_path_of(url, filename))
        return jsonify({"ok": True, "navidrome_ok": nd_ok,
                        "navidrome": nd_msg})

    @app.route("/api/sync-playlists", methods=["DELETE"])
    @auth.login_required
    def sync_playlist_remove():
        data = request.json or {}
        url = (data.get("url") or "").strip()
        filename = (data.get("file") or "").strip()
        if not auth.is_admin():
            owner = syncreg.owner_of(url, filename)
            if owner != auth.current_user()["username"]:
                return jsonify({"error": "Administrators only"}), 403
        removed = syncreg.remove_item(url, filename)
        return jsonify({"removed": removed})

    # ------------------------------------------------------------------
    # User management (admin only)
    # ------------------------------------------------------------------
    @app.route("/api/users", methods=["GET"])
    @auth.admin_required
    def users_list():
        return jsonify({"users": auth.list_users()})

    @app.route("/api/users", methods=["POST"])
    @auth.admin_required
    def users_create():
        data = request.json or {}
        ok, msg = auth.create_user(data.get("username"),
                                   data.get("password"),
                                   data.get("role", "user"))
        return (jsonify({"ok": True}) if ok
                else (jsonify({"error": msg}), 400))

    @app.route("/api/users", methods=["DELETE"])
    @auth.admin_required
    def users_delete():
        data = request.json or {}
        username = data.get("username") or ""
        ok, msg = auth.delete_user(username)
        if ok:
            remove_download_log(username)
        return (jsonify({"ok": True}) if ok
                else (jsonify({"error": msg}), 400))

    @app.route("/api/users/password", methods=["POST"])
    @auth.login_required
    def users_password():
        """Change own password; admins may change anyone's."""
        data = request.json or {}
        target = (data.get("username") or "").strip()
        me = auth.current_user()["username"]
        if target and target.lower() != me.lower() and not auth.is_admin():
            return jsonify({"error": "Administrators only"}), 403
        ok, msg = auth.change_password(target or me, data.get("password") or "")
        return (jsonify({"ok": True}) if ok
                else (jsonify({"error": msg}), 400))

    # ------------------------------------------------------------------
    # Config API (admin only)
    # ------------------------------------------------------------------
    SECRET_KEYS = {("navidrome", "password"), ("notifications", "ntfy_token")}

    @app.route("/api/config", methods=["GET"])
    @auth.admin_required
    def config_get():
        data = {}
        for section, values in cfg._data.items():
            data[section] = {}
            for key, val in values.items():
                if (section, key) in SECRET_KEYS and val:
                    data[section][key] = "__SET__"  # never ship secrets to the client
                else:
                    data[section][key] = val
        data["_env"] = {"docker": os.path.exists("/.dockerenv")}
        return jsonify(data)

    # Cookie files are the one piece of configuration nobody can type: they are
    # exported from a browser and have to land in the right place, with the
    # right name and readable by the user the container runs as. Uploading them
    # here saves a trip through the file system.
    COOKIE_TARGETS = {
        "youtube": ("downloads", "youtube_cookie_file", "yt-cookies.txt",
                    "youtube.com"),
        "soundcloud": ("downloads", "soundcloud_cookie_file", "sc-cookies.txt",
                       "soundcloud.com"),
    }

    @app.route("/api/cookies/status")
    @auth.login_required
    def cookies_status():
        """How long the cookie files are still good for.

        Everyone may ask: a warning about expiring cookies is of no use if
        only an admin ever sees it, and the answer holds no secrets - a path,
        a date and a count.
        """
        return jsonify({"cookies": cookies.all_status()})

    @app.route("/api/cookies/<kind>", methods=["POST"])
    @auth.admin_required
    def cookies_upload(kind):
        target = COOKIE_TARGETS.get(kind)
        if target is None:
            return jsonify({"error": f"Unknown cookie type: {kind}"}), 400
        section, key, filename, erwartet = target

        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return jsonify({"error": "No file received"}), 400

        raw = upload.read(2 * 1024 * 1024)          # a cookie file is a few KB
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return jsonify({"error": "Not a text file"}), 400

        lines = [l for l in text.splitlines() if l.strip() and not l.startswith("#")]
        # Netscape format: seven tab-separated fields per line. Checking this
        # here turns "downloads still fail" into "wrong file, try again".
        if not lines or not all(len(l.split("\t")) == 7 for l in lines):
            return jsonify({"error": "Not a Netscape cookie file"}), 400

        directory = Path(cfg.path(f"{section}.{key}") or "").parent
        if not directory or str(directory) in (".", "/"):
            directory = Path("/config/cookies")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename

        if path.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path.replace(path.with_name(f"{filename}.{stamp}.bak"))

        path.write_text(text, encoding="utf-8")
        path.chmod(0o644)
        # Same owner as everything else under /config, so the download tools
        # can read it whichever user the container was told to run as.
        try:
            reference = path.parent.stat()
            os.chown(path, reference.st_uid, reference.st_gid)
        except OSError:
            pass

        if cfg.path(f"{section}.{key}") != str(path):
            cfg.save({section: {key: str(path)}})

        # Check the new file straight away: whether cookies actually sign in
        # is the only thing the user wants to know at this moment.
        signed_in = cookies.refresh(kind).get(kind, {}).get("signed_in")

        hosts = sorted({l.split("\t")[0].lstrip(".") for l in lines})
        # Saved either way - the file is valid, it just may be the wrong one.
        # Exporting the cookies of the site one happens to have open is the
        # easiest mistake to make here, and the hardest to notice later.
        passt = any(erwartet in host for host in hosts)
        return jsonify({"ok": True, "path": str(path), "cookies": len(lines),
                        "hosts": hosts[:4], "signed_in": signed_in,
                        "warning": None if passt else erwartet})

    @app.route("/api/config/reset", methods=["POST"])
    @auth.admin_required
    def config_reset():
        """Reset all settings to the built-in defaults."""
        import copy
        cfg.save(copy.deepcopy(DEFAULTS))
        try:
            scheduler.reschedule()
        except Exception:
            pass
        return jsonify({"ok": True})

    @app.route("/api/config", methods=["POST"])
    @auth.admin_required
    def config_post():
        updates = request.json or {}
        # never persist client-side metadata sections
        updates = {k: v for k, v in updates.items() if not k.startswith("_")}
        bad = _validate_music_root(updates)
        if bad:
            return bad
        for section, key in SECRET_KEYS:
            if updates.get(section, {}).get(key) == "__SET__":
                del updates[section][key]
        cfg.save(updates)
        try:
            scheduler.reschedule()
        except Exception:
            pass
        return jsonify({"ok": True})

    return app


def main():
    app = create_app()
    if cfg.beets.enabled:
        beetsconf.ensure()
    if cfg.exists:
        scheduler.start()
    from waitress import serve
    print(f"Nightshift listening on http://{cfg.server.host}:{cfg.server.port}")
    serve(app, host=cfg.server.host, port=int(cfg.server.port),
          threads=16, channel_timeout=300)


if __name__ == "__main__":
    main()
