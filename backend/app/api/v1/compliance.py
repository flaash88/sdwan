"""Compliance (Regelsets, Zuweisungen, Auswertung, Flottenbericht) und Config-Suche (Phase 16)."""

from __future__ import annotations

import csv
import datetime as dt
import io
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.v1.common import get_or_404
from app.db import utcnow
from app.deps import AdminCtx, Ctx, ReadCtx, TechCtx
from app.models import ComplianceAssignment, ComplianceResult, ComplianceRuleSet, Device
from app.services.compliance import ComplianceError, evaluate_device, latest_results, search_backups, validate_rules
from app.services.targets import resolve_targets

router = APIRouter(tags=["compliance"])


class RuleSetIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    rules: list[dict[str, Any]] = []


class TargetsIn(BaseModel):
    device_ids: list[uuid.UUID] = []
    site_ids: list[uuid.UUID] = []
    tags: list[str] = []


def _rs_out(rs: ComplianceRuleSet) -> dict[str, Any]:
    return {"id": str(rs.id), "name": rs.name, "description": rs.description, "rules": rs.rules or [], "builtin": rs.builtin,
            "scope": "global" if rs.tenant_id is None else "tenant"}


def _can_edit(ctx: Ctx, rs: ComplianceRuleSet) -> None:
    if rs.builtin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Vordefiniertes Regelset – bitte kopieren")
    if rs.tenant_id is None and not ctx.is_superuser:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Globale Regelsets pflegt nur der MSP")


def _rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        return validate_rules(rules)
    except ComplianceError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


# ----------------------------------------------------------------------------- Regelsets
@router.get("/compliance/rule-sets")
async def list_sets(ctx: Ctx = ReadCtx) -> list[dict[str, Any]]:
    rows = (await ctx.db.execute(select(ComplianceRuleSet).order_by(ComplianceRuleSet.name))).scalars().all()
    assigns = (await ctx.db.execute(select(ComplianceAssignment))).scalars().all()
    out = []
    for rs in rows:
        o = _rs_out(rs)
        o["assignments"] = [{"id": str(a.id), "targets": a.targets} for a in assigns if a.rule_set_id == rs.id]
        out.append(o)
    return out


@router.post("/compliance/rule-sets", status_code=201)
async def create_set(data: RuleSetIn, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    rs = ComplianceRuleSet(tenant_id=ctx.tenant_id, name=data.name, description=data.description, rules=_rules(data.rules), builtin=False)
    ctx.db.add(rs)
    await ctx.db.flush()
    await ctx.audit("compliance.set.create", target_type="compliance_set", target_id=rs.id, details={"name": rs.name})
    await ctx.db.commit()
    return _rs_out(rs)


@router.patch("/compliance/rule-sets/{set_id}")
async def update_set(set_id: uuid.UUID, data: RuleSetIn, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    rs = await get_or_404(ctx.db, ComplianceRuleSet, set_id, "Regelset")
    _can_edit(ctx, rs)
    rs.name, rs.description, rs.rules = data.name, data.description, _rules(data.rules)
    await ctx.audit("compliance.set.update", target_type="compliance_set", target_id=rs.id, details={"name": rs.name})
    await ctx.db.commit()
    return _rs_out(rs)


@router.post("/compliance/rule-sets/{set_id}/copy", status_code=201)
async def copy_set(set_id: uuid.UUID, ctx: Ctx = AdminCtx) -> dict[str, Any]:
    src = await get_or_404(ctx.db, ComplianceRuleSet, set_id, "Regelset")
    rs = ComplianceRuleSet(tenant_id=ctx.tenant_id, name=f"{src.name} (Kopie)", description=src.description, rules=list(src.rules or []), builtin=False)
    ctx.db.add(rs)
    await ctx.db.flush()
    await ctx.audit("compliance.set.copy", target_type="compliance_set", target_id=rs.id, details={"from": str(src.id)})
    await ctx.db.commit()
    return _rs_out(rs)


@router.delete("/compliance/rule-sets/{set_id}", status_code=204, response_model=None)
async def delete_set(set_id: uuid.UUID, ctx: Ctx = AdminCtx) -> None:
    rs = await get_or_404(ctx.db, ComplianceRuleSet, set_id, "Regelset")
    _can_edit(ctx, rs)
    await ctx.audit("compliance.set.delete", target_type="compliance_set", target_id=rs.id, details={"name": rs.name})
    await ctx.db.delete(rs)
    await ctx.db.commit()


@router.post("/compliance/rule-sets/{set_id}/assign", status_code=201)
async def assign(set_id: uuid.UUID, data: TargetsIn, ctx: Ctx = TechCtx) -> dict[str, Any]:
    rs = await get_or_404(ctx.db, ComplianceRuleSet, set_id, "Regelset")
    tenant_id = ctx.require_tenant()
    targets = {k: [str(x) for x in v] for k, v in data.model_dump().items()}
    if not any(targets.values()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Mindestens ein Ziel wählen")
    a = ComplianceAssignment(tenant_id=tenant_id, rule_set_id=rs.id, targets=targets)
    ctx.db.add(a)
    await ctx.db.flush()
    await ctx.audit("compliance.assign", target_type="compliance_set", target_id=rs.id, details=targets)
    await ctx.db.commit()
    return {"id": str(a.id), "targets": targets}


@router.delete("/compliance/assignments/{assignment_id}", status_code=204, response_model=None)
async def unassign(assignment_id: uuid.UUID, ctx: Ctx = TechCtx) -> None:
    a = await get_or_404(ctx.db, ComplianceAssignment, assignment_id, "Zuweisung")
    await ctx.audit("compliance.unassign", target_type="compliance_set", target_id=a.rule_set_id, details=a.targets)
    await ctx.db.delete(a)
    await ctx.db.commit()


# ----------------------------------------------------------------------------- Auswertung & Bericht
@router.post("/compliance/evaluate")
async def evaluate(data: TargetsIn, ctx: Ctx = TechCtx) -> dict[str, Any]:
    """Manuell auswerten (ohne Ziele: alle Geräte des Mandanten mit Zuweisungen)."""
    tenant_id = ctx.require_tenant()
    targets = {k: [str(x) for x in v] for k, v in data.model_dump().items()}
    devices = await resolve_targets(ctx.db, targets, tenant_id) if any(targets.values()) else \
        list((await ctx.db.execute(select(Device).where(Device.tenant_id == tenant_id))).scalars())
    n = 0
    for d in devices:
        n += len(await evaluate_device(ctx.db, d))
    await ctx.audit("compliance.evaluate", details={"devices": len(devices), "results": n})
    await ctx.db.commit()
    return {"devices": len(devices), "results": n}


async def _matrix(ctx: Ctx, set_id: uuid.UUID | None) -> dict[str, Any]:
    sets = {rs.id: rs for rs in (await ctx.db.execute(select(ComplianceRuleSet))).scalars()}
    devices = {d.id: d for d in (await ctx.db.execute(select(Device).order_by(Device.name))).scalars()}
    latest = await latest_results(ctx.db, list(devices))
    rows = []
    for (dev_id, rs_id), r in sorted(latest.items(), key=lambda kv: (devices[kv[0][0]].name, sets[kv[0][1]].name if kv[0][1] in sets else "")):
        if set_id and rs_id != set_id or rs_id not in sets:
            continue
        rows.append({"device_id": str(dev_id), "device": devices[dev_id].name, "rule_set_id": str(rs_id), "rule_set": sets[rs_id].name,
                     "evaluated_at": r.evaluated_at, "passed": r.passed, "failed": r.failed, "unknown": r.unknown,
                     "cells": {x["rule_id"]: {"status": x["status"], "detail": x["detail"]} for x in r.results}})
    rule_cols: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rs in sets.values():
        if set_id and rs.id != set_id:
            continue
        if not any(r["rule_set_id"] == str(rs.id) for r in rows):
            continue
        for rule in rs.rules or []:
            key = (str(rs.id), rule["id"])
            if key not in seen:
                seen.add(key)
                rule_cols.append({"rule_set_id": str(rs.id), "rule_id": rule["id"], "name": rule["name"]})
    return {"rules": rule_cols, "rows": rows}


@router.get("/compliance/report")
async def report(ctx: Ctx = ReadCtx, rule_set_id: uuid.UUID | None = None) -> dict[str, Any]:
    return await _matrix(ctx, rule_set_id)


@router.get("/compliance/report.csv")
async def report_csv(ctx: Ctx = ReadCtx, rule_set_id: uuid.UUID | None = None) -> Response:
    m = await _matrix(ctx, rule_set_id)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Gerät", "Regelset", "Ausgewertet", "Bestanden", "Verletzt", "Unbekannt", *[c["name"] for c in m["rules"]]])
    label = {"ok": "ok", "warn": "Warnung", "fail": "VERLETZT", "unknown": "unbekannt"}
    for r in m["rows"]:
        cells = [label.get(r["cells"].get(c["rule_id"], {}).get("status", ""), "") if c["rule_set_id"] == r["rule_set_id"] else "" for c in m["rules"]]
        w.writerow([r["device"], r["rule_set"], r["evaluated_at"].isoformat(), r["passed"], r["failed"], r["unknown"], *cells])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="compliance.csv"'})


@router.get("/compliance/report.pdf")
async def report_pdf(ctx: Ctx = ReadCtx, rule_set_id: uuid.UUID | None = None) -> Response:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from app.config import get_settings

    m = await _matrix(ctx, rule_set_id)
    product = get_settings().product_name
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
                            title="Compliance-Bericht", author=product, creator=product)
    st = getSampleStyleSheet()
    small = st["BodyText"].clone("small", fontSize=7, leading=8)
    story: list[Any] = [Paragraph(f"{product} – Compliance-Bericht", st["Heading2"]),
                        Paragraph(f"Stand {utcnow():%d.%m.%Y %H:%M} UTC · {len(m['rows'])} Geräte/Regelsets", st["BodyText"]), Spacer(1, 4 * mm)]
    head = ["Gerät", "Regelset", "ok", "verl."] + [Paragraph(c["name"], small) for c in m["rules"]]
    data: list[list[Any]] = [head]
    styles = [("GRID", (0, 0), (-1, -1), 0.3, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 7), ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke)]
    color = {"ok": colors.HexColor("#DCFCE7"), "fail": colors.HexColor("#FEE2E2"), "unknown": colors.HexColor("#F1F5F9"),
             "warn": colors.HexColor("#FFEDD5")}
    for i, r in enumerate(m["rows"], start=1):
        row: list[Any] = [r["device"], r["rule_set"], str(r["passed"]), str(r["failed"])]
        for j, c in enumerate(m["rules"], start=4):
            cell = r["cells"].get(c["rule_id"]) if c["rule_set_id"] == r["rule_set_id"] else None
            row.append({"ok": "ok", "warn": "!", "fail": "X", "unknown": "?"}.get(cell["status"], "") if cell else "")
            if cell:
                styles.append(("BACKGROUND", (j, i), (j, i), color[cell["status"]]))
        data.append(row)
    if len(data) == 1:
        story.append(Paragraph("Keine Ergebnisse.", st["BodyText"]))
    else:
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle(styles))
        story.append(t)
    doc.build(story)
    return Response(buf.getvalue(), media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="compliance.pdf"'})


@router.get("/compliance/trend")
async def trend(ctx: Ctx = ReadCtx, days: int = Query(default=30, ge=1, le=180)) -> list[dict[str, Any]]:
    """Anteil bestandener Regeln je Tag (letztes Ergebnis je Gerät/Regelset und Tag)."""
    since = utcnow() - dt.timedelta(days=days)
    rows = (await ctx.db.execute(select(ComplianceResult).where(ComplianceResult.evaluated_at >= since)
                                 .order_by(ComplianceResult.evaluated_at))).scalars().all()
    per_day: dict[str, dict[tuple[Any, Any], ComplianceResult]] = {}
    for r in rows:
        per_day.setdefault(r.evaluated_at.date().isoformat(), {})[(r.device_id, r.rule_set_id)] = r
    out = []
    for day, items in sorted(per_day.items()):
        passed = sum(r.passed for r in items.values())
        total = sum(r.passed + r.failed for r in items.values())
        out.append({"day": day, "passed": passed, "failed": total - passed, "ratio": round(passed / total, 3) if total else None,
                    "devices_failing": sum(1 for r in items.values() if r.failed)})
    return out


@router.get("/devices/{device_id}/compliance")
async def device_compliance(device_id: uuid.UUID, ctx: Ctx = ReadCtx) -> list[dict[str, Any]]:
    dev = await get_or_404(ctx.db, Device, device_id, "Device")
    sets = {rs.id: rs.name for rs in (await ctx.db.execute(select(ComplianceRuleSet))).scalars()}
    return [{"rule_set_id": str(k[1]), "rule_set": sets.get(k[1], "?"), "evaluated_at": r.evaluated_at, "passed": r.passed,
             "failed": r.failed, "unknown": r.unknown, "results": r.results} for k, r in (await latest_results(ctx.db, [dev.id])).items()]


# ----------------------------------------------------------------------------- Config-Suche
@router.get("/config-search")
async def config_search(ctx: Ctx = ReadCtx, q: str = Query(min_length=1, max_length=200), regex: bool = False,
                        context: int = Query(default=2, ge=0, le=10), all_tenants: bool = False) -> dict[str, Any]:
    """Suche über die jeweils letzten Backups. Mandant: nur eigene Geräte; MSP mit ``all_tenants``: alle."""
    if all_tenants and not ctx.is_superuser:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Mandantenübergreifende Suche nur für den MSP")
    tenant = None if (all_tenants or ctx.tenant_id is None) and ctx.is_superuser else ctx.require_tenant()
    try:
        res = await search_backups(ctx.db, q, regex, context, tenant_id=tenant)
    except ComplianceError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await ctx.audit("config.search", details={"q": q, "regex": regex, "all_tenants": tenant is None, "hits": len(res["hits"])})
    await ctx.db.commit()
    return res
