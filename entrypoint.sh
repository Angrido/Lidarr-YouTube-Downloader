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

    # Fix ownership/perms on the music roots. We chown only the mount
    # roots and their direct subdirs (artist folders) so a multi-TB
    # library doesn't stall the container on startup. Files inside
    # albums are left alone — they keep whatever owner Lidarr/the host
    # gave them. (Fixes #66: pre-existing artist folder owned by old
    # PUID rejected writes when adding a new album.)
    for _dir in "$DOWNLOAD_PATH" "$LIDARR_PATH"; do
        if [ -n "$_dir" ] && [ -d "$_dir" ]; then
            chown "$PUID:$PGID" "$_dir" 2>/dev/null \
                || echo "WARNING: Cannot fix ownership of $_dir — ensure it is writable by uid=$PUID"
            # Match the configured UMASK on the root and artist folders
            # so the running user has group-write into existing dirs.
            chmod g+rwx "$_dir" 2>/dev/null || true
            find "$_dir" -mindepth 1 -maxdepth 1 -type d \
                \( ! -uid "$PUID" -o ! -gid "$PGID" \) \
                -exec chown "$PUID:$PGID" {} + 2>/dev/null || true
            find "$_dir" -mindepth 1 -maxdepth 1 -type d \
                -exec chmod g+rwx {} + 2>/dev/null || true
        fi
    done

    exec gosu "$PUID:$PGID" python app.py
else
    echo "Starting as root (PUID/PGID not set), UMASK=$UMASK"
    exec python app.py
fi
