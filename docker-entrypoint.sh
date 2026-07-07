#!/bin/sh
set -e

PUID="${PUID:-99}"
PGID="${PGID:-100}"

# Update group ID if it doesn't match
if [ "$(getent group app | cut -d: -f3)" != "$PGID" ]; then
    groupmod -o -g "$PGID" app
fi

# Update user ID if it doesn't match
if [ "$(id -u app)" != "$PUID" ]; then
    usermod -o -u "$PUID" -g "$PGID" app
fi

# Ensure state remains writable without scanning large media libraries.
if [ -d /state ]; then
    chown -R app:app /state
fi

if [ "${CHOWN_LIBRARY:-0}" = "1" ] && [ -d /library ]; then
    chown -R app:app /library
fi

exec gosu app "$@"
