"""Inventar (Phase 25): Kaufdatum, Garantie, Lieferant, Notizen je Gerät; EOL-Liste (Seed + MSP) mit Warnungen."""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, DeviceInventory, EolModel, Site

WARRANTY_WARN_DAYS = 60
EOL_STATUS = {"end_of_sale": "nicht mehr lieferbar", "eol": "abgekündigt (End of Life)"}


def norm_model(m: str | None) -> str:
    return "".join(str(m or "").lower().split())


def eol_out(e: EolModel) -> dict[str, Any]:
    return {"id": str(e.id), "model": e.model, "status": e.status, "since": e.since, "successor": e.successor, "note": e.note,
            "link": e.link, "enabled": e.enabled, "builtin": e.builtin}


def warnings(device: Device, inv: DeviceInventory | None, eol: EolModel | None, today: dt.date | None = None) -> list[dict[str, str]]:
    today = today or dt.date.today()
    out = []
    if eol is not None:
        out.append({"type": "eol", "tone": "orange" if eol.status == "end_of_sale" else "red",
                    "text": f"Modell {EOL_STATUS.get(eol.status, eol.status)}" + (f" – Nachfolger {eol.successor}" if eol.successor else "")})
    if inv is not None and inv.warranty_until is not None:
        if inv.warranty_until < today:
            out.append({"type": "warranty_expired", "tone": "red", "text": f"Garantie abgelaufen ({inv.warranty_until.isoformat()})"})
        elif (inv.warranty_until - today).days <= WARRANTY_WARN_DAYS:
            out.append({"type": "warranty_soon", "tone": "orange", "text": f"Garantie endet am {inv.warranty_until.isoformat()}"})
    return out


async def rows(db: AsyncSession) -> list[dict[str, Any]]:
    devs = (await db.execute(select(Device).order_by(Device.name))).scalars().all()
    inv = {i.device_id: i for i in (await db.execute(select(DeviceInventory))).scalars()}
    eols = {norm_model(e.model): e for e in (await db.execute(select(EolModel).where(EolModel.enabled.is_(True)))).scalars()}
    sites = {s.id: s.name for s in (await db.execute(select(Site))).scalars()}
    out = []
    for d in devs:
        i = inv.get(d.id)
        e = eols.get(norm_model(d.model)) if d.model else None
        out.append({"device_id": str(d.id), "device": d.name, "serial": d.serial, "model": d.model, "site": sites.get(d.site_id),
                    "routeros_version": d.routeros_version, "status": d.status.value,
                    "purchase_date": i.purchase_date if i else None, "warranty_until": i.warranty_until if i else None,
                    "supplier": i.supplier if i else None, "notes": i.notes if i else None,
                    "eol": eol_out(e) if e else None, "warnings": warnings(d, i, e)})
    return out


def to_csv(items: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    buf.write("﻿")  # Excel erkennt UTF-8
    w = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    w.writerow(["Gerät", "Seriennummer", "Modell", "Standort", "RouterOS", "Kaufdatum", "Garantie bis", "Lieferant", "Notizen", "EOL", "Hinweise"])
    for r in items:
        w.writerow([r["device"], r["serial"] or "", r["model"] or "", r["site"] or "", r["routeros_version"] or "",
                    r["purchase_date"].isoformat() if r["purchase_date"] else "", r["warranty_until"].isoformat() if r["warranty_until"] else "",
                    r["supplier"] or "", (r["notes"] or "").replace("\n", " "), EOL_STATUS.get(r["eol"]["status"], "") if r["eol"] else "",
                    " | ".join(x["text"] for x in r["warnings"])])
    return buf.getvalue()
