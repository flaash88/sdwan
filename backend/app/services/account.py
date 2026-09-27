"""Konto-Notfälle (Phase 22): 2FA zurücksetzen, Sperre aufheben – aus der Oberfläche (MSP-Admin) und per CLI."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.models import User
from app.services import platform_events


async def reset_2fa(db: AsyncSession, user: User, actor: User | None, via: str, ip: str | None = None) -> None:
    user.totp_enabled, user.totp_secret_enc, user.totp_last_step, user.recovery_codes = False, None, None, []
    user.token_version = (user.token_version or 0) + 1  # AUDIT-029: laufende Anmeldungen beenden
    await audit(db, "auth.2fa_reset", user=actor, tenant_id=user.tenant_id, ip=ip, target_type="user", target_id=user.id,
                details={"email": user.email, "via": via, "by": actor.email if actor else via})
    await platform_events.notify(db, "2FA zurückgesetzt", f"Zwei-Faktor-Anmeldung von {user.email} wurde zurückgesetzt "
                                 f"({actor.email if actor else via}). Bei der nächsten Anmeldung muss sie neu eingerichtet werden.",
                                 "warning", facts={"Benutzer": user.email, "durch": actor.email if actor else via})


async def unlock(db: AsyncSession, user: User, actor: User | None, via: str, ip: str | None = None) -> dict[str, Any]:
    was = user.locked_until
    user.locked_until, user.failed_logins = None, 0
    await audit(db, "auth.unlock", user=actor, tenant_id=user.tenant_id, ip=ip, target_type="user", target_id=user.id,
                details={"email": user.email, "via": via, "was_locked_until": was.isoformat() if was else None})
    if via == "cli":
        await platform_events.notify(db, "Konto entsperrt", f"Sperre von {user.email} per CLI aufgehoben.", "warning",
                                     facts={"Benutzer": user.email})
    return {"was_locked": was is not None}
