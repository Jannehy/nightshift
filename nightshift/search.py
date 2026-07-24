"""Music search via the iTunes Search API (no auth) + download hookup."""
from __future__ import annotations

import queue
import threading

import requests
from flask import Blueprint, jsonify, render_template, request

from .config import cfg
from .jobs import enqueue, jobs, new_job
from .spotify import run_spotify_download

bp = Blueprint("search", __name__)


@bp.route("/search")
def search_page():
    return render_template("search.html")


@bp.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    entity = request.args.get("entity", "song")  # song | album | musicArtist
    if not q:
        return jsonify({"results": []})
    if entity not in {"song", "album", "musicArtist"}:
        entity = "song"

    try:
        r = requests.get(
            "https://itunes.apple.com/search",
            params={
                "term": q,
                "entity": entity,
                "limit": 24,
                "country": cfg.server.language if cfg.server.language in ("de",) else "us",
                "media": "music",
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        return jsonify({"error": f"iTunes API error: {e}"}), 502

    results = []
    for t in data.get("results", []):
        artwork_100 = t.get("artworkUrl100") or ""
        artwork = artwork_100.replace("100x100bb", "600x600bb").replace("100x100", "600x600")

        if entity == "song":
            artist = t.get("artistName") or ""
            title = t.get("trackName") or ""
            album = t.get("collectionName") or ""
            query = f"{artist} - {title}".strip(" -")
            results.append({
                "kind": "track",
                "id": t.get("trackId"),
                "title": title,
                "artist": artist,
                "album": album,
                "artwork": artwork,
                "preview": t.get("previewUrl"),
                "duration_ms": t.get("trackTimeMillis"),
                "release_date": t.get("releaseDate"),
                "query": query,
            })
        elif entity == "album":
            artist = t.get("artistName") or ""
            album = t.get("collectionName") or ""
            query = f"{artist} - {album}".strip(" -")
            results.append({
                "kind": "album",
                "id": t.get("collectionId"),
                "title": album,
                "artist": artist,
                "album": album,
                "artwork": artwork,
                "track_count": t.get("trackCount"),
                "release_date": t.get("releaseDate"),
                "query": query,
            })
        elif entity == "musicArtist":
            artist = t.get("artistName") or ""
            results.append({
                "kind": "artist",
                "id": t.get("artistId"),
                "title": artist,
                "artist": artist,
                "artwork": "",
                "genre": t.get("primaryGenreName"),
                "query": artist,
            })

    return jsonify({"results": results})


@bp.route("/api/download-from-query", methods=["POST"])
def download_from_query():
    payload = request.get_json(silent=True) or {}
    query = (payload.get("query") or "").strip()
    if not query:
        return jsonify({"error": "missing query"}), 400

    job_id, _ = new_job()
    position = enqueue(job_id, run_spotify_download, query, label="Spotify")
    return jsonify({"job_id": job_id, "query": query, "position": position})


@bp.route("/api/download-album", methods=["POST"])
def download_album():
    """Whole album: track list via iTunes lookup, sequential downloads."""
    payload = request.get_json(silent=True) or {}
    album_id = payload.get("itunes_album_id")
    if not album_id:
        return jsonify({"error": "missing itunes_album_id"}), 400

    try:
        r = requests.get(
            "https://itunes.apple.com/lookup",
            params={"id": album_id, "entity": "song", "limit": 200},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        return jsonify({"error": f"iTunes lookup error: {e}"}), 502

    tracks = [
        item for item in data.get("results", [])
        if item.get("wrapperType") == "track" and item.get("kind") == "song"
    ]
    queries = []
    for t in tracks:
        artist = (t.get("artistName") or "").strip()
        title = (t.get("trackName") or "").strip()
        if artist and title:
            queries.append(f"{artist} - {title}")

    if not queries:
        return jsonify({"error": "No tracks found in album"}), 404

    job_id, _ = new_job()
    position = enqueue(job_id, _run_album_download, queries, label="Album")
    return jsonify({"job_id": job_id, "track_count": len(queries),
                    "position": position})


def _run_album_download(album_job_id: str, queries: list[str]):
    """Sequential single-track downloads, streams merged into the album job."""
    main_q = jobs[album_job_id]

    def emit(type_, **data):
        main_q.put({"type": type_, **data})

    total = len(queries)
    emit("status", message=f"Album download: {total} tracks (sequential)", progress=0)

    for i, query in enumerate(queries, 1):
        emit("status",
             message=f"━━━ Track {i}/{total}: {query} ━━━",
             progress=int((i - 1) / total * 100))

        sub_id = f"{album_job_id}-{i}"
        jobs[sub_id] = queue.Queue()
        sub_q = jobs[sub_id]

        sub_thread = threading.Thread(
            target=run_spotify_download, args=(sub_id, query), daemon=True,
        )
        sub_thread.start()

        while sub_thread.is_alive() or not sub_q.empty():
            try:
                main_q.put(sub_q.get(timeout=0.5))
            except queue.Empty:
                pass

        sub_thread.join(timeout=2)
        jobs.pop(sub_id, None)

    emit("status", message=f"Album download finished ({total} tracks)", progress=100)
    emit("done", message="Album complete")
