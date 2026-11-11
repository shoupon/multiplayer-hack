#!/bin/sh
# Restore the database from GCS, then run FCL under Litestream so every commit replicates.
set -eu
: "${FCL_LITESTREAM_BUCKET:?FCL_LITESTREAM_BUCKET is required}"
export FCL_PORT="${PORT:-${FCL_PORT:-8080}}"
litestream restore -config /etc/litestream.yml -if-db-not-exists -if-replica-exists "$FCL_DB_PATH"
exec litestream replicate -config /etc/litestream.yml -exec "python -m fcl serve"
