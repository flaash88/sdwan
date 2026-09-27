"""Zentrale Registrierung der Poll-Hooks aller Phasen (vermeidet zirkuläre Imports)."""

from __future__ import annotations

from typing import Any


def poll_hooks() -> list[Any]:
    """async fn(device, api, resource) -> dict | None  (Ergebnis landet in device.facts)."""
    from app.services import device_info, fw_hits, mesh, metrics, neighbors, vrrp, wan, wlan

    # vrrp zuletzt: ein abgebrochener Peer-Ping verwirft die Verbindung (siehe vrrp.PEER_PING_LIMIT_S)
    return [metrics.collect, device_info.info_poll_hook, mesh.mesh_poll_hook, wan.wan_poll_hook, fw_hits.fw_hits_hook,
            wlan.wlan_poll_hook, neighbors.neighbor_poll_hook, vrrp.vrrp_poll_hook]


def post_poll_hooks() -> list[Any]:
    """async fn(db, devices) -> None, läuft nach jedem Polling-Durchlauf."""
    from app.services import fw_hits, local_access, mesh, metrics, neighbors, vrrp, wan, ztp

    return [fw_hits.update_hits, mesh.update_peer_status, wan.update_wan_status, vrrp.update_vrrp_status, metrics.store_metrics, ztp.ztp_post_poll,
            neighbors.store_neighbors, local_access.post_poll]
