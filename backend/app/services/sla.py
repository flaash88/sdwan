"""SLA-/Verfügbarkeitsberichte (Phase 10).

Verfügbarkeit = Zeit ``online`` / (Zeit ``online`` + Zeit ``offline``) im Zeitraum, berechnet aus den
Statuswechseln (``status_events``). Zeit vor dem ersten bekannten Zustand bzw. vor dem Pairing zählt
nicht (weder up noch down). WAN-Links analog mit ``up``/``degraded`` = verfügbar, ``down`` = nicht.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import system_session, utcnow
from app.models import Alert, Device, PairingStatus, Site, SlaReport, StatusEvent, Tenant, WanLink

log = logging.getLogger(__name__)
UP = {"online", "up", "degraded"}
DOWN = {"offline", "down"}


def availability(events: list[tuple[dt.datetime, str]], start: dt.datetime, end: dt.datetime, initial: str | None) -> dict[str, Any]:
    """events: sortierte (Zeitpunkt, Status) innerhalb [start, end]; initial: Zustand bei ``start``."""
    up = down = 0.0
    outages: list[dict[str, Any]] = []
    state, t = initial, start
    cur_out: dt.datetime | None = start if initial in DOWN else None
    for at, st in [*events, (end, None)]:
        at = min(max(at, start), end)
        span = (at - t).total_seconds()
        if state in UP:
            up += span
        elif state in DOWN:
            down += span
        if st is None:
            break
        if state not in DOWN and st in DOWN:
            cur_out = at
        elif state in DOWN and st not in DOWN and cur_out is not None:
            outages.append({"start": cur_out, "end": at, "duration_s": (at - cur_out).total_seconds()})
            cur_out = None
        state, t = st, at
    if cur_out is not None:
        outages.append({"start": cur_out, "end": None, "duration_s": (end - cur_out).total_seconds()})
    measured = up + down
    return {
        "availability_pct": round(100 * up / measured, 3) if measured else None,
        "downtime_s": round(down), "measured_s": round(measured), "outages": outages,
        "outage_count": len(outages), "longest_outage_s": round(max((o["duration_s"] for o in outages), default=0)),
        "mttr_s": round(sum(o["duration_s"] for o in outages) / len(outages)) if outages else 0,
    }


async def _series(db: AsyncSession, device_id: uuid.UUID, subject: str, start: dt.datetime, end: dt.datetime) -> tuple[list[tuple[dt.datetime, str]], str | None]:
    before = (
        await db.execute(select(StatusEvent).where(StatusEvent.device_id == device_id, StatusEvent.subject == subject, StatusEvent.at < start)
                         .order_by(StatusEvent.at.desc()).limit(1))
    ).scalar_one_or_none()
    rows = (
        await db.execute(select(StatusEvent).where(StatusEvent.device_id == device_id, StatusEvent.subject == subject,
                                                   StatusEvent.at >= start, StatusEvent.at <= end).order_by(StatusEvent.at))
    ).scalars().all()
    return [(r.at, r.status) for r in rows], before.status if before else None


async def build_report(db: AsyncSession, tenant: Tenant, start: dt.datetime, end: dt.datetime) -> dict[str, Any]:
    sites = {s.id: s.name for s in (await db.execute(select(Site).where(Site.tenant_id == tenant.id))).scalars()}
    devices = (await db.execute(select(Device).where(Device.tenant_id == tenant.id, Device.pairing_status == PairingStatus.paired).order_by(Device.name))).scalars().all()
    rows = []
    total_up = total_measured = 0.0
    for d in devices:
        ev, initial = await _series(db, d.id, "device", start, end)
        a = availability(ev, start, end, initial)
        wans = []
        for lk in (await db.execute(select(WanLink).where(WanLink.device_id == d.id).order_by(WanLink.slot))).scalars():
            wev, wini = await _series(db, d.id, f"wan:{lk.id}", start, end)
            wa = availability(wev, start, end, wini)
            wans.append({"name": lk.name, "interface": lk.interface, **{k: wa[k] for k in ("availability_pct", "downtime_s", "outage_count")}})
        rows.append({"device_id": str(d.id), "device": d.name, "site": sites.get(d.site_id, "–") if d.site_id else "–", **a, "wan": wans})
        total_up += a["measured_s"] - a["downtime_s"]
        total_measured += a["measured_s"]
    alerts = (await db.execute(select(Alert).where(Alert.tenant_id == tenant.id, Alert.fired_at >= start, Alert.fired_at <= end))).scalars().all()
    return {
        "tenant": tenant.name, "tenant_id": str(tenant.id), "period_start": start, "period_end": end, "generated_at": utcnow(),
        "fleet_availability_pct": round(100 * total_up / total_measured, 3) if total_measured else None,
        "device_count": len(rows), "alert_count": len(alerts),
        "alerts_by_severity": {s: sum(1 for a in alerts if a.severity == s) for s in ("critical", "warning", "info")},
        "devices": rows,
    }


def _dur(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    return f"{s // 3600} h {(s % 3600) // 60} min"


def render_pdf(rep: dict[str, Any]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
                            title=f"SLA-Bericht {rep['tenant']}")
    st = getSampleStyleSheet()
    fmt = "%d.%m.%Y %H:%M"
    story: list[Any] = [
        Paragraph(f"SLA-Verfügbarkeitsbericht – {rep['tenant']}", st["Title"]),
        Paragraph(f"Zeitraum: {rep['period_start']:{fmt}} – {rep['period_end']:{fmt}} UTC · erstellt {rep['generated_at']:{fmt}} UTC", st["Normal"]),
        Spacer(1, 6 * mm),
    ]
    fa = rep["fleet_availability_pct"]
    summary = [["Gesamtverfügbarkeit", f"{fa:.3f} %" if fa is not None else "keine Daten"], ["Geräte", str(rep["device_count"])],
               ["Alarme im Zeitraum", f"{rep['alert_count']} (kritisch {rep['alerts_by_severity']['critical']}, Warnung {rep['alerts_by_severity']['warning']})"]]
    t = Table(summary, colWidths=[60 * mm, 100 * mm])
    t.setStyle(TableStyle([("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story += [t, Spacer(1, 6 * mm), Paragraph("Verfügbarkeit je Gerät", st["Heading2"])]
    data = [["Gerät", "Standort", "Verfügbarkeit", "Ausfallzeit", "Ausfälle", "Längster", "MTTR"]]
    for d in rep["devices"]:
        av = d["availability_pct"]
        data.append([d["device"], d["site"], f"{av:.3f} %" if av is not None else "–", _dur(d["downtime_s"]), str(d["outage_count"]),
                     _dur(d["longest_outage_s"]), _dur(d["mttr_s"]) if d["outage_count"] else "–"])
    tbl = Table(data, repeatRows=1, colWidths=[38 * mm, 32 * mm, 24 * mm, 22 * mm, 16 * mm, 20 * mm, 20 * mm])
    style = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f766e")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
             ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 8),
             ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1"))]
    for i, d in enumerate(rep["devices"], start=1):
        av = d["availability_pct"]
        if av is not None and av < 99.0:
            style.append(("TEXTCOLOR", (2, i), (2, i), colors.HexColor("#dc2626")))
    tbl.setStyle(TableStyle(style))
    story.append(tbl)
    wan_rows = [["Gerät", "WAN", "Interface", "Verfügbarkeit", "Ausfallzeit", "Ausfälle"]]
    for d in rep["devices"]:
        for w in d["wan"]:
            av = w["availability_pct"]
            wan_rows.append([d["device"], w["name"], w["interface"], f"{av:.3f} %" if av is not None else "–", _dur(w["downtime_s"]), str(w["outage_count"])])
    if len(wan_rows) > 1:
        story += [Spacer(1, 6 * mm), Paragraph("WAN-Verfügbarkeit", st["Heading2"])]
        wt = Table(wan_rows, repeatRows=1)
        wt.setStyle(TableStyle(style[:5]))
        story.append(wt)
    outs = [(d["device"], o) for d in rep["devices"] for o in d["outages"]]
    if outs:
        story += [Spacer(1, 6 * mm), Paragraph("Ausfälle", st["Heading2"])]
        ot = Table([["Gerät", "Beginn", "Ende", "Dauer"]] + [[n, f"{o['start']:{fmt}}", f"{o['end']:{fmt}}" if o["end"] else "andauernd", _dur(o["duration_s"])] for n, o in outs[:200]], repeatRows=1)
        ot.setStyle(TableStyle(style[:5]))
        story.append(ot)
    doc.build(story)
    return buf.getvalue()


def previous_month(now: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    now = now or utcnow()
    first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_prev = first_this - dt.timedelta(days=1)
    return last_prev.replace(day=1), first_this


async def generate_and_store(db: AsyncSession, tenant: Tenant, start: dt.datetime, end: dt.datetime, send: bool) -> SlaReport:
    from app.services.mailer import send_mail

    rep = await build_report(db, tenant, start, end)
    pdf = render_pdf(rep)
    summary = {k: v for k, v in rep.items() if k != "devices"} | {"devices": [{k: d[k] for k in ("device", "site", "availability_pct", "downtime_s", "outage_count")} for d in rep["devices"]]}
    report = SlaReport(tenant_id=tenant.id, period_start=start, period_end=end, summary=_jsonable(summary), pdf=pdf, sent_to=[])
    db.add(report)
    recipients = list(dict.fromkeys([*((tenant.settings or {}).get("report_recipients") or []), *([tenant.contact_email] if tenant.contact_email else [])]))
    if send and recipients:
        fa = rep["fleet_availability_pct"]
        ok = await send_mail(recipients, f"[SD-WAN] SLA-Bericht {tenant.name} {start:%m/%Y}",
                             f"Anbei der SLA-Verfügbarkeitsbericht für {start:%d.%m.%Y} – {end:%d.%m.%Y}.\n"
                             f"Gesamtverfügbarkeit: {f'{fa:.3f} %' if fa is not None else 'keine Daten'}\n",
                             [(f"sla-{tenant.slug}-{start:%Y-%m}.pdf", pdf, "application/pdf")])
        if ok:
            report.sent_to = recipients
    await db.flush()
    return report


def _jsonable(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_jsonable(v) for v in o]
    if isinstance(o, dt.datetime):
        return o.isoformat()
    return o


async def monthly_reports() -> None:
    """Worker-Job (1. des Monats): Bericht für den Vormonat je Mandant erzeugen und versenden."""
    start, end = previous_month()
    async with system_session() as db:
        for tenant in (await db.execute(select(Tenant).where(Tenant.is_active.is_(True)))).scalars().all():
            if not (tenant.settings or {}).get("monthly_report", True):
                continue
            exists = (await db.execute(select(SlaReport).where(SlaReport.tenant_id == tenant.id, SlaReport.period_start == start))).first()
            if exists:
                continue
            await generate_and_store(db, tenant, start, end, send=True)
        await db.commit()
