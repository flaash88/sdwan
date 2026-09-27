#!/usr/bin/env bash
# Update: neuesten Stand holen, Images neu bauen, Stack neu starten (Migrationen laufen automatisch).
set -euo pipefail
cd "$(dirname "$0")/.."
BRANCH=$(git rev-parse --abbrev-ref HEAD)
git pull --ff-only origin "$BRANCH"

hub_id() { docker compose ps -q wireguard-hub 2>/dev/null || true; }
HUB_BEFORE=$(hub_id)
docker compose up -d --build --remove-orphans
HUB_AFTER=$(hub_id)

# syslog und flows laufen im Netz-Namespace des Hubs: wurde der Hub neu erstellt, hängen sie am alten Namespace
if [ "$HUB_BEFORE" != "$HUB_AFTER" ]; then
  echo "wireguard-hub wurde neu erstellt – starte syslog und flows neu"
  docker compose up -d --force-recreate --no-deps syslog flows
fi
docker image prune -f >/dev/null

# Auf einen gesunden Hub warten (Healthcheck: Peer-Sync aktuell, Peer-Anzahl = API-Liste)
echo -n "Warte auf wireguard-hub (healthy) "
for _ in $(seq 1 60); do
  STATUS=$(docker inspect -f '{{.State.Health.Status}}' "$(hub_id)" 2>/dev/null || echo "unbekannt")
  [ "$STATUS" = "healthy" ] && break
  echo -n "."
  sleep 5
done
echo " $STATUS"
PEERS=$(docker compose exec -T wireguard-hub sh -c 'wg show wg0 peers | wc -l' 2>/dev/null || echo "?")
echo "WireGuard-Peers im Hub: $PEERS"
docker compose exec -T wireguard-hub python /app/agent.py --health || true
docker compose ps
if [ "$STATUS" != "healthy" ]; then
  echo "WARNUNG: wireguard-hub ist nicht healthy – docker compose logs wireguard-hub prüfen" >&2
  exit 1
fi
