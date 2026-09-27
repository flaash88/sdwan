"""Plattform-Sicherung und Wiederherstellung (Phase 21).

Ein Archiv (tar.gz, age-verschlüsselt) enthält:
* ``db.dump``      – ``pg_dump -Fc`` der Plattform-Datenbank (inkl. Hotspot-Logos/-Seiten, die in der DB liegen)
* ``env``          – die ``.env`` (SECRET_KEY/ENCRYPTION_KEY: ohne sie sind gespeicherte Router-Passwörter unlesbar)
* ``hub/``         – WireGuard-Hub-Schlüssel und -Konfiguration (``hub.key``, ``wg0.conf``): gleicher Schlüssel +
                     gleicher Endpoint ⇒ Router verbinden sich nach dem Restore ohne Eingriff
* ``influx/``      – optional (``PLATFORM_BACKUP_INFLUX``) ``influx backup``
* ``manifest.json``– Zeitpunkt, Migrationsstand, Hub-Endpoint, sha256 je Teil

Verschlüsselt wird ausschließlich mit dem age-**Public-Key** (``PLATFORM_BACKUP_AGE_RECIPIENT``); ohne ihn wird keine
Sicherung geschrieben. Der Private Key liegt nie auf dem Server (siehe docs/DISASTER-RECOVERY.md).

CLI (im Worker-/API-Container):
    python -m app.platform_backup run                     # Sicherung jetzt
    python -m app.platform_backup restore ARCHIV --identity KEYFILE --out DIR [--database-url URL]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.config import get_settings

log = logging.getLogger(__name__)
PREFIX = "sdwan-platform-"
SUFFIX = ".tar.gz.age"


class PlatformBackupError(RuntimeError):
    pass


def libpq_url(url: str) -> str:
    """SQLAlchemy-URL (postgresql+asyncpg://…) → libpq-URL für pg_dump/pg_restore."""
    if not url.startswith("postgresql"):
        raise PlatformBackupError("Plattform-Sicherung unterstützt nur PostgreSQL")
    return "postgresql://" + url.split("://", 1)[1]


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(cmd: list[str], env: dict[str, str] | None = None, timeout: int = 3600) -> None:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env={**os.environ, **(env or {})}, check=False)
    except FileNotFoundError as exc:
        raise PlatformBackupError(f"Programm fehlt: {cmd[0]}") from exc
    if res.returncode != 0:
        raise PlatformBackupError(f"{cmd[0]} fehlgeschlagen: {(res.stderr or res.stdout).strip()[:500]}")


def recipient() -> Any:
    from pyrage import x25519

    key = get_settings().platform_backup_age_recipient.strip()
    if not key:
        return None
    try:
        return x25519.Recipient.from_str(key)
    except Exception as exc:  # noqa: BLE001
        raise PlatformBackupError(f"PLATFORM_BACKUP_AGE_RECIPIENT ist kein gültiger age-Public-Key: {exc}") from exc


async def migration_revision(database_url: str) -> str | None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(database_url)
    try:
        async with eng.connect() as c:
            return (await c.execute(text("select version_num from alembic_version"))).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - z. B. Test-DB ohne Alembic
        return None
    finally:
        await eng.dispose()


def build_archive(work: Path, database_url: str, env_file: Path | None, hub_dir: Path | None, influx: bool,
                  revision: str | None) -> tuple[Path, dict[str, Any]]:
    """Legt die Teile in ``work`` an und packt sie zu ``work/archive.tar.gz``. Rückgabe (Pfad, Manifest)."""
    s = get_settings()
    parts: dict[str, Any] = {}
    warnings: list[str] = []
    (work / "hub").mkdir()
    _run(["pg_dump", "-Fc", "--no-owner", "-f", str(work / "db.dump"), libpq_url(database_url)])
    parts["db.dump"] = {"size": (work / "db.dump").stat().st_size, "sha256": _sha(work / "db.dump")}
    if env_file and env_file.is_file():
        shutil.copy2(env_file, work / "env")
        parts["env"] = {"size": (work / "env").stat().st_size, "sha256": _sha(work / "env")}
    else:
        warnings.append(f".env nicht gefunden ({env_file}) – Schlüssel fehlen in der Sicherung")
    if hub_dir and hub_dir.is_dir():
        for f in sorted(hub_dir.iterdir()):
            if f.is_file() and (f.name.endswith(".key") or f.name.endswith(".conf")):
                shutil.copy2(f, work / "hub" / f.name)
                parts[f"hub/{f.name}"] = {"size": f.stat().st_size, "sha256": _sha(f)}
    if not any(k.startswith("hub/") for k in parts):
        warnings.append(f"Hub-Schlüssel nicht gefunden ({hub_dir}) – Router müssten nach einem Restore neu gekoppelt werden")
    if influx:
        # ANNAHME (Labor): influx-CLI im Container, Token/URL aus der Umgebung
        try:
            _run(["influx", "backup", str(work / "influx"), "--host", s.influx_url, "--token", s.influx_token])
            parts["influx/"] = {"size": sum(p.stat().st_size for p in (work / "influx").rglob("*") if p.is_file())}
        except PlatformBackupError as exc:
            warnings.append(f"InfluxDB nicht gesichert: {exc}")
    manifest = {"created_at": dt.datetime.now(dt.UTC).isoformat(), "revision": revision, "hub_endpoint": s.wg_hub_endpoint,
                "hub_port": s.wg_hub_port, "public_url": s.public_url, "parts": parts, "warnings": warnings}
    (work / "manifest.json").write_text(json.dumps(manifest, indent=2))
    archive = work / "archive.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name in ["manifest.json", "db.dump", "env", "hub", "influx"]:
            if (work / name).exists():
                tar.add(work / name, arcname=name)
    return archive, manifest


def encrypt(src: Path, dst: Path, rcpt: Any) -> None:
    import pyrage

    pyrage.encrypt_file(str(src), str(dst), [rcpt])


def decrypt(src: Path, dst: Path, identity_text: str) -> None:
    import pyrage
    from pyrage import x25519

    ident = None
    for line in identity_text.splitlines():
        line = line.strip()
        if line.startswith("AGE-SECRET-KEY-"):
            ident = x25519.Identity.from_str(line)
    if ident is None:
        raise PlatformBackupError("Keine age-Identität (AGE-SECRET-KEY-…) gefunden")
    pyrage.decrypt_file(str(src), str(dst), [ident])


def prune_local(directory: Path, keep_days: int, now: dt.datetime | None = None) -> list[str]:
    now = now or dt.datetime.now(dt.UTC)
    removed = []
    for f in directory.glob(f"{PREFIX}*{SUFFIX}"):
        ts = f.name[len(PREFIX):-len(SUFFIX)]
        try:
            created = dt.datetime.strptime(ts, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
        except ValueError:
            continue
        if now - created > dt.timedelta(days=keep_days):
            f.unlink()
            removed.append(f.name)
    return removed


def upload_remote(path: Path, remote: str, keep_days: int) -> None:
    """rclone-Ziel (S3-kompatibel oder SFTP). ANNAHME (Labor): rclone-Konfiguration im Container vorhanden."""
    _run(["rclone", "copyto", str(path), f"{remote.rstrip('/')}/{path.name}"])
    _run(["rclone", "delete", "--min-age", f"{keep_days}d", "--include", f"{PREFIX}*{SUFFIX}", remote])


async def run(trigger: str = "scheduled", started_by: str | None = None, database_url: str | None = None,
              out_dir: str | None = None, record_id: Any = None) -> dict[str, Any]:
    """Sicherung erstellen, protokollieren (``platform_backups``) und bei Fehler Plattform-Alarm auslösen."""
    from app.db import system_session, utcnow
    from app.models import PlatformBackup
    from app.services import platform_events

    s = get_settings()
    database_url = database_url or s.database_url
    async with system_session() as db:
        rec = await db.get(PlatformBackup, record_id) if record_id else None
        if rec is None:
            rec = PlatformBackup(trigger=trigger, started_by=started_by)
            db.add(rec)
        rec.status = "running"
        await db.commit()
        try:
            rcpt = recipient()
            if rcpt is None:
                rec.status, rec.error = "not_configured", "PLATFORM_BACKUP_AGE_RECIPIENT fehlt – ohne age-Public-Key wird nicht gesichert"
            else:
                directory = Path(out_dir or s.platform_backup_dir)
                directory.mkdir(parents=True, exist_ok=True)
                revision = await migration_revision(database_url)
                with tempfile.TemporaryDirectory(prefix="sdwan-backup-") as tmp:
                    archive, manifest = await asyncio.to_thread(
                        build_archive, Path(tmp), database_url, Path(s.platform_backup_env_file), Path(s.platform_backup_hub_dir),
                        s.platform_backup_influx, revision)
                    name = f"{PREFIX}{dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%SZ')}{SUFFIX}"
                    target = directory / name
                    await asyncio.to_thread(encrypt, archive, target, rcpt)
                rec.filename, rec.size, rec.sha256 = name, target.stat().st_size, _sha(target)
                rec.contents = {"parts": manifest["parts"], "warnings": manifest["warnings"], "revision": revision}
                targets: dict[str, str] = {"local": "ok"}
                pruned = await asyncio.to_thread(prune_local, directory, s.platform_backup_keep_days)
                if s.platform_backup_rclone_remote:
                    try:
                        await asyncio.to_thread(upload_remote, target, s.platform_backup_rclone_remote, s.platform_backup_keep_days)
                        targets["remote"] = "ok"
                    except PlatformBackupError as exc:
                        targets["remote"] = str(exc)
                rec.targets = {**targets, "pruned": pruned}
                rec.status = "ok" if targets.get("remote", "ok") == "ok" else "failed"
                if rec.status == "failed":
                    rec.error = f"Externes Ziel: {targets['remote']}"
        except (PlatformBackupError, OSError) as exc:
            rec.status, rec.error = "failed", str(exc)
        rec.finished_at = utcnow()
        if rec.status == "failed":
            await platform_events.fire(db, "platform_backup_failed", rec.error or "Sicherung fehlgeschlagen")
        elif rec.status == "ok":
            await platform_events.resolve(db, "platform_backup_failed")
        await db.commit()
        return {"id": str(rec.id), "status": rec.status, "filename": rec.filename, "size": rec.size, "error": rec.error,
                "targets": rec.targets, "contents": rec.contents}


async def backup_job() -> None:
    """Worker-Job (täglich ``PLATFORM_BACKUP_HOUR_UTC``)."""
    await run("scheduled")


async def queue_job() -> None:
    """Worker-Job (jede Minute): in der Oberfläche angeforderte Sicherungen ausführen. Nur der Worker hat die
    Volumes (.env, Hub, Sicherungsverzeichnis) eingebunden."""
    from sqlalchemy import select

    from app.db import system_session
    from app.models import PlatformBackup

    async with system_session() as db:
        queued = (await db.execute(select(PlatformBackup).where(PlatformBackup.status == "queued")
                                   .order_by(PlatformBackup.created_at))).scalars().all()
        ids = [(q.id, q.trigger, q.started_by) for q in queued]
    for rid, trig, by in ids:
        await run(trig, by, record_id=rid)


def restore(archive: Path, identity_text: str, out: Path, database_url: str | None = None) -> dict[str, Any]:
    """Archiv entschlüsseln und nach ``out`` entpacken; optional Datenbank per pg_restore einspielen.

    Die Dateien ``out/env`` und ``out/hub/*`` spielt ``deploy/restore.sh`` an ihren Platz (.env, Hub-Volume)."""
    out.mkdir(parents=True, exist_ok=True)
    plain = out / "archive.tar.gz"
    decrypt(archive, plain, identity_text)
    with tarfile.open(plain, "r:gz") as tar:
        tar.extractall(out, filter="data")
    plain.unlink()
    manifest = json.loads((out / "manifest.json").read_text())
    for name, meta in manifest["parts"].items():
        p = out / name
        if "sha256" in meta and p.is_file() and _sha(p) != meta["sha256"]:
            raise PlatformBackupError(f"Prüfsumme stimmt nicht: {name}")
    if database_url:
        _run(["pg_restore", "--clean", "--if-exists", "--no-owner", "-d", libpq_url(database_url), str(out / "db.dump")])
    return manifest


def list_local(directory: str | None = None) -> list[dict[str, Any]]:
    d = Path(directory or get_settings().platform_backup_dir)
    if not d.is_dir():
        return []
    return [{"filename": f.name, "size": f.stat().st_size} for f in sorted(d.glob(f"{PREFIX}*{SUFFIX}"), reverse=True)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.platform_backup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="Sicherung jetzt erstellen")
    sub.add_parser("list", help="lokale Sicherungen anzeigen")
    r = sub.add_parser("restore", help="Archiv entschlüsseln/entpacken (und DB einspielen)")
    r.add_argument("archive")
    r.add_argument("--identity", required=True, help="Datei mit dem age-Private-Key (AGE-SECRET-KEY-…)")
    r.add_argument("--out", required=True)
    r.add_argument("--database-url", default=None, help="Ziel-DB für pg_restore (leer = nur entpacken)")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        res = asyncio.run(run("cli", "cli"))
        print(json.dumps(res, indent=2, default=str))
        return 0 if res["status"] == "ok" else 1
    if a.cmd == "list":
        print(json.dumps(list_local(), indent=2))
        return 0
    manifest = restore(Path(a.archive), Path(a.identity).read_text(), Path(a.out), a.database_url)
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

