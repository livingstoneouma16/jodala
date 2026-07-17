#!/bin/sh
# Runs as root at container start (see Dockerfile: no USER before this),
# then drops to the unprivileged `appuser` for the actual process.
#
# There's no local data directory to prepare anymore -- the database is
# PostgreSQL, reached over the network via DATABASE_URL (set by Render,
# Railway, Fly.io, or docker-compose's postgres service), so there's
# nothing left to chown the way the old SQLite file volume needed.
set -e

exec gosu appuser "$@"
