"""Zentrale Registrierung der Poll-Hooks aller Phasen (vermeidet zirkuläre Imports)."""

from __future__ import annotations

from typing import Any


def poll_hooks() -> list[Any]:
    """async fn(device, api, resource) -> dict | None  (Ergebnis landet in device.facts)."""
    from app.services import mesh, metrics, vrrp, wan

    return [metrics.collect, mesh.mesh_poll_hook, wan.wan_poll_hook, vrrp.vrrp_poll_hook]


def post_poll_hooks() -> list[Any]:
    """async fn(db, devices) -> None, läuft nach jedem Polling-Durchlauf."""
    from app.services import mesh, metrics, vrrp, wan, ztp

    return [mesh.update_peer_status, wan.update_wan_status, vrrp.update_vrrp_status, metrics.store_metrics, ztp.ztp_post_poll]
