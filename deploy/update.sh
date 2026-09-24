#!/usr/bin/env bash
# Update: neuesten Stand holen, Images neu bauen, Stack neu starten (Migrationen laufen automatisch).
set -euo pipefail
cd "$(dirname "$0")/.."
BRANCH=$(git rev-parse --abbrev-ref HEAD)
git pull --ff-only origin "$BRANCH"
docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null
docker compose ps
