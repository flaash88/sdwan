"""Sicherheitsmeldungen (Phase 23): Pflege durch MSP-Admins, Betroffenheit je Gerät und Flotte."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.v1.common import get_or_404
from app.deps import Ctx, ReadCtx, SuperCtx
from app.models import Device, SecurityAdvisory
from app.services import advisories as adv

router = APIRouter(tags=["advisories"])


class AdvisoryIn(BaseModel):
    cve: str = Field(min_length=3, max_length=40)
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    function: str = "general"
    severity: str = "medium"
    affected_from: str = Field(min_length=1, max_length=20)
    affected_to: str | None = Field(default=None, max_length=20)
    fixed_in: str | None = Field(default=None, max_length=20)
    link: str | None = Field(default=None, max_length=500, pattern=r"^https?://")
    enabled: bool = True


def _validate(data: AdvisoryIn) -> dict[str, Any]:
    d = data.model_dump()
    d["affected_to"] = d["affected_to"] or None
    d["fixed_in"] = d["fixed_in"] or None
    try:
        adv.validate(d)
    except adv.AdvisoryError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return d


@router.get("/advisories")
async def list_advisories(ctx: Ctx = ReadCtx) -> dict[str, Any]:
    """Alle Meldungen mit Anzahl betroffener Geräte im aktuellen Bereich (Mandant bzw. alle für MSP)."""
    rows = (await ctx.db.execute(select(SecurityAdvisory).order_by(SecurityAdvisory.cve))).scalars().all()
    devices = (await ctx.db.execute(select(Device))).scalars().all()
    counts: dict[str, dict[str, int]] = {}
    enabled = [a for a in rows if a.enabled]
    for d in devices:
        for m in await adv.device_advisories(ctx.db, d, enabled):
            c = counts.setdefault(m["id"], {"affected": 0, "possible": 0})
            c[m["status"]] += 1
    return {"functions": adv.FUNCTIONS, "severities": adv.SEVERITIES,
            "advisories": [{**adv.advisory_out(a), **counts.get(str(a.id), {"affected": 0, "possible": 0})} for a in rows]}


@router.get("/advisories/fleet")
async def fleet(ctx: Ctx = ReadCtx) -> dict[str, list[dict[str, Any]]]:
    """Je Gerät die zutreffenden Meldungen (für Geräteliste, Firmware-Seite, Dashboard)."""
    enabled = await adv.enabled_advisories(ctx.db)
    out: dict[str, list[dict[str, Any]]] = {}
    if not enabled:
        return out
    for d in (await ctx.db.execute(select(Device))).scalars():
        m = await adv.device_advisories(ctx.db, d, enabled)
        if m:
            out[str(d.id)] = [{k: x[k] for k in ("id", "cve", "title", "severity", "function", "fixed_in", "status", "link")} for x in m]
    return out


@router.get("/devices/{device_id}/advisories")
async def device_advisories(device_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict[str, Any]:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    return {"version": dev.routeros_version, "functions": await adv.device_functions(ctx.db, dev),
            "advisories": await adv.device_advisories(ctx.db, dev)}


@router.post("/advisories", status_code=201)
async def create(data: AdvisoryIn, ctx: Ctx = SuperCtx) -> dict[str, Any]:
    a = SecurityAdvisory(builtin=False, created_by=ctx.user.email, **_validate(data))
    ctx.db.add(a)
    await ctx.db.flush()
    await ctx.audit("advisory.create", target_type="security_advisory", target_id=a.id, details={"cve": a.cve})
    await ctx.db.commit()
    return adv.advisory_out(a)


@router.put("/advisories/{advisory_id}")
async def update(advisory_id: uuid.UUID, data: AdvisoryIn, ctx: Ctx = SuperCtx) -> dict[str, Any]:
    a = await get_or_404(ctx.db, SecurityAdvisory, advisory_id, "Meldung")
    if a.builtin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Beispiel-Eintrag – bitte eine eigene Meldung anlegen")
    for k, v in _validate(data).items():
        setattr(a, k, v)
    await ctx.audit("advisory.update", target_type="security_advisory", target_id=a.id, details={"cve": a.cve, "enabled": a.enabled})
    await ctx.db.commit()
    return adv.advisory_out(a)


@router.delete("/advisories/{advisory_id}", status_code=204, response_model=None)
async def delete(advisory_id: uuid.UUID, ctx: Ctx = SuperCtx) -> None:
    a = await get_or_404(ctx.db, SecurityAdvisory, advisory_id, "Meldung")
    if a.builtin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Beispiel-Eintrag kann nicht gelöscht werden")
    await ctx.audit("advisory.delete", target_type="security_advisory", target_id=a.id, details={"cve": a.cve})
    await ctx.db.delete(a)
    await ctx.db.commit()
