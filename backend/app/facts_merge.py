"""Kein Lost Update in ``Device.facts`` (AUDIT-015).

``facts`` ist ein JSON-Dokument, in das mehrere Stellen schreiben (Poll, Content-Filter ``dns_backup``, Reboot-Marker der
Alarme, VRRP-Peers, Firmware-Info …) – jeweils als ``dev.facts = {**dev.facts, …}`` aus der beim Laden gelesenen Kopie.
Laufen zwei Sessions überlappend (typisch: Flotten-Poll über mehrere Sekunden), überschrieb die spätere die Änderung der
früheren.

Vor jedem Flush wird deshalb für jedes geänderte Gerät ein Drei-Wege-Merge gemacht: Basis = beim Laden gelesener Stand,
„ours“ = Stand in dieser Session, „theirs“ = aktueller Stand in der Datenbank. Schlüssel, die diese Session geändert oder
entfernt hat, gewinnen; alle anderen kommen aus der Datenbank. Eine Abfrage je Flush für alle betroffenen Geräte.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

_MISSING = object()


def three_way(base: dict[str, Any] | None, ours: dict[str, Any] | None, theirs: dict[str, Any] | None) -> dict[str, Any]:
    base, ours, theirs = dict(base or {}), dict(ours or {}), dict(theirs or {})
    out = dict(theirs)
    for k in set(base) | set(ours):
        b, o = base.get(k, _MISSING), ours.get(k, _MISSING)
        if b == o:
            continue  # von dieser Session nicht geändert -> Datenbankstand behalten
        if o is _MISSING:
            out.pop(k, None)  # diese Session hat den Schlüssel entfernt
        else:
            out[k] = o
    return out


@event.listens_for(Session, "before_flush")
def _merge_device_facts(session: Session, _flush_context: Any, _instances: Any) -> None:
    from app.models import Device

    pending: dict[Any, tuple[Device, dict[str, Any]]] = {}
    for obj in session.dirty:
        if not isinstance(obj, Device) or obj.id is None:
            continue
        hist = inspect(obj).attrs.facts.history
        if not hist.has_changes() or not hist.deleted:
            continue
        pending[obj.id] = (obj, hist.deleted[0] or {})
    if not pending:
        return
    with session.no_autoflush:
        rows = session.execute(select(Device.id, Device.facts).where(Device.id.in_(list(pending)))
                               .execution_options(skip_tenant_filter=True)).all()
    for dev_id, theirs in rows:
        obj, base = pending[dev_id]
        if (theirs or {}) == base:
            continue  # niemand sonst hat geschrieben
        obj.facts = three_way(base, obj.facts, theirs)
