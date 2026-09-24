"""Öffentliche Endpunkte, die der Router während des Onboardings aufruft (ohne JWT)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.db import get_db
from app.schemas import PairIn
from app.services.onboarding import onboarding_script
from app.services.pairing import PairingError, complete_pairing, find_device_by_token

router = APIRouter(tags=["onboarding"])
log = logging.getLogger(__name__)


def _rsc_error(msg: str) -> PlainTextResponse:
    safe = msg.replace('"', "'")
    return PlainTextResponse(f':error "SD-WAN: {safe}"\n', status_code=200)


@router.get("/onboard/{token}.rsc", response_class=PlainTextResponse)
async def get_onboarding_script(token: str, db: AsyncSession = Depends(get_db)) -> PlainTextResponse:
    try:
        device = await find_device_by_token(db, token)
    except PairingError as exc:
        return _rsc_error(str(exc))
    return PlainTextResponse(onboarding_script(token, device.name))


@router.post("/pair", response_class=PlainTextResponse)
async def pair(data: PairIn, request: Request, db: AsyncSession = Depends(get_db)) -> PlainTextResponse:
    ip = request.client.host if request.client else None
    try:
        _device, script = await complete_pairing(db, data, ip=ip)
    except PairingError as exc:
        await db.rollback()
        await audit(db, "device.pair_failed", details={"reason": str(exc), "serial": data.serial}, ip=ip, success=False)
        await db.commit()
        log.warning("Pairing fehlgeschlagen: %s", exc)
        return _rsc_error(str(exc))
    return PlainTextResponse(script)
