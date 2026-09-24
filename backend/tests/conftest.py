from __future__ import annotations

import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ.update(
    {
        "DATABASE_URL": os.environ.get("TEST_DATABASE_URL", f"sqlite+aiosqlite:///{_tmp}/test.db"),
        "USE_REDIS": "false",
        "ROUTEROS_BACKEND": "simulator",
        "DB_AUTO_CREATE": "true",
        "INFLUX_ENABLED": "false",
        "PUBLIC_URL": "https://cloud.test",
        "WG_HUB_ENDPOINT": "hub.test",
        "HUB_TOKEN": "hubtoken",
        "BOOTSTRAP_ADMIN_EMAIL": "msp@test.example.com",
        "BOOTSTRAP_ADMIN_PASSWORD": "mspadmin123",
        "SMTP_HOST": "",
        "NEXTDNS_API_KEY": "",
    }
)

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.bootstrap import ensure_bootstrap_admin  # noqa: E402
from app.db import Base, get_engine, reset_engine  # noqa: E402
from app.main import app  # noqa: E402
from app.routeros import simulator  # noqa: E402
from app.services.wireguard import generate_keypair  # noqa: E402

HUB_PRIV, HUB_PUB = generate_keypair()


@pytest.fixture(autouse=True)
async def fresh_db():
    import app.models  # noqa: F401

    reset_engine()  # Engine pro Test (jeder Test hat seinen eigenen Event-Loop)
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    simulator.reset()
    await ensure_bootstrap_admin()
    yield
    await get_engine().dispose()


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def login(client: httpx.AsyncClient, email: str, password: str) -> dict[str, str]:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
async def msp(client):
    return await login(client, "msp@test.example.com", "mspadmin123")


@pytest.fixture
async def hub(client):
    r = await client.put("/api/v1/internal/hub/register", json={"public_key": HUB_PUB, "endpoint": "hub.test"},
                         headers={"X-Hub-Token": "hubtoken"})
    assert r.status_code == 200, r.text
    return HUB_PUB


async def make_tenant(client, msp, slug="acme") -> dict:
    r = await client.post("/api/v1/tenants", json={"name": slug.title(), "slug": slug}, headers=msp)
    assert r.status_code == 201, r.text
    return r.json()


async def make_tenant_admin(client, msp, tenant_id, email="admin@acme.example.com", role="admin") -> dict[str, str]:
    r = await client.post("/api/v1/users", json={"email": email, "password": "password123", "role": role, "tenant_id": tenant_id}, headers=msp)
    assert r.status_code == 201, r.text
    return await login(client, email, "password123")


async def make_paired_device(client, headers, site_id=None, name="rtr1") -> dict:
    r = await client.post("/api/v1/devices", json={"name": name, "site_id": site_id}, headers=headers)
    assert r.status_code == 201, r.text
    dev = r.json()["device"]
    r = await client.post(f"/api/v1/devices/{dev['id']}/simulate-pair", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()

