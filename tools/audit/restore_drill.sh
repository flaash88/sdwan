#!/usr/bin/env bash
# Audit D – Restore-Übung ohne Docker: platform_backup run (echte Quell-DB) → restore in eine frische DB →
# Zeilenzahlen je Tabelle vergleichen. Aufruf: SRC_DB=sdwan tools/audit/restore_drill.sh  (PY=… Interpreter)
set -euo pipefail
cd "$(dirname "$0")/../../backend" || exit 1
PY="${PY:-python}"
SRC_DB="${SRC_DB:-sdwan}"
DST_DB=sdwan_audit_restore
W="$(mktemp -d /tmp/audit-restore.XXXXXX)"
trap 'rm -rf "$W"' EXIT
"$PY" - "$W" <<'PYEOF'
import sys, pyrage
k = pyrage.x25519.Identity.generate()
open(f"{sys.argv[1]}/identity.txt", "w").write(str(k))
open(f"{sys.argv[1]}/recipient.txt", "w").write(str(k.to_public()))
PYEOF
mkdir -p "$W/hub" "$W/out" "$W/backups"
printf 'SECRET_KEY=drill\n' > "$W/env"; printf 'hubkey\n' > "$W/hub/hub.key"
export DATABASE_URL="postgresql+asyncpg://sdwan:sdwan@localhost:5432/$SRC_DB" ROUTEROS_BACKEND=simulator INFLUX_ENABLED=false
PLATFORM_BACKUP_AGE_RECIPIENT="$(cat "$W/recipient.txt")"
export PLATFORM_BACKUP_AGE_RECIPIENT PLATFORM_BACKUP_DIR="$W/backups"
export PLATFORM_BACKUP_ENV_FILE="$W/env" PLATFORM_BACKUP_HUB_DIR="$W/hub"
t0=$(date +%s)
"$PY" -m app.platform_backup run
ARCHIVE="$(find "$W/backups" -type f | head -1)"
t1=$(date +%s)
su postgres -c "psql -q -c 'DROP DATABASE IF EXISTS $DST_DB' -c 'CREATE DATABASE $DST_DB OWNER sdwan'"
"$PY" -m app.platform_backup restore "$ARCHIVE" --identity "$W/identity.txt" --out "$W/out" \
  --database-url "postgresql+asyncpg://sdwan:sdwan@localhost:5432/$DST_DB"
t2=$(date +%s)
count() {
  su postgres -c "psql -At -d $1 -c \"select table_name from information_schema.tables where table_schema='public' order by 1\"" |
  while read -r t; do echo "$t $(su postgres -c "psql -At -d $1 -c 'select count(*) from \"$t\"'")"; done
}
count "$SRC_DB" > "$W/src.txt"; count "$DST_DB" > "$W/dst.txt"
echo "Sicherung ${ARCHIVE##*/}: $(stat -c %s "$ARCHIVE") Byte, Dauer $((t1-t0)) s; Restore $((t2-t1)) s"
echo "Tabellen: $(wc -l < "$W/src.txt"), Zeilen gesamt Quelle $(awk '{s+=$2} END{print s}' "$W/src.txt"), Ziel $(awk '{s+=$2} END{print s}' "$W/dst.txt")"
echo ".env zurück: $(cat "$W/out/env" | head -1) · Hub: $(ls "$W/out/hub")"
if diff "$W/src.txt" "$W/dst.txt"; then echo "RESTORE OK – alle Tabellen identisch gezählt"; else echo "RESTORE ABWEICHUNG"; exit 1; fi
