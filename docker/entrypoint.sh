#!/bin/sh
# Restore any pending data bundle, then start the server.
#
# Data reaches a hosted instance by being uploaded onto the volume (on Railway,
# `railway volume files upload`). Something then has to unpack it, and that has
# to happen *here* -- before uvicorn starts -- because the app holds
# catalog.sqlite open in WAL mode and warehouse.duckdb open for the whole
# process lifetime. Replacing those files under a live process risks corrupting
# them. Before the app opens anything, it is just files.
#
# Drop a .tgz in $DATAQ_DATA_DIR/_inbox and restart the service.
#
# A bundle whose name contains ".replace." wipes the data dir first -- that is
# the reset. Anything else is unpacked over what is already there. Putting the
# intent in the filename means it travels with the file, so a reset cannot be
# caused (or missed) by a stale environment variable set weeks earlier.
# DATAQ_RESTORE_MODE overrides, for the case where you cannot rename the file.
set -eu

DATA_DIR="${DATAQ_DATA_DIR:-/data}"
INBOX="$DATA_DIR/_inbox"

mkdir -p "$INBOX"

for bundle in "$INBOX"/*.tgz "$INBOX"/*.tar.gz; do
    # The glob is literal when nothing matches, so check the file exists.
    [ -e "$bundle" ] || continue

    name="$(basename "$bundle")"
    mode="${DATAQ_RESTORE_MODE:-merge}"
    case "$name" in *.replace.*) mode="replace" ;; esac
    echo "entrypoint: restoring $name (mode=$mode)"

    if [ "$mode" = "replace" ]; then
        echo "entrypoint: clearing $DATA_DIR first"
        # Everything except the inbox itself, which holds the bundle we are
        # about to read.
        find "$DATA_DIR" -mindepth 1 -maxdepth 1 ! -name '_inbox' -exec rm -rf {} +
    fi

    tar xzf "$bundle" -C "$DATA_DIR"

    # Rename rather than delete: a restart must not redo the restore, and
    # keeping the file means a bad bundle can still be inspected.
    mv "$bundle" "$bundle.applied-$(date +%Y%m%dT%H%M%S)"
    echo "entrypoint: restore complete"
done

# Railway (and most platforms) inject PORT. The fallback keeps `docker run`
# working locally. Shell form is required here -- an exec-form CMD would pass
# the string "${PORT}" through literally without expanding it.
exec uvicorn dataq.api.app:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --app-dir backend/src
