#!/usr/bin/env bash
# Audit C.4 – Lasttest 500 Geräte / 5 Mandanten gegen eine eigene PostgreSQL-Datenbank.
# Aufruf: tools/audit/run_load.sh [ausgabe.json]   (PY=… für den Python-Interpreter der Test-Umgebung)
set -euo pipefail
cd "$(dirname "$0")/../../backend" || exit 1
PY="${PY:-python}"
OUT="${1:-/tmp/audit_load.json}"
DB=sdwan_audit_load
su postgres -c "psql -q -c 'DROP DATABASE IF EXISTS $DB' -c 'CREATE DATABASE $DB OWNER sdwan'"
AUDIT_LOAD=1 AUDIT_LOAD_OUT="$OUT" TEST_DATABASE_URL="postgresql+asyncpg://sdwan:sdwan@localhost:5432/$DB" \
  "$PY" -m pytest -q -p no:logging tests/audit/test_audit_load.py
cat "$OUT"
