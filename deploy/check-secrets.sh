#!/usr/bin/env bash
# Prüft .env auf Standard- bzw. fehlende Geheimnisse (AUDIT-006/023). Exit 1 bei Fund – update.sh bricht dann ab,
# BEVOR Container neu gebaut oder gestartet werden (eine laufende Plattform bleibt unverändert in Betrieb).
# Aufruf: deploy/check-secrets.sh [pfad/zur/.env]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ENV_FILE="${1:-.env}"

val() {  # letzter Wert einer Variable in .env (ohne Anführungszeichen), leer wenn nicht gesetzt
  [ -f "$ENV_FILE" ] || return 0
  grep -E "^[[:space:]]*$1=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}

problems=0
bad() {
  problems=$((problems + 1))
  echo "  ✗ $1: $2" >&2
  echo "      Neuen Wert erzeugen und in $ENV_FILE eintragen:  $1=\$(openssl rand -hex ${3:-32})" >&2
}

# Variable, Standardwerte (Leer = Compose-Default greift), Mindestlänge
check() {
  local name=$1 minlen=$2; shift 2
  local v; v=$(val "$name")
  if [ -z "$v" ]; then bad "$name" "nicht gesetzt – der Standardwert aus docker-compose.yml bzw. der App würde verwendet"; return; fi
  for d in "$@"; do
    if [ "$v" = "$d" ]; then bad "$name" "Standardwert '$d'"; return; fi
  done
  case "$v" in change-me*) bad "$name" "Platzhalter '$v'"; return ;; esac
  if [ "${#v}" -lt "$minlen" ]; then bad "$name" "zu kurz (${#v} Zeichen, mindestens $minlen)"; fi
}

check SECRET_KEY 32 "change-me-please-change-me-please-32b"
check HUB_TOKEN 16 "change-me-hub-token"
check BOOTSTRAP_ADMIN_PASSWORD 10 "admin12345"
check INFLUX_TOKEN 16 "sdwan-influx-token"
check INFLUX_ADMIN_PASSWORD 12 "sdwan-influx-admin"
check GRAFANA_ADMIN_PASSWORD 12 "admin"

if [ "$problems" -gt 0 ]; then
  echo "" >&2
  echo "✗ $problems unsichere(s) Geheimnis(se) in $ENV_FILE – bitte ersetzen (siehe oben)." >&2
  echo "  Bei bestehenden Daten zusätzlich im jeweiligen Dienst ändern (die Werte gelten dort nur bei der Ersteinrichtung):" >&2
  echo "    GRAFANA_ADMIN_PASSWORD:  docker compose exec grafana grafana cli admin reset-admin-password '<neu>'" >&2
  echo "    INFLUX_ADMIN_PASSWORD:   docker compose exec influxdb influx user password -n admin -p '<neu>'" >&2
  echo "    INFLUX_TOKEN:            docker compose exec influxdb influx auth create --all-access --org sdwan  (Token in .env)" >&2
  echo "    SECRET_KEY:              VORHER den bisherigen Datenschlüssel sichern, sonst sind Geräte-Passwörter/PSKs unlesbar:" >&2
  echo "                             echo \"ENCRYPTION_KEY=\$(docker compose run --rm --no-deps -T api python -m app.cli encryption-key)\" >> .env" >&2
  echo "                             (nur wenn ENCRYPTION_KEY noch leer ist; Details: docs/DISASTER-RECOVERY.md, Geheimnisse ersetzen)" >&2
  exit 1
fi
echo "✓ Geheimnisse in $ENV_FILE: keine Standardwerte"
