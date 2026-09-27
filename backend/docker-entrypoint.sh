#!/bin/sh
# Route ins Management-Netz über den WireGuard-Hub-Container legen, damit
# RouterOS-API-Calls ausschließlich durch den Tunnel laufen. Gesteuert über ROUTE_WG_NETWORK/ROUTE_VIA_HUB
# (bewusst getrennt von WG_NETWORK, das die App selbst braucht).
#
# NIE im Netz-Namespace des Hubs (syslog/flows mit network_mode service:wireguard-hub): dort würde
# "ip route replace $ROUTE_WG_NETWORK via $ROUTE_VIA_HUB" die Route ins wg0-Interface überschreiben – der Hub
# schickt Tunnel-Pakete dann an sich selbst, alle Router wären offline.
set -e
if [ -n "$ROUTE_VIA_HUB" ] && [ -n "$ROUTE_WG_NETWORK" ]; then
  if ip link show "${WG_INTERFACE:-wg0}" >/dev/null 2>&1; then
    echo "im Hub-Namespace – Route übersprungen (Interface ${WG_INTERFACE:-wg0} vorhanden)"
  else
    ip route replace "$ROUTE_WG_NETWORK" via "$ROUTE_VIA_HUB" 2>/dev/null \
      && echo "route $ROUTE_WG_NETWORK via $ROUTE_VIA_HUB" \
      || echo "WARN: konnte Route nicht setzen (NET_ADMIN fehlt?)"
  fi
fi
if [ "$RUN_MIGRATIONS" = "true" ]; then
  alembic upgrade head
fi
exec "$@"
