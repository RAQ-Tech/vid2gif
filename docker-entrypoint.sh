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

# Ensure ownership of library and state directories
for dir in /library /state; do
    if [ -d "$dir" ]; then
        chown -R app:app "$dir"
    fi
done

python -m pip install --no-cache-dir -r /srv/requirements.txt

exec gosu app "$@"
