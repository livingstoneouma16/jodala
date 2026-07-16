#!/bin/sh
# Runs as root at container start (see Dockerfile: no USER before this),
# fixes ownership of the mounted data directory, then drops to the
# unprivileged `appuser` for the actual process.
#
# Why this is needed: hosting platforms that attach a persistent volume at
# deploy time (Render Disks, Railway Volumes, Fly.io Volumes) mount it
# freshly owned by root, *after* the image's build-time `chown` already
# ran -- so without this step, appuser would get "Permission denied"
# trying to create the SQLite file the first time the container starts on
# any of those platforms.
set -e

DATA_DIR="$(dirname "${DB_PATH:-/data/sacco.db}")"
mkdir -p "$DATA_DIR"
chown -R appuser:appuser "$DATA_DIR"

exec gosu appuser "$@"
