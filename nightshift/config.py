"""Nightshift – central config loader.

Load order (later sources override earlier ones):
  1. Built-in defaults (below)
  2. config.yaml (path via NIGHTSHIFT_CONFIG, default: /config/config.yaml)
  3. Environment variables: NIGHTSHIFT_<SECTION>_<KEY>  (e.g. NIGHTSHIFT_SERVER_PORT=9000)

Usage:
    from nightshift.config import cfg
    cfg.server.port          -> 8765
    cfg.library.music_root   -> "/music"
    cfg.path("library.music_root")  -> same value, resolved dynamically
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

try:
    import yaml
except ImportError as e:  # pragma: no cover
    raise SystemExit("PyYAML missing: pip install pyyaml") from e


DEFAULTS: dict = {
    "server": {
        "host": "0.0.0.0",
        "port": 8765,
        "language": "de",
        "theme": "auto",
    },
    "library": {
        "music_root": "/music",
        "spotify_dir": "playlists",
        "spotify_output_template": "playlists/{album-artist}/{album}/{track-number} - {title}.{output-ext}",
        "soundcloud_dir": "SoundCloud",
        "youtube_dir": "YouTube",
    },
    "downloads": {
        "youtube_cookie_file": "",
        "soundcloud_cookie_file": "",
        "spotify_format": "mp3",
        "spotify_bitrate": "320k",
        "spotify_threads": 8,
        "max_attempts": 5,
        "retry_wait_seconds": 3,
    },
    "nightly": {
        "schedule": "0 23 * * *",
        "spotdl_sync_dir": "/config/spotdl-sync",
        "sync_timeout_seconds": 900,
        "max_attempts": 10,
        "fetch_lyrics": True,
        # spotDL's sync deletes local files that left the playlist. For a
        # library manager keeping them is the safer default — rotating
        # playlists ("Discover Weekly") would otherwise erase music the
        # user never chose to remove.
        "keep_removed_tracks": True,
    },
    "sync": {
        "enabled": True,
        "registry_file": "/config/sync-registry.json",
    },
    "beets": {
        "enabled": True,
        "config_file": "",
    },
    "navidrome": {
        "enabled": False,
        "url": "http://localhost:4533",
        "username": "",
        "password": "",
        "default_public": True,
        "import_retries": 20,
        "import_retry_delay": 3,
    },
    "notifications": {
        # A full ntfy topic URL, e.g. https://ntfy.sh/my-nightshift. Empty
        # disables sending entirely; nothing is contacted unless it is set.
        "ntfy_url": "",
        # Optional bearer token for a protected topic.
        "ntfy_token": "",
        # How many days before a cookie file expires the warning starts.
        "cookie_warn_days": 14,
        "notify_cookies": True,
    },
    "logging": {
        "dir": "/config/logs",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _apply_env(config: dict) -> dict:
    """NIGHTSHIFT_<SECTION>_<KEY> overrides config[section][key]."""
    prefix = "NIGHTSHIFT_"
    for env_key, raw in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        rest = env_key[len(prefix):].lower()
        # section = first segment, key = the rest (keys may contain underscores)
        for section in config:
            if rest.startswith(section + "_"):
                key = rest[len(section) + 1:]
                if key in config[section]:
                    current = config[section][key]
                    if isinstance(current, bool):
                        config[section][key] = raw.lower() in ("1", "true", "yes", "on")
                    elif isinstance(current, int):
                        try:
                            config[section][key] = int(raw)
                        except ValueError:
                            pass
                    else:
                        config[section][key] = raw
                break
    return config


def _to_namespace(d: dict):
    if isinstance(d, dict):
        return SimpleNamespace(**{k: _to_namespace(v) for k, v in d.items()})
    return d


class Config:
    def __init__(self):
        self._data: dict = {}
        self._ns = None
        self.config_path: Path | None = None
        self.reload()

    def reload(self):
        data = DEFAULTS
        path = Path(os.environ.get("NIGHTSHIFT_CONFIG", "/config/config.yaml"))
        if path.is_file():
            with open(path) as f:
                file_cfg = yaml.safe_load(f) or {}
            data = _deep_merge(data, file_cfg)
            self.config_path = path
        data = _apply_env(data)
        self._data = data
        self._ns = _to_namespace(data)

    @property
    def exists(self) -> bool:
        """True if a config.yaml was found (otherwise: show the setup wizard)."""
        return self.config_path is not None

    def path(self, dotted: str, default=None):
        node = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def save(self, updates: dict):
        """Merges updates into config.yaml (used by the settings page)."""
        target = self.config_path or Path(
            os.environ.get("NIGHTSHIFT_CONFIG", "/config/config.yaml"))
        current = {}
        if target.is_file():
            with open(target) as f:
                current = yaml.safe_load(f) or {}
        merged = _deep_merge(current, updates)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        with open(tmp, "w") as f:
            yaml.safe_dump(merged, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, target)
        self.reload()

    # Attribute access: cfg.server.port
    def __getattr__(self, item):
        if self._ns and hasattr(self._ns, item):
            return getattr(self._ns, item)
        raise AttributeError(item)

    # --- Derived paths (use these everywhere instead of hardcoding) ---
    @property
    def soundcloud_path(self) -> str:
        return str(Path(self.library.music_root) / self.library.soundcloud_dir)

    @property
    def youtube_path(self) -> str:
        return str(Path(self.library.music_root) / self.library.youtube_dir)

    @property
    def spotify_path(self) -> str:
        return str(Path(self.library.music_root) / self.library.spotify_dir)


cfg = Config()
