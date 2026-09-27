"""Firewall-Editor (Phase 14): Objekte, Dienste, Zonen, Bausteine, Zonen je Gerät, Trefferzähler."""

from __future__ import annotations

import copy
import re
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.v1.common import get_or_404
from app.db import utcnow
from app.deps import Ctx, ReadCtx, TechCtx
from app.models import Device, DeviceZoneMember, FirewallPolicy, FwBlock, FwDefconfDisabled, FwObject, FwRuleHit, FwService, FwZone, PolicyVersion
from app.routeros import RouterOSError, connect_device
from app.services.fw_compile import (
    Catalog,
    SpecError,
    compile_spec,
    new_id,
    referenced,
    slugify,
    validate_entries,
    validate_object,
)

router = APIRouter(tags=["firewall"])

KINDS: dict[str, Any] = {"objects": FwObject, "services": FwService, "zones": FwZone, "blocks": FwBlock}
_IFACE = re.compile(r"^[A-Za-z0-9._\-]{1,64}\Z")


# ----------------------------------------------------------------------------- Ausgabe
def _base(r: Any) -> dict[str, Any]:
    return {"id": str(r.id), "name": r.name, "scope": "global" if r.tenant_id is None else "tenant",
            "builtin": r.builtin, "description": getattr(r, "description", None)}


def _out(kind: str, r: Any) -> dict[str, Any]:
    o = _base(r)
    if kind == "objects":
        o.update(slug=r.slug, kind=r.kind, values=r.values or [], members=r.members or [])
    elif kind == "services":
        o.update(slug=r.slug, entries=r.entries or [], members=r.members or [])
    elif kind == "zones":
        o.update(slug=r.slug, source=r.source, management=r.management)
    else:
        o.update(params=r.params or [], rules=r.rules or [], nat=r.nat or [])
    return o


async def catalog(ctx: Ctx, tenant_id: uuid.UUID | None = None) -> Catalog:
    """Für eine Policy sichtbare Einträge: global + Mandant der Policy (MSP ohne Mandant: alle)."""
    rows = {}
    for kind in ("objects", "services", "zones"):
        m = KINDS[kind]
        q = select(m)
        items = (await ctx.db.execute(q)).scalars().all()
        rows[kind] = [r for r in items if r.tenant_id is None or tenant_id is None or r.tenant_id == tenant_id]
    return Catalog.from_rows(rows["objects"], rows["services"], rows["zones"])


def check_scope(spec: dict[str, Any], cat: Catalog, policy_tenant: uuid.UUID | None) -> None:
    """Globale Policies dürfen nur globale Objekte nutzen; Mandanten-Policies globale oder eigene."""
    refs = referenced(spec)
    for kind, ids in (("objects", refs["objects"]), ("services", refs["services"]), ("zones", refs["zones"])):
        pool = getattr(cat, kind)
        for i in ids:
            item = pool.get(i)
            if item is None:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Nicht gefunden oder nicht sichtbar: {kind} {i}")
            if item["tenant_id"] and (policy_tenant is None or item["tenant_id"] != str(policy_tenant)):
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{item['name']} gehört einem Mandanten – in globalen Policies nicht nutzbar")


# ----------------------------------------------------------------------------- Katalog
@router.get("/fw/catalog")
async def get_catalog(ctx: Ctx = ReadCtx) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for kind, m in KINDS.items():
        rows = (await ctx.db.execute(select(m).order_by(m.name))).scalars().all()
        out[kind] = [_out(kind, r) for r in rows]
    return out


class ItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    kind: str | None = None  # objects
    values: list[str] = []
    members: list[str] = []
    entries: list[dict[str, Any]] = []  # services
    source: str = "manual"  # zones
    management: bool = False
    params: list[dict[str, Any]] = []  # blocks
    rules: list[dict[str, Any]] = []
    nat: list[dict[str, Any]] = []


async def _validated(ctx: Ctx, kind: str, data: ItemIn, own_id: uuid.UUID | None = None) -> dict[str, Any]:
    f: dict[str, Any] = {"name": data.name.strip(), "description": data.description}
    try:
        if kind == "objects":
            if data.kind not in ("host", "network", "range", "group", "feed"):
                raise SpecError("Typ: host, network, range, group oder feed")
            if data.kind == "feed":
                raise SpecError("Threat-Feed-Objekte entstehen automatisch aus den Threat-Feeds")
            f.update(kind=data.kind, slug=slugify(data.name))
            if data.kind == "group":
                for m in data.members:
                    if own_id and m == str(own_id):
                        raise SpecError("Gruppe kann sich nicht selbst enthalten")
                    if await ctx.db.get(FwObject, uuid.UUID(m)) is None:
                        raise SpecError(f"Objekt {m} nicht gefunden")
                f.update(members=list(dict.fromkeys(data.members)), values=[])
            else:
                if data.kind == "host" and len(data.values) != 1:
                    raise SpecError("Host: genau eine Adresse")
                f.update(values=validate_object(data.kind, data.values), members=[])
        elif kind == "services":
            for m in data.members:
                if await ctx.db.get(FwService, uuid.UUID(m)) is None:
                    raise SpecError(f"Dienst {m} nicht gefunden")
            if not data.entries and not data.members:
                raise SpecError("Dienst braucht mindestens ein Protokoll oder Mitglieder")
            f.update(slug=slugify(data.name), entries=validate_entries(data.entries), members=list(dict.fromkeys(data.members)))
        elif kind == "zones":
            if data.source not in ("manual", "wan"):
                raise SpecError("Quelle: manual oder wan")
            f.update(slug=slugify(data.name), source=data.source, management=data.management)
        else:
            for p in data.params:
                if p.get("type") not in ("zone", "object", "service") or not re.match(r"^[a-z_]{1,30}\Z", str(p.get("key", ""))):
                    raise SpecError("Parameter: key (a-z_) und type zone|object|service")
            f.update(params=data.params, rules=data.rules, nat=data.nat)
    except (SpecError, ValueError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return f


def _require_kind(kind: str) -> Any:
    if kind not in KINDS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unbekannter Typ")
    return KINDS[kind]


def _check_write(ctx: Ctx, kind: str, row: Any | None = None) -> None:
    from app.models import ROLE_RANK, Role

    if kind == "blocks" and ROLE_RANK[ctx.role] < ROLE_RANK[Role.admin]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Bausteine pflegen nur Admins")
    if row is not None:
        if row.builtin:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Vordefinierter Eintrag – bitte kopieren und die Kopie anpassen")
        if row.tenant_id is None and not ctx.is_superuser:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Globale Einträge pflegt nur der MSP")


@router.post("/fw/{kind}", status_code=201)
async def create_item(kind: str, data: ItemIn, ctx: Ctx = TechCtx) -> dict[str, Any]:
    model = _require_kind(kind)
    _check_write(ctx, kind)
    f = await _validated(ctx, kind, data)
    row = model(tenant_id=ctx.tenant_id, builtin=False, **f)  # MSP ohne Mandant -> global
    ctx.db.add(row)
    await ctx.db.flush()
    await ctx.audit(f"fw.{kind}.create", target_type=f"fw_{kind}", target_id=row.id, details={"name": row.name})
    await ctx.db.commit()
    return _out(kind, row)


@router.patch("/fw/{kind}/{item_id}")
async def update_item(kind: str, item_id: uuid.UUID, data: ItemIn, ctx: Ctx = TechCtx) -> dict[str, Any]:
    model = _require_kind(kind)
    row = await get_or_404(ctx.db, model, item_id, "Eintrag")
    _check_write(ctx, kind, row)
    f = await _validated(ctx, kind, data, own_id=row.id)
    for k, v in f.items():
        setattr(row, k, v)
    await ctx.db.flush()
    recompiled = await recompile_referencing(ctx, kind, str(row.id)) if kind != "blocks" else []
    await ctx.audit(f"fw.{kind}.update", target_type=f"fw_{kind}", target_id=row.id, details={"name": row.name, "recompiled": recompiled})
    await ctx.db.commit()
    return {**_out(kind, row), "recompiled_policies": recompiled}


@router.delete("/fw/{kind}/{item_id}", status_code=204, response_model=None)
async def delete_item(kind: str, item_id: uuid.UUID, ctx: Ctx = TechCtx) -> None:
    model = _require_kind(kind)
    row = await get_or_404(ctx.db, model, item_id, "Eintrag")
    _check_write(ctx, kind, row)
    if kind != "blocks":
        users = await _referencing(ctx, kind, str(row.id))
        if users:
            raise HTTPException(status.HTTP_409_CONFLICT, f"Wird verwendet von: {', '.join(p.name for p in users)}")
        if kind in ("objects", "services"):
            groups = [g for g in (await ctx.db.execute(select(model))).scalars() if str(row.id) in (g.members or [])]
            if groups:
                raise HTTPException(status.HTTP_409_CONFLICT, f"Mitglied in: {', '.join(g.name for g in groups)}")
        if kind == "zones" and (await ctx.db.execute(select(DeviceZoneMember).where(DeviceZoneMember.zone_id == row.id).limit(1))).first():
            raise HTTPException(status.HTTP_409_CONFLICT, "Zone ist Interfaces auf Geräten zugeordnet")
    await ctx.audit(f"fw.{kind}.delete", target_type=f"fw_{kind}", target_id=row.id, details={"name": row.name})
    await ctx.db.delete(row)
    await ctx.db.commit()


@router.post("/fw/{kind}/{item_id}/copy", status_code=201)
async def copy_item(kind: str, item_id: uuid.UUID, ctx: Ctx = TechCtx) -> dict[str, Any]:
    model = _require_kind(kind)
    _check_write(ctx, kind)
    src = await get_or_404(ctx.db, model, item_id, "Eintrag")
    cols = {c.key for c in model.__table__.columns} - {"id", "tenant_id", "created_at", "builtin", "seed_key"}
    f = {c: copy.deepcopy(getattr(src, c)) for c in cols}
    f["name"] = f"{src.name} (Kopie)"
    if "slug" in f:
        f["slug"] = slugify(f["name"])
    row = model(tenant_id=ctx.tenant_id, builtin=False, **f)
    ctx.db.add(row)
    await ctx.db.flush()
    await ctx.audit(f"fw.{kind}.copy", target_type=f"fw_{kind}", target_id=row.id, details={"from": str(src.id)})
    await ctx.db.commit()
    return _out(kind, row)


async def _referencing(ctx: Ctx, kind: str, item_id: str) -> list[FirewallPolicy]:
    key = {"objects": "objects", "services": "services", "zones": "zones"}[kind]
    pols = (await ctx.db.execute(select(FirewallPolicy).where(FirewallPolicy.mode == "simple"))).scalars().all()
    return [p for p in pols if item_id in referenced(p.spec or {})[key]]


async def recompile_referencing(ctx: Ctx, kind: str, item_id: str) -> list[str]:
    """Geänderte Objekte -> betroffene einfache Policies neu kompilieren (neue Version, KEIN automatisches Deploy)."""
    done = []
    pols = await _referencing(ctx, kind, item_id)
    if kind in ("objects", "services"):  # auch über Gruppen referenziert
        model = KINDS[kind]
        groups = {str(g.id) for g in (await ctx.db.execute(select(model))).scalars() if item_id in (g.members or [])}
        for g in groups:
            pols += [p for p in await _referencing(ctx, kind, g) if p not in pols]
    for p in pols:
        try:
            content = compile_and_validate(p.spec or {}, await catalog(ctx, p.tenant_id))
        except HTTPException:
            continue  # Policy bleibt auf der alten Version; Lint zeigt den Fehler
        if content != p.content:
            p.version += 1
            p.content, p.updated_at = content, utcnow()
            ctx.db.add(PolicyVersion(policy_id=p.id, version=p.version, content=content, spec=p.spec,
                                     note=f"Neu kompiliert (geändert: {kind})", created_by=ctx.user.email))
            done.append(p.name)
    return done


def compile_and_validate(spec: dict[str, Any], cat: Catalog) -> dict[str, Any]:
    from app.services.policy import PolicyError, validate_content

    try:
        return validate_content(compile_spec(spec, cat))
    except (SpecError, PolicyError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


# ----------------------------------------------------------------------------- Bausteine einfügen
class ExpandIn(BaseModel):
    params: dict[str, Any] = {}


@router.post("/fw/blocks/{block_id}/expand")
async def expand_block(block_id: uuid.UUID, data: ExpandIn, ctx: Ctx = ReadCtx) -> dict[str, Any]:
    """Baustein mit Parametern → Regeln/NAT (neue IDs) zum Einfügen in den Editor-Entwurf."""
    b = await get_or_404(ctx.db, FwBlock, block_id, "Baustein")
    cat = await catalog(ctx, ctx.tenant_id)
    by_slug = {
        "zone": {z["slug"]: zid for zid, z in cat.zones.items()},
        "svc": {s["slug"]: sid for sid, s in cat.services.items()},
        "obj": {o["slug"]: oid for oid, o in cat.objects.items()},
    }
    seed_keys = {}
    for kind, m in (("zone", FwZone), ("svc", FwService), ("obj", FwObject)):
        for r in (await ctx.db.execute(select(m).where(m.seed_key.is_not(None)))).scalars():
            seed_keys.setdefault(kind, {})[r.seed_key.split("-", 1)[1]] = str(r.id)
    params: dict[str, Any] = {}
    for p in b.params or []:
        v = data.params.get(p["key"])
        if v in (None, "", []) and p.get("default"):
            v = p["default"]
        if v in (None, "", []) and not p.get("optional"):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Parameter „{p.get('label', p['key'])}“ fehlt")
        params[p["key"]] = v

    def resolve(v: Any) -> Any:
        if isinstance(v, list):
            out: list[Any] = []
            for x in v:
                r = resolve(x)
                out += r if isinstance(r, list) else ([r] if r not in (None, "") else [])
            return out
        if not isinstance(v, str):
            return v
        if v.startswith("$"):
            return resolve(params.get(v[1:]))
        m = re.match(r"^(zone|svc|obj):([a-z0-9\-]+)\Z", v)
        if m:
            kind, slug = m.groups()
            found = seed_keys.get(kind, {}).get(slug) or by_slug[kind].get(slug)
            if not found:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Baustein verweist auf {v}, nicht vorhanden")
            return found
        return v

    rules = []
    for r in b.rules or []:
        rr = {k: resolve(v) for k, v in r.items()}
        rr.update(id=new_id(), enabled=True, block=b.name)
        for k in ("src", "dst", "services"):
            rr[k] = rr.get(k) or []
        rules.append(rr)
    nat = [{**{k: resolve(v) for k, v in n.items()}, "id": new_id(), "enabled": True} for n in b.nat or []]
    return {"rules": rules, "nat": nat, "block": b.name}


# ----------------------------------------------------------------------------- Zonen je Gerät
class ZoneMemberIn(BaseModel):
    interface: str
    zone_id: uuid.UUID


class DeviceZonesIn(BaseModel):
    members: list[ZoneMemberIn] = []
    apply: bool = True


@router.get("/devices/{device_id}/zones")
async def get_device_zones(device_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict[str, Any]:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    rows = (await ctx.db.execute(select(DeviceZoneMember).where(DeviceZoneMember.device_id == dev.id))).scalars().all()
    return {"members": [{"interface": m.interface, "zone_id": str(m.zone_id)} for m in rows],
            "last_error": (dev.facts or {}).get("zones_last_error")}


async def set_device_zones(db: Any, dev: Device, members: list[tuple[str, uuid.UUID]]) -> None:
    """Zuordnung speichern (auch aus ZTP genutzt)."""
    for m in (await db.execute(select(DeviceZoneMember).where(DeviceZoneMember.device_id == dev.id))).scalars().all():
        await db.delete(m)
    await db.flush()
    for iface, zid in members:
        db.add(DeviceZoneMember(tenant_id=dev.tenant_id, device_id=dev.id, zone_id=zid, interface=iface))
    await db.flush()


async def apply_device_zones(db: Any, dev: Device) -> dict[str, Any]:
    from app.services.zones import device_zone_config, push_zones

    cfg = await device_zone_config(db, dev)
    try:
        async with connect_device(dev) as api:
            stats = await push_zones(api, cfg)
        dev.facts = {**(dev.facts or {}), "zones_last_error": None}
        return {"ok": True, "stats": stats}
    except RouterOSError as exc:
        dev.facts = {**(dev.facts or {}), "zones_last_error": str(exc)}
        return {"ok": False, "error": str(exc)}


@router.put("/devices/{device_id}/zones")
async def put_device_zones(device_id: uuid.UUID, data: DeviceZonesIn, ctx: Ctx = TechCtx) -> dict[str, Any]:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    seen: set[str] = set()
    pairs = []
    for m in data.members:
        if not _IFACE.match(m.interface):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Ungültiges Interface {m.interface!r}")
        if m.interface in seen:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Interface {m.interface} ist mehrfach zugeordnet")
        z = await ctx.db.get(FwZone, m.zone_id)
        if z is None or (z.tenant_id is not None and z.tenant_id != dev.tenant_id):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Zone nicht gefunden")
        if z.source == "wan":
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Zone {z.name} folgt der WAN-Konfiguration und wird nicht zugeordnet")
        seen.add(m.interface)
        pairs.append((m.interface, m.zone_id))
    await set_device_zones(ctx.db, dev, pairs)
    result = await apply_device_zones(ctx.db, dev) if data.apply else None
    await ctx.audit("fw.zones.update", target_type="device", target_id=dev.id,
                    details={"members": [{"interface": i, "zone_id": str(z)} for i, z in pairs], "applied": bool(result and result["ok"])})
    await ctx.db.commit()
    return {"members": [{"interface": i, "zone_id": str(z)} for i, z in pairs], "apply": result}


@router.get("/devices/{device_id}/zones/suggestions")
async def zone_suggestions(device_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict[str, Any]:
    """Vorschlag aus der Werkskonfiguration: Mitglieder der defconf-Interface-Lists WAN/LAN → Zonen WAN/LAN."""
    from app.services.fw_defconf import zone_suggestions as read_suggestions

    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    try:
        async with connect_device(dev) as api:
            found = await read_suggestions(api)
    except RouterOSError as exc:
        return {"suggestions": [], "error": str(exc)}
    zones = [z for z in (await ctx.db.execute(select(FwZone))).scalars() if z.tenant_id in (None, dev.tenant_id)]
    out = []
    for f in found:
        # eigene Zone des Mandanten vor globaler gleichen Kürzels
        z = next((z for z in sorted(zones, key=lambda z: z.tenant_id is None) if z.slug == f["zone_slug"]), None)
        if z is None or not f["interfaces"]:
            continue
        out.append({**f, "zone_id": str(z.id), "zone_name": z.name, "applicable": z.source != "wan",
                    "note": "Zone folgt der WAN-Konfiguration der Plattform – nur zur Kontrolle" if z.source == "wan" else None})
    return {"suggestions": out, "error": None}


# ----------------------------------------------------------------------------- Werks-Firewall (defconf)
@router.get("/devices/{device_id}/firewall/defconf")
async def defconf_state(device_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict[str, Any]:
    """Von der Plattform deaktivierte defconf-Regeln (gemerkt) und aktuell aktive defconf-Regeln (live)."""
    from app.services.fw_defconf import active_defconf

    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    rows = (await ctx.db.execute(select(FwDefconfDisabled).where(FwDefconfDisabled.device_id == dev.id))).scalars().all()
    active, error = None, None
    try:
        async with connect_device(dev) as api:
            active = await active_defconf(api)
    except RouterOSError as exc:
        error = str(exc)
    return {"disabled": [{"rule_id": r.rule_id, "chain": r.chain, "action": r.action, "comment": r.comment, "disabled_at": r.created_at,
                          "ambiguous": r.status == "ambiguous"}
                         for r in rows], "active": active, "error": error}


@router.post("/devices/{device_id}/firewall/defconf/restore")
async def defconf_restore(device_id: uuid.UUID, ctx: Ctx = TechCtx) -> dict[str, Any]:
    """Nur die von der Plattform deaktivierten defconf-Regeln wieder aktivieren (liegen dann hinter dem Default-Drop)."""
    from app.services.fw_defconf import apply_result, enable, records, remembered_rows

    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    rows = await remembered_rows(ctx.db, dev.id)
    if not rows:
        return {"enabled": [], "missing": [], "already_active": [], "ambiguous": [], "relocated": {}}
    try:
        async with connect_device(dev) as api:
            res = await enable(api, records(rows))
    except RouterOSError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await apply_result(ctx.db, dev.id, res)
    await ctx.audit("fw.defconf.restore", target_type="device", target_id=dev.id, details=res)
    await ctx.db.commit()
    return res


# ----------------------------------------------------------------------------- Trefferzähler
@router.get("/policies/{policy_id}/hits")
async def policy_hits(policy_id: uuid.UUID, ctx: Ctx = ReadCtx) -> dict[str, Any]:
    from app.services.policy import _tag

    p = await get_or_404(ctx.db, FirewallPolicy, policy_id, "Policy")
    tag = _tag(p.id)
    rows = (await ctx.db.execute(select(FwRuleHit).where(FwRuleHit.key.like(f"{tag}:%")))).scalars().all()
    out: dict[str, dict[str, Any]] = {}
    for h in rows:
        rid = h.key.split(":", 1)[1]
        o = out.setdefault(rid, {"packets": 0, "bytes": 0, "last_hit_at": None, "tracking_since": h.created_at, "devices": 0})
        o["packets"] += h.packets or 0
        o["bytes"] += h.bytes or 0
        o["devices"] += 1
        if h.last_hit_at and (o["last_hit_at"] is None or h.last_hit_at > o["last_hit_at"]):
            o["last_hit_at"] = h.last_hit_at
        if h.created_at and h.created_at < o["tracking_since"]:
            o["tracking_since"] = h.created_at
    return {"rules": out}


@router.post("/devices/{device_id}/firewall/reset-counters")
async def reset_counters(device_id: uuid.UUID, ctx: Ctx = TechCtx) -> dict[str, Any]:
    """Zähler der verwalteten Regeln zurücksetzen (nur ``sdwan:fw:``-Regeln, manuelle bleiben unberührt)."""
    from app.services.fw_hits import PATHS

    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    n = 0
    try:
        async with connect_device(dev) as api:
            for path in PATHS:
                for r in await api.print(path):
                    if str(r.get("comment", "")).startswith("sdwan:fw:"):
                        # ANNAHME (Labor): /ip/firewall/filter/reset-counters mit .id setzt die Zähler dieser Regel zurück
                        await api.call(f"{path}/reset-counters", **{".id": r[".id"]})
                        n += 1
    except RouterOSError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Router nicht erreichbar: {exc}") from exc
    for h in (await ctx.db.execute(select(FwRuleHit).where(FwRuleHit.device_id == dev.id))).scalars():
        h.packets, h.bytes = 0, 0
    await ctx.audit("fw.reset_counters", target_type="device", target_id=dev.id, details={"rules": n})
    await ctx.db.commit()
    return {"reset": n}

