#!/bin/sh
set -e

PUID=${PUID:-0}
PGID=${PGID:-0}
UMASK=${UMASK:-002}

umask "$UMASK"

if [ "$PUID" != "0" ] && [ "$PGID" != "0" ]; then
    echo "Starting with PUID=$PUID, PGID=$PGID, UMASK=$UMASK"

    if ! getent group "$PGID" > /dev/null 2>&1; then
        addgroup --gid "$PGID" appgroup
    fi

    if ! getent passwd "$PUID" > /dev/null 2>&1; then
        adduser --uid "$PUID" --gid "$PGID" --shell /bin/sh --disabled-password --gecos "" appuser
    fi

    chown -R "$PUID:$PGID" /config

    for _dir in "$DOWNLOAD_PATH" "$LIDARR_PATH"; do
        if [ -n "$_dir" ] && [ -d "$_dir" ]; then
            chown "$PUID:$PGID" "$_dir" 2>/dev/null \
                || echo "WARNING: Cannot fix ownership of $_dir — ensure it is writable by uid=$PUID"
        fi
    done

    exec gosu "$PUID:$PGID" python app.py
else
    echo "Starting as root (PUID/PGID not set), UMASK=$UMASK"
    exec python app.py
fi
