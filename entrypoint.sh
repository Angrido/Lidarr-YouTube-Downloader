#!/bin/sh
set -e

PUID=${PUID:-0}
PGID=${PGID:-0}
UMASK=${UMASK:-002}

# Apply umask for file creation
umask "$UMASK"

if [ "$PUID" != "0" ] && [ "$PGID" != "0" ]; then
    echo "Starting with PUID=$PUID, PGID=$PGID, UMASK=$UMASK"

    # Create group if GID is not already in use
    if ! getent group "$PGID" > /dev/null 2>&1; then
        addgroup --gid "$PGID" appgroup
    fi

    # Create user if UID is not already in use
    if ! getent passwd "$PUID" > /dev/null 2>&1; then
        adduser --uid "$PUID" --gid "$PGID" --shell /bin/sh --disabled-password --gecos "" appuser
    fi

    # Ensure /config is owned by the app user
    chown -R "$PUID:$PGID" /config

    # Run as the app user
    exec gosu "$PUID:$PGID" python app.py
else
    echo "Starting as root (PUID/PGID not set), UMASK=$UMASK"
    exec python app.py
fi
