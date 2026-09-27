"""Script-Bibliothek und Massen-Ausführung (Phase 17).

* Variablen nur aus einer festen Liste (``{{ device.name }}`` …), kein Template-Motor. Unbekannte Variablen und
  Werte mit Zeichen, die das Script verändern könnten, sind Fehler.
* Ausführung per SSH (wie der Backup-Export), damit die Ausgabe wie im Terminal erfasst wird.
  ANNAHME (Labor): mehrzeilige Scripts laufen per SSH-Befehl wie eingetippt.
* „ändernd“: vorher Backup (Auslöser ``pre-script``); schlägt das Backup fehl, wird auf diesem Gerät nicht
  ausgeführt. Gestaffelt in Batches, Pause bei zu vielen Fehlern (wie Firmware-Rollout).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.config import get_settings
from app.db import system_session, utcnow
from app.models import Device, DeviceStatus, PairingStatus, ScriptRun, ScriptRunItem, Site, Tenant

log = logging.getLogger(__name__)

VARIABLES = ("device.name", "device.identity", "device.tunnel_ip", "device.serial", "device.model", "site.name", "tenant.name", "tenant.slug")
_VAR = re.compile(r"\{\{\s*([A-Za-z_.]+)\s*\}\}")
_UNSAFE = re.compile(r'["\\$\[\]{};\r\n]')
OUTPUT_LIMIT = 64 * 1024
EXEC_TIMEOUT_S = 120
# RouterOS-Fehlermeldungen in der Ausgabe
_ERROR_OUT = re.compile(r"(syntax error|expected end of command|bad command name|failure:|no such item|input does not match)", re.IGNORECASE)


class ScriptError(ValueError):
    pass


def variables_used(text: str) -> list[str]:
    return sorted(set(_VAR.findall(text)))


def validate_content(text: str) -> list[str]:
    """Prüft Variablen; liefert Warnungen (z. B. verwaltete sdwan:-Objekte)."""
    if not text.strip():
        raise ScriptError("Script ist leer")
    if len(text) > 20000:
        raise ScriptError("Script zu lang (max. 20 000 Zeichen)")
    unknown = [v for v in variables_used(text) if v not in VARIABLES]
    if unknown:
        raise ScriptError(f"Unbekannte Variablen: {', '.join(unknown)} (erlaubt: {', '.join(VARIABLES)})")
    return warnings(text)


def warnings(text: str) -> list[str]:
    w = []
    if "sdwan:" in text or "sdwan-" in text:
        w.append("Das Script bezieht sich auf verwaltete Objekte (sdwan:/sdwan-). Änderungen daran überschreibt die Plattform "
                 "beim nächsten Abgleich – oder sie brechen Funktionen der Plattform.")
    if re.search(r"^\s*/system\s+(reset-configuration|reboot|shutdown)", text, re.MULTILINE):
        w.append("Das Script startet den Router neu bzw. setzt ihn zurück.")
    if re.search(r"/user\s+(remove|set)|/ip\s+service\s+(set|disable)", text):
        w.append("Das Script ändert Benutzer oder Dienste – die Plattform könnte den Zugriff verlieren.")
    return w


def context_for(device: Device, site: Site | None, tenant: Tenant | None) -> dict[str, str]:
    return {
        "device.name": device.name, "device.identity": device.identity or "", "device.tunnel_ip": device.tunnel_ip or "",
        "device.serial": device.serial or "", "device.model": device.model or "",
        "site.name": site.name if site else "", "tenant.name": tenant.name if tenant else "", "tenant.slug": tenant.slug if tenant else "",
    }


def _inside_quotes(text: str, pos: int) -> bool:
    """Steht ``pos`` innerhalb eines RouterOS-Strings ("…", ``\"`` maskiert)?"""
    inside, i = False, 0
    while i < pos:
        c = text[i]
        if c == "\\" and inside:
            i += 2
            continue
        if c == '"':
            inside = not inside
        i += 1
    return inside


def render(text: str, ctx: dict[str, str]) -> str:
    """Variablen einsetzen. Außerhalb eines Strings wird der Wert immer als RouterOS-String gequotet – ein Wert mit
    Leerzeichen oder ``=`` kann so keine zusätzlichen Parameter einschleusen (AUDIT-051); innerhalb eines Strings wird
    er unverändert eingesetzt (Sonderzeichen sind ohnehin abgelehnt)."""
    from app.routeros.naming import routeros_str

    out: list[str] = []
    last = 0
    for m in _VAR.finditer(text):
        key = m.group(1)
        if key not in VARIABLES:
            raise ScriptError(f"Unbekannte Variable {key}")
        val = ctx.get(key, "")
        if _UNSAFE.search(val):
            raise ScriptError(f"Wert von {key} enthält unzulässige Zeichen – Script würde verändert")
        out.append(text[last:m.start()])
        out.append(val if _inside_quotes(text, m.start()) else routeros_str(val))
        last = m.end()
    out.append(text[last:])
    return "".join(out)


async def render_for(db: AsyncSession, text: str, device: Device) -> str:
    site = await db.get(Site, device.site_id) if device.site_id else None
    tenant = await db.get(Tenant, device.tenant_id)
    return render(text, context_for(device, site, tenant))


async def execute(device: Device, text: str) -> tuple[bool, str]:
    """Führt das (gerenderte) Script aus -> (ok, Ausgabe)."""
    s = get_settings()
    if s.routeros_backend == "simulator":
        from app.routeros.client import open_connection

        conn = await open_connection(device.tunnel_ip, "", "")
        try:
            out = conn.router.run_user_script(text)  # type: ignore[attr-defined]
        finally:
            await conn.close()
        return not _ERROR_OUT.search(out), out[:OUTPUT_LIMIT]
    import asyncssh

    from app.routeros.client import assert_tunnel_address
    from app.security import decrypt_secret

    assert_tunnel_address(device.tunnel_ip)
    try:
        async with asyncssh.connect(device.tunnel_ip, port=s.ssh_port, username=s.routeros_api_user,
                                    password=decrypt_secret(device.api_password_enc), known_hosts=None, connect_timeout=15) as conn:
            res = await asyncio.wait_for(conn.run(text, check=False), timeout=EXEC_TIMEOUT_S)
    except (OSError, asyncssh.Error, TimeoutError) as exc:
        return False, f"SSH-Fehler: {exc}"
    out = f"{res.stdout or ''}{res.stderr or ''}"
    ok = res.exit_status in (0, None) and not _ERROR_OUT.search(out)
    return ok, out[:OUTPUT_LIMIT]


async def run_item(db: AsyncSession, run: ScriptRun, item: ScriptRunItem) -> None:
    from app.services.backup import BackupError, take_backup

    dev = await db.get(Device, item.device_id)
    item.started_at = utcnow()
    if dev is None or dev.pairing_status != PairingStatus.paired or dev.status == DeviceStatus.offline:
        item.status, item.error, item.finished_at = "skipped", "Gerät nicht erreichbar", utcnow()
        return
    item.status = "running"
    if run.category == "change":
        try:
            b, _ = await take_backup(db, dev, "pre-script", note=f"vor Script {run.script_name} v{run.script_version}", created_by=run.created_by)
            item.backup_id = b.id
        except Exception as exc:  # noqa: BLE001 - ohne Backup keine Änderung
            item.status, item.error, item.finished_at = "failed", f"Backup vor Ausführung fehlgeschlagen: {exc}", utcnow()
            return
    ok, out = await execute(dev, item.rendered)
    item.output = out
    item.status = "success" if ok else "failed"
    if not ok:
        item.error = (_ERROR_OUT.search(out).group(0) if _ERROR_OUT.search(out) else out[:200]) or "Fehler"
    item.finished_at = utcnow()


async def tick_run(db: AsyncSession, run: ScriptRun) -> None:
    items = (await db.execute(select(ScriptRunItem).where(ScriptRunItem.run_id == run.id).order_by(ScriptRunItem.batch_no))).scalars().all()
    batch = [i for i in items if i.batch_no == run.current_batch]
    for i in batch:
        if i.status == "queued":
            await run_item(db, run, i)
    failed = sum(1 for i in items if i.status == "failed")
    if failed >= run.max_failures:
        run.status, run.last_error = "paused", f"{failed} Fehler – Ausführung angehalten"
        return
    later = [i for i in items if i.batch_no > run.current_batch and i.status == "queued"]
    if later:
        run.current_batch = min(i.batch_no for i in later)
    else:
        run.status, run.finished_at = ("completed" if not failed else "failed"), utcnow()


async def script_tick() -> None:
    """Worker-Job: je laufender Ausführung einen Batch abarbeiten."""
    async with system_session() as db:
        runs = (await db.execute(select(ScriptRun).where(ScriptRun.status == "running"))).scalars().all()
        for run in runs:
            try:
                await tick_run(db, run)
            except Exception as exc:  # noqa: BLE001
                log.exception("Script-Ausführung %s", run.id)
                run.last_error = str(exc)
            if run.status in ("completed", "failed", "paused"):
                await audit(db, f"script.run.{run.status}", tenant_id=run.tenant_id, target_type="script_run", target_id=run.id,
                            success=run.status == "completed", details={"script": run.script_name, "error": run.last_error})
            await db.commit()


def plan_batches(device_ids: list[Any], batch_size: int) -> list[tuple[Any, int]]:
    return [(d, i // max(batch_size, 1)) for i, d in enumerate(device_ids)]
