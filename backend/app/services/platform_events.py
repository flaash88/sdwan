"""Plattformweite Alarme und Benachrichtigungen (ohne Mandant) – Phase 21 ff.

* ``fire``/``resolve``: Alarm in ``platform_alerts`` (je Typ höchstens einer aktiv), Mail an alle MSP-Admins und
  optional ``PLATFORM_WEBHOOK_URL``.
* ``notify``: reine Benachrichtigung ohne Alarm (z. B. 2FA zurückgesetzt, Vor-Ort-Passwort angezeigt).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import utcnow
from app.models import PlatformAlert, User

log = logging.getLogger(__name__)

TYPES = {"platform_backup_failed": "Plattform-Sicherung fehlgeschlagen"}


async def _superuser_mails(db: AsyncSession) -> list[str]:
    return [u.email for u in (await db.execute(select(User).where(User.is_superuser.is_(True), User.is_active.is_(True)))).scalars()]


async def notify(db: AsyncSession, title: str, text: str, severity: str = "info", facts: dict[str, str] | None = None,
                 mail: bool = False, webhook_url: str | None = None) -> bool:
    """Webhook (Plattform oder übergeben) und optional Mail an MSP-Admins. Fehler werden nur protokolliert."""
    from app.services import webhook

    sent = False
    url = webhook_url or get_settings().platform_webhook_url
    if url:
        try:
            webhook.validate_url(url)
            payload = webhook.build_payload("generic", title=title, text=text, severity=severity, resolved=False,
                                            facts=facts or {}, link=get_settings().public_url, extra={"source": "platform"})
            sent = await webhook.send(url, payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("Plattform-Webhook: %s", exc)
    if mail:
        try:
            from app.services.mailer import send_mail

            await send_mail(await _superuser_mails(db), f"[Plattform] {title}", text)
        except Exception as exc:  # noqa: BLE001
            log.warning("Plattform-Mail: %s", exc)
    return sent


async def fire(db: AsyncSession, type_: str, message: str, severity: str = "critical") -> PlatformAlert:
    a = (await db.execute(select(PlatformAlert).where(PlatformAlert.type == type_, PlatformAlert.status == "firing"))).scalar_one_or_none()
    if a is not None:
        a.message = message
        return a
    a = PlatformAlert(type=type_, status="firing", severity=severity, message=message, fired_at=utcnow())
    db.add(a)
    await db.flush()
    await notify(db, TYPES.get(type_, type_), message, severity, mail=True)
    return a


async def resolve(db: AsyncSession, type_: str) -> None:
    for a in (await db.execute(select(PlatformAlert).where(PlatformAlert.type == type_, PlatformAlert.status == "firing"))).scalars():
        a.status, a.resolved_at = "resolved", utcnow()
        await notify(db, f"Behoben: {TYPES.get(type_, type_)}", a.message, "info", mail=True)


def out(a: PlatformAlert) -> dict[str, Any]:
    return {"id": str(a.id), "type": a.type, "label": TYPES.get(a.type, a.type), "status": a.status, "severity": a.severity,
            "message": a.message, "fired_at": a.fired_at, "resolved_at": a.resolved_at}
