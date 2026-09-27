"""Notfall-Befehle mit Server-Zugriff (Phase 22):

    docker compose exec api python -m app.cli reset-2fa <email>
    docker compose exec api python -m app.cli unlock <email>
    docker compose run --rm --no-deps api python -m app.cli encryption-key

``encryption-key`` gibt den aktuell wirksamen Schlüssel für verschlüsselte DB-Werte aus (``ENCRYPTION_KEY`` bzw. aus
``SECRET_KEY`` abgeleitet). Vor dem Austausch eines Standard-``SECRET_KEY`` (AUDIT-006) als ``ENCRYPTION_KEY`` in die .env
übernehmen – sonst sind Geräte-Passwörter und PSKs nicht mehr lesbar.

Beide schreiben einen Audit-Eintrag (Akteur ``cli``) und melden sich über den Plattform-Webhook.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.db import system_session
from app.models import User


async def _run(cmd: str, email: str) -> int:
    from app.services.account import reset_2fa, unlock

    async with system_session() as db:
        user = (await db.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none()
        if user is None:
            print(f"Benutzer {email} nicht gefunden")
            return 1
        if cmd == "reset-2fa":
            await reset_2fa(db, user, None, "cli")
            msg = f"2FA von {user.email} zurückgesetzt – Einrichtung bei der nächsten Anmeldung"
        else:
            res = await unlock(db, user, None, "cli")
            msg = f"{user.email} entsperrt" + ("" if res["was_locked"] else " (war nicht gesperrt)")
        await db.commit()
    print(msg)
    return 0


def encryption_key() -> str:
    import base64
    import hashlib

    from app.config import get_settings

    s = get_settings()
    return s.encryption_key or base64.urlsafe_b64encode(hashlib.sha256(("enc:" + s.secret_key).encode()).digest()).decode()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    ap.add_argument("command", choices=["reset-2fa", "unlock", "encryption-key"])
    ap.add_argument("email", nargs="?")
    a = ap.parse_args(argv)
    if a.command == "encryption-key":
        print(encryption_key())
        return 0
    if not a.email:
        ap.error("E-Mail-Adresse fehlt")
    return asyncio.run(_run(a.command, a.email))


if __name__ == "__main__":
    raise SystemExit(main())
