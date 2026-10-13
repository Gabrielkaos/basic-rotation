#!/bin/sh
# Runs once, when the database volume is first created, and sets up the two
# logins. Blue gets the password from its slot in api_secrets, so the API
# works from the start; green gets no password until the first rotation gives
# it one.
#
# The same script also works against any database when run by hand, with the
# usual PGHOST/PGPORT/PGPASSWORD variables set:
#   SECRETS_DIR=secrets SCHEMA_FILE=db/schema.sql sh db/init.sh
set -eu
SECRETS_DIR="${SECRETS_DIR:-/secrets}"
SCHEMA_FILE="${SCHEMA_FILE:-/db/schema.sql}"

BLUE_PASSWORD=$(sed -n 's/.*"app_blue_password": *"\([^"]*\)".*/\1/p' "$SECRETS_DIR/api_secrets.json")
if [ -z "$BLUE_PASSWORD" ]; then
  echo "$SECRETS_DIR/api_secrets.json has no app_blue_password: run 'python3 rotate/secrets_store.py init' first" >&2
  exit 1
fi

psql -v ON_ERROR_STOP=1 -U postgres -d demo -v blue_password="$BLUE_PASSWORD" -f "$SCHEMA_FILE"
