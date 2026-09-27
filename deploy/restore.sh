#!/usr/bin/env bash
# =============================================================================
# Plattform-Wiederherstellung (Phase 21) auf einem FRISCHEN Server
#
#   1. Server vorbereiten: DNS des Hub-Endpoints (WG_HUB_ENDPOINT) und PUBLIC_URL auf den neuen Server zeigen
#      lassen – gleicher Name ⇒ die Router verbinden sich ohne Eingriff wieder.
#   2. Repository auschecken (gleicher Stand oder neuer), dann:
#
#      sudo ARCHIVE=/pfad/sdwan-platform-….tar.gz.age IDENTITY=/pfad/age-key.txt bash deploy/restore.sh
#
#   IDENTITY ist die Datei mit dem age-Private-Key (AGE-SECRET-KEY-…). Sie wird nur für den Restore gebraucht und
#   danach wieder vom Server entfernt (siehe docs/DISASTER-RECOVERY.md).
#
# Ablauf: entschlüsseln + prüfen → .env zurück → Hub-Schlüssel ins Volume → PostgreSQL starten und Dump einspielen
#         → gesamten Stack starten → Health-Check.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
: "${ARCHIVE:?ARCHIVE=<Sicherungsdatei> angeben}"
: "${IDENTITY:?IDENTITY=<age-Private-Key-Datei> angeben}"
[ -f "$ARCHIVE" ] || { echo "Archiv nicht gefunden: $ARCHIVE" >&2; exit 1; }
[ -f "$IDENTITY" ] || { echo "Schlüsseldatei nicht gefunden: $IDENTITY" >&2; exit 1; }
if [ -f .env ] && [ "${FORCE:-no}" != "yes" ]; then
  echo "Es gibt bereits eine .env – Restore überschreibt sie. Mit FORCE=yes fortfahren." >&2; exit 1
fi

WORK="$(mktemp -d /tmp/sdwan-restore.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
cp "$ARCHIVE" "$WORK/archive.age"
cp "$IDENTITY" "$WORK/identity.txt"

echo "➜ Image bauen (enthält pg_restore und die Restore-Logik)"
docker compose build api >/dev/null

echo "➜ Archiv entschlüsseln und Prüfsummen kontrollieren"
docker compose run --rm --no-deps -T -v "$WORK:/restore" --entrypoint python api \
  -m app.platform_backup restore /restore/archive.age --identity /restore/identity.txt --out /restore/out

echo "➜ .env zurückspielen"
install -m 600 "$WORK/out/env" .env

echo "➜ WireGuard-Hub-Schlüssel ins Volume hub-data"
docker compose run --rm --no-deps -T -v "$WORK/out/hub:/restore-hub:ro" --entrypoint sh wireguard-hub \
  -c 'mkdir -p /data && cp /restore-hub/* /data/ && chmod 600 /data/*.key'

echo "➜ PostgreSQL starten und Dump einspielen"
docker compose up -d postgres
for _ in $(seq 1 60); do docker compose exec -T postgres pg_isready -q && break; sleep 2; done
docker compose run --rm --no-deps -T -v "$WORK/out:/restore:ro" --entrypoint sh api -c \
  "pg_restore --clean --if-exists --no-owner -d \"\$(python -c 'from app.platform_backup import libpq_url; from app.config import get_settings; print(libpq_url(get_settings().database_url))')\" /restore/db.dump"

if [ -d "$WORK/out/influx" ]; then
  echo "⚠ InfluxDB-Sicherung vorhanden – Einspielen manuell: docker compose cp … influxdb && influx restore (siehe DISASTER-RECOVERY.md)"
fi

echo "➜ Stack starten"
docker compose up -d
for _ in $(seq 1 60); do
  if docker compose exec -T api python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')" 2>/dev/null; then
    echo "✔ Plattform läuft. Router verbinden sich über den gleichen Hub-Endpoint wieder (einige Minuten)."
    echo "  Jetzt: age-Schlüsseldatei vom Server löschen: shred -u \"$IDENTITY\""
    exit 0
  fi
  sleep 3
done
echo "⚠ Health-Check ohne Erfolg – Logs prüfen: docker compose logs api" >&2
exit 1
