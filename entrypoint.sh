#!/bin/sh
set -e

PUID=${PUID:-0}
PGID=${PGID:-0}
UMASK=${UMASK:-002}

# Apply umask for file creation
umask "$UMASK"

if [ "$PUID" != "0" ] && [ "$PGID" != "0" ]; then
    echo "Starting with PUID=$PUID, PGID=$PGID, UMASK=$UMASK"

    # Create group if it doesn't exist
    if ! getent group appgroup > /dev/null 2>&1; then
        addgroup -g "$PGID" appgroup
    fi

    # Create user if it doesn't exist
    if ! getent passwd appuser > /dev/null 2>&1; then
        adduser -u "$PUID" -G appgroup -s /bin/sh -D appuser
    fi

    # Ensure /config is owned by the app user
    chown -R appuser:appgroup /config

    # Run as the app user
    exec gosu appuser:appgroup python app.py
else
    echo "Starting as root (PUID/PGID not set), UMASK=$UMASK"
    exec python app.py
fi
