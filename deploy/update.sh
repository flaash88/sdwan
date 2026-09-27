#!/usr/bin/env bash
# Update: neuesten Stand holen, Images neu bauen, Stack neu starten (Migrationen laufen automatisch).
set -euo pipefail
cd "$(dirname "$0")/.."
BRANCH=$(git rev-parse --abbrev-ref HEAD)
git pull --ff-only origin "$BRANCH"

# Vor jedem Neubau/Neustart: Standard-Geheimnisse? Dann abbrechen und NICHTS neu starten – die laufende Plattform bleibt
# in Betrieb (ein Neustart mit Standardwerten würde ohnehin verweigert, AUDIT-006/023).
if ! deploy/check-secrets.sh .env; then
  echo "Update abgebrochen – keine Container neu gebaut oder gestartet. Nach dem Ersetzen erneut: deploy/update.sh" >&2
  exit 1
fi

# AUDIT-003: bestehende Installationen ohne TRUSTED_PROXIES – Frontend-nginx (.30) und bei Caddy/nginx auf dem Host
# zusätzlich das Docker-Gateway (.1) eintragen, sonst zählt das Login-Limit alle Nutzer als eine Adresse.
if [ -f .env ] && ! grep -qE "^TRUSTED_PROXIES=" .env; then
  NET=$(grep -E "^SDWAN_NET=" .env | tail -n1 | cut -d= -f2-); NET=${NET:-172.30.0}
  if { [ -f /etc/caddy/Caddyfile ] && systemctl is-active --quiet caddy 2>/dev/null; } || [ -f /etc/nginx/sites-enabled/sdwan.conf ]; then
    echo "TRUSTED_PROXIES=${NET}.30,${NET}.1" >> .env
  else
    echo "TRUSTED_PROXIES=${NET}.30" >> .env
  fi
  echo "TRUSTED_PROXIES in .env ergänzt: $(grep -E '^TRUSTED_PROXIES=' .env | cut -d= -f2-) (eigener Proxy davor? Adresse ergänzen)"
fi

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
# Erreichbarkeit vom Hub aus: jede Tunnel-IP der gekoppelten Geräte (Peers) einmal anpingen (1 Paket, 2 s)
REACH=$(docker compose exec -T wireguard-hub sh -c '
  ok=0; all=0
  for ip in $(wg show wg0 allowed-ips | awk "{for (i=2;i<=NF;i++) print \$i}" | cut -d/ -f1); do
    all=$((all+1)); ping -c 1 -W 2 "$ip" >/dev/null 2>&1 && ok=$((ok+1))
  done
  echo "$ok/$all"' 2>/dev/null || echo "?")
echo "Vom Hub erreichbare Geräte (Ping auf Tunnel-IP): $REACH"
docker compose ps
if [ "$STATUS" != "healthy" ]; then
  echo "WARNUNG: wireguard-hub ist nicht healthy – docker compose logs wireguard-hub prüfen" >&2
  exit 1
fi
