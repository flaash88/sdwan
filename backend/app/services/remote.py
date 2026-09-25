"""Remote Access (Phase 8): zeitlich begrenzte SSH/Winbox/WebFig-Zugänge über den Tunnel.

* Pro Session: eigener Port aus dem Pool des ``remote-proxy``-Dienstes, erlaubte Quell-IP/CIDR,
  Ablaufzeit, temporärer RouterOS-Benutzer (zufälliges Passwort, nur vom Hub aus erlaubt).
* Der Proxy verbindet ausschließlich zur Tunnel-IP des Geräts (Management-Netz).
* Jede Verbindung wird mit Quell-IP, Dauer und Bytes im Audit-Log festgehalten.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app.audit import audit
from app.config import get_settings
from app.db import system_session, utcnow
from app.models import Device, RemoteSession
from app.routeros import RouterOSError, connect_device
from app.routeros.client import DeviceAPI
from app.routeros.schema import REMOTE_GROUP, REMOTE_POLICIES
from app.security import generate_password

log = logging.getLogger(__name__)

PROTOCOLS: dict[str, tuple[str, int]] = {"ssh": ("ssh", 22), "winbox": ("winbox", 8291), "webfig": ("www", 80)}


class RemoteError(Exception):
    pass


async def allocate_port(db: AsyncSession) -> int:
    lo, hi = get_settings().remote_ports
    used = {
        r[0]
        for r in await db.execute(
            select(RemoteSession.listen_port).where(RemoteSession.status == "active").execution_options(skip_tenant_filter=True)
        )
    }
    for port in range(lo, hi + 1):
        if port not in used:
            return port
    raise RemoteError("Kein freier Proxy-Port – bitte bestehende Sessions beenden")


async def _ensure_service(api: DeviceAPI, service: str) -> tuple[int, dict[str, Any]]:
    """Dienst aktivieren und Hub-Adresse erlauben (falls der Dienst auf Adressen beschränkt ist).

    Rückgabe: (Port, Zustand vorher). Der Zustand ``{service, disabled, address, changed}`` wird in der Sitzung
    gespeichert und beim Ende wiederhergestellt, falls die Plattform etwas geändert hat."""
    rows = await api.print("/ip/service", name=service)
    if not rows:
        raise RemoteError(f"Dienst {service} nicht gefunden")
    svc = rows[0]
    hub = f"{get_settings().wg_hub_ip}/32"
    changes: dict[str, Any] = {}
    if str(svc.get("disabled", "false")).lower() in ("true", "yes"):
        changes["disabled"] = "no"
    addr = str(svc.get("address", "") or "")
    if addr and hub not in addr.split(","):
        changes["address"] = f"{addr},{hub}"
    before = {"service": service, "disabled": str(svc.get("disabled", "false")), "address": addr, "changed": bool(changes)}
    if changes:
        await api.set("/ip/service", svc[".id"], **changes)
    return int(svc.get("port", PROTOCOLS.get(service, ("", 0))[1]) or 0), before


async def _other_active(db: AsyncSession, sess: RemoteSession) -> list[RemoteSession]:
    """Andere aktive Sitzungen auf demselben Gerät, die denselben RouterOS-Dienst nutzen."""
    service = PROTOCOLS[sess.protocol][0]
    rows = (await db.execute(select(RemoteSession).where(
        RemoteSession.device_id == sess.device_id, RemoteSession.status == "active", RemoteSession.id != sess.id,
    ).order_by(RemoteSession.created_at))).scalars().all()
    return [r for r in rows if PROTOCOLS.get(r.protocol, ("",))[0] == service]


async def _restore_service(db: AsyncSession, sess: RemoteSession, device: Device) -> None:
    """Ursprünglichen Dienstzustand wiederherstellen – nur wenn die Plattform ihn geändert hat und keine andere
    aktive Sitzung den Dienst noch braucht. Bei Fehler bleibt ``pending`` gesetzt; der Worker versucht es erneut."""
    st = dict(sess.service_restore or {})
    if not st.get("changed") or st.get("restored"):
        return
    if await _other_active(db, sess):
        st["pending"] = False
        st["handed_over"] = True  # eine andere Sitzung trägt denselben Ursprungszustand und stellt zurück
        sess.service_restore = st
        return
    try:
        async with connect_device(device) as api:
            for r in await api.print("/ip/service", name=st["service"]):
                await api.set("/ip/service", r[".id"], disabled=st["disabled"], address=st["address"])
        st.update(pending=False, restored=True)
    except RouterOSError as exc:
        st["pending"] = True
        sess.last_error = f"Dienst {st['service']} konnte nicht zurückgestellt werden: {exc}"
        log.warning("Remote-Session %s: %s", sess.id, exc)
    sess.service_restore = st


async def open_session(db: AsyncSession, device: Device, user: Any, protocol: str, minutes: int, allowed_cidr: str, reason: str | None) -> tuple[RemoteSession, str | None]:
    if protocol not in PROTOCOLS:
        raise RemoteError("Protokoll: ssh | winbox | webfig")
    service, default_port = PROTOCOLS[protocol]
    port = await allocate_port(db)
    sess = RemoteSession(
        tenant_id=device.tenant_id, device_id=device.id, user_id=user.id, user_email=user.email, protocol=protocol,
        target_port=default_port, listen_port=port, allowed_cidr=allowed_cidr, reason=reason, status="active",
        expires_at=utcnow() + dt.timedelta(minutes=minutes),
    )
    db.add(sess)
    await db.flush()
    password: str | None = None
    # Läuft schon eine Sitzung mit demselben Dienst, gilt deren Ursprungszustand (nicht der bereits geänderte)
    inherited = next((o.service_restore for o in await _other_active(db, sess) if o.service_restore), None)
    try:
        async with connect_device(device) as api:
            port, before = await _ensure_service(api, service)
            sess.target_port = port or default_port
            sess.service_restore = dict(inherited) if inherited else before
            if protocol in ("ssh", "winbox", "webfig"):
                await _ensure_remote_group(api)
                username = f"sdwan-rs-{sess.id.hex[:8]}"
                password = generate_password(20)
                await api.add("/user", name=username, group=REMOTE_GROUP, password=password,
                               address=f"{get_settings().wg_hub_ip}/32", comment=f"sdwan:remote:{sess.id}")
                sess.ros_username = username
    except RemoteError as exc:
        _failed(sess, str(exc), inherited)
        raise
    except RouterOSError as exc:
        _failed(sess, str(exc), inherited)
        raise RemoteError(f"Gerät nicht erreichbar: {exc}") from exc
    return sess, password


def _failed(sess: RemoteSession, error: str, inherited: dict[str, Any] | None) -> None:
    sess.status, sess.last_error, sess.closed_at = "failed", error, utcnow()
    # Dienst wurde evtl. schon eingeschaltet -> vom Worker zurückstellen lassen (nicht, wenn eine andere Sitzung ihn nutzt)
    if sess.service_restore and sess.service_restore.get("changed") and not inherited:
        sess.service_restore = {**sess.service_restore, "pending": True}


async def _ensure_remote_group(api: DeviceAPI) -> None:
    """Gruppe für temporäre Benutzer anlegen/aktualisieren. Kein Ausweichen auf 'full', wenn das scheitert."""
    from app.services.api_rights import ensure_group

    hint = (f"Der API-Benutzer braucht selbst alle Policies der Gruppe {REMOTE_GROUP} ({', '.join(REMOTE_POLICIES)}) – "
            "siehe Selbsttest „Rechte API-Benutzer“")
    try:
        ok = await ensure_group(api, REMOTE_GROUP, REMOTE_POLICIES, "sdwan:remote")
    except RouterOSError as exc:
        raise RemoteError(f"Gruppe {REMOTE_GROUP} konnte nicht angelegt werden: {exc}. {hint}") from exc
    if not ok:
        raise RemoteError(f"Gruppe {REMOTE_GROUP} hat nach dem Anlegen nicht die erwarteten Policies. {hint}")


async def close_session(db: AsyncSession, sess: RemoteSession, status: str, by: str | None) -> None:
    if sess.status != "active":
        return
    sess.status, sess.closed_at, sess.closed_by = status, utcnow(), by
    device = await db.get(Device, sess.device_id)
    if device is not None and sess.ros_username:
        try:
            async with connect_device(device) as api:
                for u in await api.print("/user", name=sess.ros_username):
                    await api.remove("/user", u[".id"])
        except RouterOSError as exc:
            sess.last_error = f"Temp-User konnte nicht entfernt werden: {exc}"
            log.warning("Remote-Session %s: %s", sess.id, exc)
    if device is not None:
        await db.flush()  # Status 'closed' sichtbar für die Prüfung auf weitere aktive Sitzungen
        await _restore_service(db, sess, device)
    await events.publish(sess.tenant_id, "remote.session", {"id": str(sess.id), "status": status})


async def expire_sessions() -> None:
    """Worker-Job: abgelaufene Sessions schließen, Temp-User entfernen, Dienste zurückstellen.

    Liest alles aus der Datenbank – funktioniert daher auch nach einem Neustart der Plattform. Fehlgeschlagene
    Rückstellungen (Router war nicht erreichbar) werden hier erneut versucht."""
    async with system_session() as db:
        since = utcnow() - dt.timedelta(days=7)
        for sess in (await db.execute(select(RemoteSession).where(
                RemoteSession.status != "active", RemoteSession.closed_at >= since, RemoteSession.service_restore.is_not(None)))).scalars().all():
            if (sess.service_restore or {}).get("pending"):
                device = await db.get(Device, sess.device_id)
                if device is not None:
                    await _restore_service(db, sess, device)
        rows = (await db.execute(select(RemoteSession).where(RemoteSession.status == "active", RemoteSession.expires_at <= utcnow()))).scalars().all()
        for sess in rows:
            await close_session(db, sess, "expired", "system")
            await audit(db, "remote.expired", tenant_id=sess.tenant_id, target_type="device", target_id=sess.device_id,
                        details={"session": str(sess.id), "user": sess.user_email, "protocol": sess.protocol,
                                 "connections": sess.connections, "bytes_in": sess.bytes_in, "bytes_out": sess.bytes_out})
        await db.commit()
