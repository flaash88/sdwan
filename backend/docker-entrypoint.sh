#!/bin/sh
# Route ins Management-Netz über den WireGuard-Hub-Container legen, damit
# RouterOS-API-Calls ausschließlich durch den Tunnel laufen.
#
# NIE im Netz-Namespace des Hubs (syslog/flows mit network_mode service:wireguard-hub): dort würde
# "ip route replace $WG_NETWORK via $HUB_INTERNAL_IP" die Route ins wg0-Interface überschreiben – der Hub
# schickt Tunnel-Pakete dann an sich selbst, alle Router wären offline.
set -e
if [ -n "$HUB_INTERNAL_IP" ] && [ -n "$WG_NETWORK" ]; then
  if ip link show "${WG_INTERFACE:-wg0}" >/dev/null 2>&1; then
    echo "im Hub-Namespace – Route übersprungen (Interface ${WG_INTERFACE:-wg0} vorhanden)"
  else
    ip route replace "$WG_NETWORK" via "$HUB_INTERNAL_IP" 2>/dev/null \
      && echo "route $WG_NETWORK via $HUB_INTERNAL_IP" \
      || echo "WARN: konnte Route nicht setzen (NET_ADMIN fehlt?)"
  fi
fi
if [ "$RUN_MIGRATIONS" = "true" ]; then
  alembic upgrade head
fi
exec "$@"
