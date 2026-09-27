"""Notfall-Befehle mit Server-Zugriff (Phase 22):

    docker compose exec api python -m app.cli reset-2fa <email>
    docker compose exec api python -m app.cli unlock <email>

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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    ap.add_argument("command", choices=["reset-2fa", "unlock"])
    ap.add_argument("email")
    a = ap.parse_args(argv)
    return asyncio.run(_run(a.command, a.email))


if __name__ == "__main__":
    raise SystemExit(main())
