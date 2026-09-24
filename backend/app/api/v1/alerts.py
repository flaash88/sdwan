"""Alerts & SLA-Reports (Phase 10)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select

from app.api.v1.common import get_or_404
from app.db import utcnow
from app.deps import AdminCtx, Ctx, ReadCtx, TechCtx
from app.models import Alert, AlertRule, Device, SlaReport, Tenant
from app.services.alerts import TYPES, create_default_rules, evaluate_tenant
from app.services.mailer import send_mail
from app.services.sla import _jsonable, build_report, generate_and_store, render_pdf

router = APIRouter(tags=["alerts", "reports"])


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: str
    severity: Literal["info", "warning", "critical"] = "warning"
    params: dict[str, Any] = {}
    duration_s: int = Field(default=120, ge=0, le=86400)
    site_ids: list[uuid.UUID] = []
    device_ids: list[uuid.UUID] = []
    recipients: list[EmailStr] = []
    notify_resolved: bool = True
    enabled: bool = True

    @field_validator("type")
    @classmethod
    def v_type(cls, v: str) -> str:
        if v not in TYPES:
            raise ValueError(f"type: {list(TYPES)}")
        return v

    @field_validator("params")
    @classmethod
    def v_params(cls, v: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if "threshold" in v:
            out["threshold"] = float(v["threshold"])
        if "metric" in v:
            if v["metric"] not in ("wan", "mgmt"):
                raise ValueError("metric: wan | mgmt")
            out["metric"] = v["metric"]
        return out


def _rule_out(r: AlertRule) -> dict:
    return {"id": str(r.id), "name": r.name, "type": r.type, "type_label": TYPES.get(r.type), "severity": r.severity, "params": r.params,
            "duration_s": r.duration_s, "site_ids": r.site_ids, "device_ids": r.device_ids, "recipients": r.recipients,
            "notify_resolved": r.notify_resolved, "enabled": r.enabled}


def _alert_out(a: Alert, names: dict | None = None) -> dict:
    return {"id": str(a.id), "rule_id": str(a.rule_id) if a.rule_id else None, "device_id": str(a.device_id) if a.device_id else None,
            "device": (names or {}).get(a.device_id), "subject": a.subject, "status": a.status, "severity": a.severity, "message": a.message,
            "value": a.value, "started_at": a.started_at, "fired_at": a.fired_at, "resolved_at": a.resolved_at, "notified": a.notified,
            "acknowledged_by": a.acknowledged_by, "acknowledged_at": a.acknowledged_at}


def _rule_data(data: RuleIn) -> dict:
    d = data.model_dump(mode="json")
    return d


@router.get("/alert-rules")
async def list_rules(ctx: Ctx = ReadCtx) -> list[dict]:
    return [_rule_out(r) for r in (await ctx.db.execute(select(AlertRule).order_by(AlertRule.name))).scalars()]


@router.get("/alert-rules/types")
async def rule_types(_ctx: Ctx = ReadCtx) -> dict:
    return TYPES


@router.post("/alert-rules", status_code=201)
async def create_rule(data: RuleIn, ctx: Ctx = AdminCtx) -> dict:
    r = AlertRule(tenant_id=ctx.require_tenant(), **_rule_data(data))
    ctx.db.add(r)
    await ctx.db.flush()
    await ctx.audit("alert_rule.create", target_type="alert_rule", target_id=r.id, details=_rule_data(data))
    await ctx.db.commit()
    return _rule_out(r)


@router.post("/alert-rules/defaults", status_code=201)
async def defaults(ctx: Ctx = AdminCtx) -> list[dict]:
    rules = await create_default_rules(ctx.db, ctx.require_tenant())
    await ctx.audit("alert_rule.defaults", details={"count": len(rules)})
    await ctx.db.commit()
    return [_rule_out(r) for r in rules]


@router.put("/alert-rules/{rule_id}")
async def update_rule(rule_id: uuid.UUID, data: RuleIn, ctx: Ctx = AdminCtx) -> dict:
    r = await get_or_404(ctx.db, AlertRule, rule_id, "Regel")
    for k, v in _rule_data(data).items():
        setattr(r, k, v)
    await ctx.audit("alert_rule.update", target_type="alert_rule", target_id=r.id, details=_rule_data(data))
    await ctx.db.commit()
    return _rule_out(r)


@router.delete("/alert-rules/{rule_id}", status_code=204, response_model=None)
async def delete_rule(rule_id: uuid.UUID, ctx: Ctx = AdminCtx) -> None:
    r = await get_or_404(ctx.db, AlertRule, rule_id, "Regel")
    await ctx.audit("alert_rule.delete", target_type="alert_rule", target_id=r.id, details={"name": r.name})
    await ctx.db.delete(r)
    await ctx.db.commit()


@router.post("/alert-rules/{rule_id}/test")
async def test_rule(rule_id: uuid.UUID, ctx: Ctx = AdminCtx) -> dict:
    r = await get_or_404(ctx.db, AlertRule, rule_id, "Regel")
    tenant = await get_or_404(ctx.db, Tenant, r.tenant_id, "Tenant")
    to = r.recipients or ([tenant.contact_email] if tenant.contact_email else [])
    if not to:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Keine Empfänger (Regel oder Mandanten-Kontakt)")
    ok = await send_mail(to, f"[SD-WAN][TEST] {r.name}", f"Test-Benachrichtigung der Alert-Regel '{r.name}' für {tenant.name}.")
    return {"sent": ok, "to": to}


@router.post("/alerts/evaluate")
async def evaluate_now(ctx: Ctx = TechCtx) -> dict:
    tenant = await get_or_404(ctx.db, Tenant, ctx.require_tenant(), "Tenant")
    stats = await evaluate_tenant(ctx.db, tenant)
    await ctx.db.commit()
    return stats


@router.get("/alerts")
async def list_alerts(ctx: Ctx = ReadCtx, state: Literal["open", "all", "resolved"] = "open", limit: int = Query(default=200, le=1000)) -> list[dict]:
    q = select(Alert).order_by(Alert.started_at.desc()).limit(limit)
    if state == "open":
        q = q.where(Alert.status == "firing")
    elif state == "resolved":
        q = q.where(Alert.status == "resolved")
    else:
        q = q.where(Alert.status != "pending")
    alerts = (await ctx.db.execute(q)).scalars().all()
    names = {d.id: d.name for d in (await ctx.db.execute(select(Device))).scalars()}
    return [_alert_out(a, names) for a in alerts]


@router.post("/alerts/{alert_id}/ack")
async def ack(alert_id: uuid.UUID, ctx: Ctx = TechCtx) -> dict:
    a = await get_or_404(ctx.db, Alert, alert_id, "Alert")
    a.acknowledged_by, a.acknowledged_at = ctx.user.email, utcnow()
    await ctx.audit("alert.ack", target_type="alert", target_id=a.id, details={"message": a.message})
    await ctx.db.commit()
    return _alert_out(a)


# ----------------------------------------------------------------------------- Reports
def _period(start: dt.date | None, end: dt.date | None) -> tuple[dt.datetime, dt.datetime]:
    today = utcnow().date()
    s = dt.datetime.combine(start or today.replace(day=1), dt.time(), dt.UTC)
    e = dt.datetime.combine(end, dt.time(), dt.UTC) + dt.timedelta(days=1) if end else utcnow()
    if e <= s:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ende muss nach Beginn liegen")
    if (e - s).days > 366:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Maximal 1 Jahr")
    return s, e


@router.get("/reports/sla")
async def sla(ctx: Ctx = ReadCtx, start: dt.date | None = None, end: dt.date | None = None, format: Literal["json", "pdf"] = "json") -> Any:
    """SLA-Bericht für einen Zeitraum (``end`` inklusive; Standard: aktueller Monat bis jetzt)."""
    tenant = await get_or_404(ctx.db, Tenant, ctx.require_tenant(), "Tenant")
    s, e = _period(start, end)
    rep = await build_report(ctx.db, tenant, s, e)
    if format == "pdf":
        pdf = render_pdf(rep)
        return Response(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="sla-{tenant.slug}-{s:%Y%m%d}-{e:%Y%m%d}.pdf"'})
    return _jsonable(rep)


class ReportSettings(BaseModel):
    monthly_report: bool = True
    report_recipients: list[EmailStr] = []


@router.get("/reports")
async def list_reports(ctx: Ctx = ReadCtx) -> dict:
    tenant = await get_or_404(ctx.db, Tenant, ctx.require_tenant(), "Tenant")
    rows = (await ctx.db.execute(select(SlaReport).order_by(SlaReport.period_start.desc()).limit(36))).scalars()
    return {
        "settings": {"monthly_report": (tenant.settings or {}).get("monthly_report", True),
                     "report_recipients": (tenant.settings or {}).get("report_recipients", []), "contact_email": tenant.contact_email},
        "reports": [{"id": str(r.id), "period_start": r.period_start, "period_end": r.period_end, "created_at": r.created_at,
                     "fleet_availability_pct": r.summary.get("fleet_availability_pct"), "sent_to": r.sent_to} for r in rows],
    }


@router.put("/reports/settings")
async def report_settings(data: ReportSettings, ctx: Ctx = AdminCtx) -> dict:
    tenant = await get_or_404(ctx.db, Tenant, ctx.require_tenant(), "Tenant")
    tenant.settings = {**(tenant.settings or {}), **data.model_dump(mode="json")}
    await ctx.audit("report.settings", details=data.model_dump(mode="json"))
    await ctx.db.commit()
    return data.model_dump()


@router.post("/reports", status_code=201)
async def create_report(ctx: Ctx = TechCtx, start: dt.date | None = None, end: dt.date | None = None, send: bool = False) -> dict:
    tenant = await get_or_404(ctx.db, Tenant, ctx.require_tenant(), "Tenant")
    s, e = _period(start, end)
    r = await generate_and_store(ctx.db, tenant, s, e, send)
    await ctx.audit("report.create", target_type="report", target_id=r.id, details={"start": s.isoformat(), "end": e.isoformat(), "sent_to": r.sent_to})
    await ctx.db.commit()
    return {"id": str(r.id), "sent_to": r.sent_to, "fleet_availability_pct": r.summary.get("fleet_availability_pct")}


@router.get("/reports/{report_id}/pdf")
async def report_pdf(report_id: uuid.UUID, ctx: Ctx = ReadCtx) -> Response:
    r = await get_or_404(ctx.db, SlaReport, report_id, "Bericht")
    return Response(r.pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="sla-{r.period_start:%Y%m%d}.pdf"'})
