from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, status

from app.api.v1.common import get_or_404
from app.config import get_settings
from app.deps import Ctx, ReadCtx
from app.models import Device
from app.services.metrics import MEASUREMENTS, RANGES, get_sink, live_payload, request_live

router = APIRouter(prefix="/devices/{device_id}/metrics", tags=["metrics"])


@router.get("")
async def history(
    device_id: uuid.UUID,
    ctx: Ctx = ReadCtx,
    measurement: str = Query(default="system"),
    range_: str = Query(default="1h", alias="range"),
) -> dict:
    if measurement not in MEASUREMENTS or range_ not in RANGES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"measurement: {MEASUREMENTS}, range: {list(RANGES)}")
    dev = await get_or_404(ctx.db, Device, device_id, "Device")  # Tenant-Prüfung
    try:
        rows = await get_sink().query(measurement, dev.id, range_)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Metrik-Datenbank nicht erreichbar: {exc}") from exc
    return {"measurement": measurement, "range": range_, "points": rows}


@router.post("/live")
async def live(device_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict:
    """Aktiviert 5-s-Polling für dieses Gerät (solange der Client alle ~60 s erneuert)."""
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    await request_live(dev.id)
    return {"live": True, "snapshot": live_payload(dev)}


@router.get("/grafana")
async def grafana_link(device_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    base = get_settings().grafana_public_url.rstrip("/")
    return {"url": f"{base}/d/sdwan-device/sdwan-device?var-tenant={dev.tenant_id}&var-device={dev.id}", "msp_only": True}
