#!/bin/sh
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# Ensure directories exist
mkdir -p /config/logs /config/spotdl-sync /config/cookies /config/beets

if [ "$(id -u)" = "0" ] && [ "$PUID" != "0" ]; then
    # Non-root operation: user with the requested UID/GID (linuxserver.io pattern)
    if ! getent group nightshift >/dev/null 2>&1; then
        addgroup --gid "$PGID" nightshift 2>/dev/null || true
    fi
    if ! getent passwd nightshift >/dev/null 2>&1; then
        adduser --uid "$PUID" --gid "$PGID" --disabled-password \
                --gecos "" nightshift 2>/dev/null || true
    fi
    chown -R "$PUID:$PGID" /config
    # Deliberately NOT chowning /music recursively (it can be huge) –
    # the host folder must be writable for PUID/PGID (see README)
    exec gosu nightshift python -m nightshift
fi

exec python -m nightshift
