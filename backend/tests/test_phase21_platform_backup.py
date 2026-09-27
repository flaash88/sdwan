"""Phase 21 – Plattform-Sicherung: Archiv (age), Aufbewahrung, Status/Alarm, Wiederherstellung in eine frische DB."""

from __future__ import annotations

import datetime as dt
import os
import shutil
import uuid
from pathlib import Path

import pytest
from pyrage import x25519
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app import platform_backup as pb
from app.config import get_settings
from app.db import Base, system_session
from app.models import PlatformAlert, PlatformBackup, Tenant, User

PG_ADMIN = os.environ.get("PLATFORM_BACKUP_TEST_PG", "postgresql+asyncpg://sdwan:sdwan@localhost:5432/sdwan")


def _files(tmp: Path) -> tuple[Path, Path]:
    env = tmp / "platform.env"
    env.write_text("SECRET_KEY=test-secret\nENCRYPTION_KEY=test-enc\n")
    hub = tmp / "hub"
    hub.mkdir()
    (hub / "hub.key").write_text("HUBPRIVATEKEY=\n")
    (hub / "wg0.conf").write_text("[Interface]\nListenPort = 51820\n")
    (hub / "ignored.txt").write_text("x")
    return env, hub


def _configure(monkeypatch, tmp: Path, recipient: str) -> None:
    env, hub = _files(tmp)
    s = get_settings()
    monkeypatch.setattr(s, "platform_backup_age_recipient", recipient)
    monkeypatch.setattr(s, "platform_backup_env_file", str(env))
    monkeypatch.setattr(s, "platform_backup_hub_dir", str(hub))
    monkeypatch.setattr(s, "platform_backup_dir", str(tmp / "out"))
    monkeypatch.setattr(s, "platform_backup_rclone_remote", "")


async def _pg_available() -> bool:
    if not shutil.which("pg_dump") or not shutil.which("pg_restore"):
        return False
    eng = create_async_engine(PG_ADMIN)
    try:
        async with eng.connect() as c:
            await c.execute(text("select 1"))
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        await eng.dispose()


async def _fresh_db(name: str) -> str:
    eng = create_async_engine(PG_ADMIN, isolation_level="AUTOCOMMIT")
    async with eng.connect() as c:
        await c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        await c.execute(text(f'CREATE DATABASE "{name}"'))
    await eng.dispose()
    return PG_ADMIN.rsplit("/", 1)[0] + "/" + name


async def _drop_db(name: str) -> None:
    eng = create_async_engine(PG_ADMIN, isolation_level="AUTOCOMMIT")
    async with eng.connect() as c:
        await c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    await eng.dispose()


async def _counts(url: str) -> dict[str, int]:
    eng = create_async_engine(url)
    out = {}
    async with eng.connect() as c:
        for t in Base.metadata.sorted_tables:
            out[t.name] = (await c.execute(select(func.count()).select_from(t))).scalar_one()
    await eng.dispose()
    return out


async def test_backup_and_restore_into_fresh_db(client, msp, tmp_path, monkeypatch):
    if not await _pg_available():
        pytest.skip("PostgreSQL/pg_dump nicht verfügbar")
    ident = x25519.Identity.generate()
    _configure(monkeypatch, tmp_path, str(ident.to_public()))
    suffix = uuid.uuid4().hex[:8]
    src, dst = f"sdwan_dr_src_{suffix}", f"sdwan_dr_dst_{suffix}"
    src_url, dst_url = await _fresh_db(src), await _fresh_db(dst)
    try:
        eng = create_async_engine(src_url)
        async with eng.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        async with eng.begin() as c:
            tid = uuid.uuid4()
            await c.execute(Tenant.__table__.insert().values(id=tid, name="Beispiel-Mandant", slug="beispiel", settings={},
                                                             created_at=dt.datetime.now(dt.UTC)))
            await c.execute(User.__table__.insert().values(id=uuid.uuid4(), email="a@example.com", password_hash="x", role="admin",
                                                           tenant_id=tid, is_superuser=False, is_active=True, created_at=dt.datetime.now(dt.UTC)))
        await eng.dispose()

        res = await pb.run("manual", "test", database_url=src_url)
        assert res["status"] == "ok", res
        archive = tmp_path / "out" / res["filename"]
        assert archive.name.startswith(pb.PREFIX) and archive.stat().st_size == res["size"]
        assert b"Beispiel-Mandant" not in archive.read_bytes()  # verschlüsselt
        assert set(res["contents"]["parts"]) == {"db.dump", "env", "hub/hub.key", "hub/wg0.conf"} and res["contents"]["warnings"] == []

        # Wiederherstellung in eine frische Datenbank (wie restore.sh)
        manifest = pb.restore(archive, str(ident), tmp_path / "restored", dst_url)
        assert manifest["hub_endpoint"] == get_settings().wg_hub_endpoint
        assert (tmp_path / "restored" / "env").read_text().startswith("SECRET_KEY=test-secret")
        assert (tmp_path / "restored" / "hub" / "hub.key").read_text() == "HUBPRIVATEKEY=\n"
        src_counts, dst_counts = await _counts(src_url), await _counts(dst_url)
        assert src_counts == dst_counts and dst_counts["tenants"] == 1 and dst_counts["users"] == 1
        eng = create_async_engine(dst_url)
        async with eng.connect() as c:
            assert (await c.execute(text("select name from tenants"))).scalar_one() == "Beispiel-Mandant"
        await eng.dispose()

        # falscher Schlüssel → kein Klartext
        with pytest.raises(Exception):
            pb.restore(archive, str(x25519.Identity.generate()), tmp_path / "wrong")
    finally:
        await _drop_db(src)
        await _drop_db(dst)

    # Status in der Oberfläche (nur MSP-Admin)
    st = (await client.get("/api/v1/platform/backups", headers=msp)).json()
    assert st["config"]["configured"] and st["last_ok"]["filename"] == res["filename"]


async def test_not_configured_and_failure_alert(client, msp, tmp_path, monkeypatch):
    _configure(monkeypatch, tmp_path, "")
    res = await pb.run("manual", "test")
    assert res["status"] == "not_configured" and not (tmp_path / "out").exists()
    # Fehler (z. B. SQLite statt PostgreSQL bzw. kaputter Schlüssel) → Plattform-Alarm, danach bei Erfolg behoben
    monkeypatch.setattr(get_settings(), "platform_backup_age_recipient", "age1kaputt")
    res = await pb.run("manual", "test")
    assert res["status"] == "failed" and "age" in res["error"]
    async with system_session() as db:
        (a,) = (await db.execute(select(PlatformAlert))).scalars().all()
        assert a.type == "platform_backup_failed" and a.status == "firing"
    res = await pb.run("manual", "test")  # zweiter Fehler: kein zweiter Alarm
    alerts = (await client.get("/api/v1/platform/alerts", headers=msp)).json()
    assert len(alerts) == 1 and alerts[0]["label"] == "Plattform-Sicherung fehlgeschlagen"


def test_prune_and_encrypt_roundtrip(tmp_path):
    now = dt.datetime(2026, 9, 27, 3, 0, tzinfo=dt.UTC)
    for days in (1, 13, 15, 40):
        (tmp_path / f"{pb.PREFIX}{(now - dt.timedelta(days=days)).strftime('%Y%m%dT%H%M%SZ')}{pb.SUFFIX}").write_bytes(b"x")
    (tmp_path / "fremd.txt").write_text("bleibt")
    removed = pb.prune_local(tmp_path, 14, now)
    assert len(removed) == 2 and len(list(tmp_path.glob(f"{pb.PREFIX}*"))) == 2 and (tmp_path / "fremd.txt").exists()
    ident = x25519.Identity.generate()
    (tmp_path / "plain").write_bytes(b"geheim")
    pb.encrypt(tmp_path / "plain", tmp_path / "enc", ident.to_public())
    pb.decrypt(tmp_path / "enc", tmp_path / "dec", f"# kommentar\n{ident}\n")
    assert (tmp_path / "dec").read_bytes() == b"geheim"
    assert pb.libpq_url("postgresql+asyncpg://u:p@h:5432/d") == "postgresql://u:p@h:5432/d"


async def test_request_backup_api_superuser_only(client, msp):
    from tests.conftest import make_tenant, make_tenant_admin

    r = await client.post("/api/v1/platform/backups", headers=msp)
    assert r.status_code == 202 and r.json()["status"] == "queued"
    assert (await client.post("/api/v1/platform/backups", headers=msp)).status_code == 409
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    assert (await client.get("/api/v1/platform/backups", headers=h)).status_code == 403
    async with system_session() as db:
        assert (await db.execute(select(PlatformBackup))).scalars().one().trigger == "manual"
