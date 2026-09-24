"""Interne Endpunkte für den WireGuard-Hub-Agent (Shared-Token-Auth, nicht öffentlich nutzen)."""

from __future__ import annotations

import datetime as dt
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db, utcnow
from app.models import Device, HubPeerStat, PairingStatus, SystemSetting
from app.schemas import HubPeer, HubPeerStatIn, HubRegisterIn
from app.services.wireguard import is_valid_wg_key

router = APIRouter(prefix="/internal/hub", tags=["internal"])


async def hub_auth(x_hub_token: str = Header(default="")) -> None:
    if not hmac.compare_digest(x_hub_token, get_settings().hub_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid hub token")


@router.put("/register", dependencies=[Depends(hub_auth)])
async def register_hub(data: HubRegisterIn, db: AsyncSession = Depends(get_db)) -> dict:
    if not is_valid_wg_key(data.public_key):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid key")
    row = await db.get(SystemSetting, "hub")
    value = {"public_key": data.public_key, "endpoint": data.endpoint}
    if row is None:
        db.add(SystemSetting(key="hub", value=value, updated_at=utcnow()))
    else:
        row.value = value
        row.updated_at = utcnow()
    await db.commit()
    s = get_settings()
    return {"address": f"{s.wg_hub_ip}/{s.wg_net.prefixlen}", "listen_port": s.wg_hub_port, "network": s.wg_network}


@router.get("/peers", response_model=list[HubPeer], dependencies=[Depends(hub_auth)])
async def hub_peers(db: AsyncSession = Depends(get_db)) -> list[HubPeer]:
    rows = await db.execute(
        select(Device).where(Device.pairing_status == PairingStatus.paired, Device.wg_public_key.is_not(None))
    )
    return [HubPeer(public_key=d.wg_public_key, allowed_ips=f"{d.tunnel_ip}/32", device_id=d.id) for d in rows.scalars()]


@router.post("/stats", dependencies=[Depends(hub_auth)])
async def hub_stats(stats: list[HubPeerStatIn], db: AsyncSession = Depends(get_db)) -> dict:
    now = utcnow()
    by_key = {d.wg_public_key: d for d in (await db.execute(select(Device).where(Device.wg_public_key.is_not(None)))).scalars()}
    for st in stats:
        hs = dt.datetime.fromtimestamp(st.latest_handshake, dt.UTC) if st.latest_handshake else None
        row = await db.get(HubPeerStat, st.public_key)
        if row is None:
            row = HubPeerStat(public_key=st.public_key)
            db.add(row)
        row.endpoint, row.latest_handshake, row.rx_bytes, row.tx_bytes, row.updated_at = st.endpoint, hs, st.rx_bytes, st.tx_bytes, now
        dev = by_key.get(st.public_key)
        if dev is not None and hs is not None:
            dev.last_handshake_at = hs
    await db.commit()
    return {"ok": True, "count": len(stats)}
