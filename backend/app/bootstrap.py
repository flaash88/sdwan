"""Initialer MSP-Admin beim ersten Start."""

from __future__ import annotations

import logging

from sqlalchemy import func, select

from app.config import get_settings
from app.db import system_session
from app.models import Role, User
from app.security import hash_password

log = logging.getLogger(__name__)


async def ensure_bootstrap_admin() -> None:
    s = get_settings()
    async with system_session() as db:
        if await db.scalar(select(func.count()).select_from(User)):
            return
        db.add(User(email=s.bootstrap_admin_email.lower(), full_name="MSP Admin", role=Role.admin,
                    is_superuser=True, password_hash=hash_password(s.bootstrap_admin_password)))
        await db.commit()
        log.warning("Bootstrap-Admin %s angelegt – Passwort sofort ändern!", s.bootstrap_admin_email)
