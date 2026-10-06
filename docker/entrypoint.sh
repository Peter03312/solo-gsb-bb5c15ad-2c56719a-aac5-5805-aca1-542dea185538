#!/usr/bin/env sh
# Container startup: initialise schema + demo data (idempotent), then serve.
set -e

echo "[entrypoint] DB_PATH=${DB_PATH:-/data/licenses.db} SIGNING_KEYS_DIR=${SIGNING_KEYS_DIR:-/data/signing-keys} INIT_DB=${INIT_DB:-true} SEED_DEMO=${SEED_DEMO:-true} INIT_SIGNING_KEY=${INIT_SIGNING_KEY:-true}"

# INIT_DB (default true) runs CREATE TABLE IF NOT EXISTS; SEED_DEMO seeds an
# empty database. Initialisation happens inside the app lifespan, so a single
# uvicorn process both initialises and serves.
exec uvicorn app.main:app --host "${APP_HOST:-0.0.0.0}" --port "${APP_PORT:-8000}"
