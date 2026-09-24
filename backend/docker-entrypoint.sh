#!/bin/sh
# Route ins Management-Netz über den WireGuard-Hub-Container legen, damit
# RouterOS-API-Calls ausschließlich durch den Tunnel laufen.
set -e
if [ -n "$HUB_INTERNAL_IP" ] && [ -n "$WG_NETWORK" ]; then
  ip route replace "$WG_NETWORK" via "$HUB_INTERNAL_IP" 2>/dev/null \
    && echo "route $WG_NETWORK via $HUB_INTERNAL_IP" \
    || echo "WARN: konnte Route nicht setzen (NET_ADMIN fehlt?)"
fi
if [ "$RUN_MIGRATIONS" = "true" ]; then
  alembic upgrade head
fi
exec "$@"
