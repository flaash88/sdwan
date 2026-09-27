"""ZTP-Massenimport (Phase 25): CSV → Vorschau mit Prüfung je Zeile → Anlegen nur gültiger Zeilen nach Bestätigung.

Spalten (Kopfzeile Pflicht, Trennzeichen ``;`` oder ``,``): ``name``, ``serial`` (Pflicht), ``model`` (nur Hinweis,
wird mit dem Gerät beim Pairing verglichen), ``site`` (Standortname), ``template`` (Vorlagenname), ``tags``
(durch ``|`` getrennt), ``vrrp_local_address``. Angelegt wird über dieselbe Logik wie „Geräte vorbereiten“.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import utcnow
from app.models import Device, ProvisioningTemplate, Site
from app.routeros.naming import validate_label
from app.services.pairing import issue_pairing_token
from app.services.wireguard import allocate_tunnel_ip
from app.services.ztp import bootstrap_script

COLUMNS = ("name", "serial", "model", "site", "template", "tags", "vrrp_local_address")
REQUIRED = ("name", "serial")
MAX_ROWS = 500
SERIAL_RE = re.compile(r"^[A-Za-z0-9\-]{3,64}\Z")


class ImportError_(ValueError):
    pass


def read_csv(text: str) -> list[dict[str, str]]:
    text = text.lstrip("﻿").strip()
    if not text:
        raise ImportError_("CSV ist leer")
    first = text.splitlines()[0]
    delim = ";" if first.count(";") >= first.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    head = [str(h or "").strip().lower() for h in reader.fieldnames or []]
    missing = [c for c in REQUIRED if c not in head]
    if missing:
        raise ImportError_(f"Kopfzeile: Spalte(n) fehlen: {', '.join(missing)} (erlaubt: {', '.join(COLUMNS)})")
    unknown = [h for h in head if h and h not in COLUMNS]
    if unknown:
        raise ImportError_(f"Kopfzeile: unbekannte Spalte(n): {', '.join(unknown)} (erlaubt: {', '.join(COLUMNS)})")
    reader.fieldnames = head
    rows = [{k: str(v or "").strip() for k, v in r.items() if k} for r in reader]
    rows = [r for r in rows if any(r.values())]
    if len(rows) > MAX_ROWS:
        raise ImportError_(f"Höchstens {MAX_ROWS} Zeilen je Import")
    return rows


async def validate(db: AsyncSession, tenant_id: uuid.UUID, text: str) -> list[dict[str, Any]]:
    rows = read_csv(text)
    sites = {s.name.strip().lower(): s for s in (await db.execute(select(Site))).scalars()}
    tpls = {t.name.strip().lower(): t for t in (await db.execute(select(ProvisioningTemplate))).scalars()}
    serials = [r.get("serial", "").upper() for r in rows]
    existing = set((await db.execute(select(func.upper(Device.serial)).where(func.upper(Device.serial).in_([s for s in serials if s]))
                                     .execution_options(skip_tenant_filter=True))).scalars())
    names = set((await db.execute(select(func.lower(Device.name)))).scalars())
    seen_serial: dict[str, int] = {}
    seen_name: dict[str, int] = {}
    out = []
    for i, r in enumerate(rows, start=2):  # Zeile 1 = Kopfzeile
        errs: list[str] = []
        name, serial = r.get("name", ""), r.get("serial", "").upper()
        if not name:
            errs.append("Name fehlt")
        else:
            try:
                validate_label(name, "Name", 200)
            except ValueError as exc:
                errs.append(str(exc))
        if not serial:
            errs.append("Seriennummer fehlt")
        elif not SERIAL_RE.match(serial):
            errs.append("Seriennummer: 3–64 Zeichen, nur Buchstaben, Ziffern und -")
        elif serial in existing:
            errs.append("Seriennummer bereits registriert")
        elif serial in seen_serial:
            errs.append(f"Seriennummer doppelt (Zeile {seen_serial[serial]})")
        if name and name.lower() in names:
            errs.append("Gerätename existiert bereits")
        elif name and name.lower() in seen_name:
            errs.append(f"Gerätename doppelt (Zeile {seen_name[name.lower()]})")
        site = sites.get(r.get("site", "").lower()) if r.get("site") else None
        if r.get("site") and site is None:
            errs.append(f"Standort „{r['site']}“ unbekannt")
        tpl = tpls.get(r.get("template", "").lower()) if r.get("template") else None
        if r.get("template") and tpl is None:
            errs.append(f"Vorlage „{r['template']}“ unbekannt")
        vla = r.get("vrrp_local_address") or None
        if vla and tpl is not None and (tpl.content or {}).get("vrrp"):
            from app.services.vrrp import VrrpError, validate_set

            try:
                validate_set([{**x, "local_address": x.get("local_address") or vla} for x in tpl.content["vrrp"]])
            except VrrpError as exc:
                errs.append(f"VRRP: {exc}")
        tags = [t.strip() for t in re.split(r"[|]", r.get("tags", "")) if t.strip()]
        if serial and serial not in seen_serial:
            seen_serial[serial] = i
        if name and name.lower() not in seen_name:
            seen_name[name.lower()] = i
        out.append({"line": i, "name": name, "serial": serial, "model": r.get("model") or None, "site": site.name if site else r.get("site") or None,
                    "site_id": str(site.id) if site else None, "template": tpl.name if tpl else r.get("template") or None,
                    "template_id": str(tpl.id) if tpl else None, "tags": tags, "vrrp_local_address": vla, "errors": errs, "ok": not errs})
    return out


async def stage_device(db: AsyncSession, tenant_id: uuid.UUID, *, name: str, serial: str, site_id: uuid.UUID | None, tags: list[str],
                       template: ProvisioningTemplate | None, ttl_days: int, by: str, vrrp_local_address: str | None = None,
                       model: str | None = None) -> dict[str, Any]:
    """Ein Gerät für den Versand anlegen (seriengebundener Token + Bootstrap-Script) – gemeinsame Logik für
    „Geräte vorbereiten“ und den CSV-Import."""
    from app.schemas import DeviceOut

    facts: dict[str, Any] = {}
    if vrrp_local_address:
        facts["ztp_vrrp_local_address"] = vrrp_local_address
    if model:
        facts["ztp_expected_model"] = model
    dev = Device(tenant_id=tenant_id, name=name, serial=serial.upper(), site_id=site_id, tags=tags,
                 tunnel_ip=await allocate_tunnel_ip(db), ztp_template_id=template.id if template else None,
                 ztp_state="staged", ztp_log=[{"at": utcnow().isoformat(), "state": "staged", "msg": f"Vorbereitet von {by}"}], facts=facts)
    db.add(dev)
    info = issue_pairing_token(dev, ttl_hours=ttl_days * 24)
    await db.flush()
    return {"device": DeviceOut.model_validate(dev).model_dump(mode="json"), "token": info.token, "expires_at": info.expires_at,
            "command": info.command, "bootstrap_script": bootstrap_script(info.token, dev, template), "min_routeros": info.min_routeros}
