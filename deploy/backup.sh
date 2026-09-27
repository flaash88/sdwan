#!/usr/bin/env bash
# =============================================================================
# Plattform-Sicherung (Phase 21) – manuell oder per Cron auf dem Server
#
#   sudo bash deploy/backup.sh            # Sicherung jetzt (im Worker-Container)
#   sudo bash deploy/backup.sh list       # vorhandene lokale Sicherungen
#
# Der Worker sichert zusätzlich täglich um PLATFORM_BACKUP_HOUR_UTC. Inhalt: PostgreSQL-Dump, .env (Schlüssel!),
# WireGuard-Hub-Schlüssel und -Konfiguration, optional InfluxDB – als ein age-verschlüsseltes Archiv in
# PLATFORM_BACKUP_HOST_DIR (Standard ./backups) und optional auf einem rclone-Ziel (S3/SFTP).
# Voraussetzung: PLATFORM_BACKUP_AGE_RECIPIENT (age-Public-Key) in .env. Siehe docs/DISASTER-RECOVERY.md.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
cmd="${1:-run}"
case "$cmd" in
  run)  docker compose exec -T worker python -m app.platform_backup run ;;
  list) docker compose exec -T worker python -m app.platform_backup list ;;
  *) echo "Verwendung: $0 [run|list]" >&2; exit 2 ;;
esac
