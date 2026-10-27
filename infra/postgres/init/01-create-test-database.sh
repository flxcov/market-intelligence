#!/bin/sh
# Creates a separate database for automated tests, so tests never touch dev data.
# Executed by the postgres image on first start only (empty data volume).
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -c "CREATE DATABASE \"${POSTGRES_DB}_test\" OWNER \"$POSTGRES_USER\";"
