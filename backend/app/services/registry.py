"""Zentrale Registrierung der Poll-Hooks aller Phasen (vermeidet zirkuläre Imports)."""

from __future__ import annotations

from typing import Any


def poll_hooks() -> list[Any]:
    """async fn(device, api, resource) -> dict | None  (Ergebnis landet in device.facts)."""
    from app.services import mesh

    return [mesh.mesh_poll_hook]


def post_poll_hooks() -> list[Any]:
    """async fn(db, devices) -> None, läuft nach jedem Polling-Durchlauf."""
    from app.services import mesh

    return [mesh.update_peer_status]
