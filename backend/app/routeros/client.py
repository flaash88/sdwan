"""RouterOS-API-Zugriff – ausschließlich über den WireGuard-Management-Tunnel.

``connect_device(device)`` liefert eine asynchrone Verbindung. Zwei Backends:

* ``api``       – librouteros (synchron, in einem Thread ausgeführt)
* ``simulator`` – In-Memory-Router für Demo-Betrieb und Tests

Harte Regel: Es wird nur zu Adressen im Management-Netz (settings.wg_network) verbunden.
Jeder Versuch, eine andere Adresse anzusprechen, wirft ``RouterOSError``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

from app.config import get_settings
from app.security import decrypt_secret

log = logging.getLogger(__name__)

MANAGED_PREFIX = "sdwan:"


class RouterOSError(Exception):
    pass


class Connection(Protocol):
    async def call(self, cmd: str, **params: Any) -> list[dict[str, Any]]: ...

    async def close(self) -> None: ...


def _norm(value: Any) -> str:
    """Vergleichbare String-Form (librouteros liefert bool/int statt 'yes'/'10')."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        value = ",".join(str(v) for v in value)
    v = str(value)
    return {"yes": "true", "no": "false"}.get(v, v)


def _to_ros(value: Any) -> Any:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return value


class LibRouterOSConnection:
    def __init__(self, api: Any) -> None:
        self._api = api
        self._lock = asyncio.Lock()

    async def call(self, cmd: str, **params: Any) -> list[dict[str, Any]]:
        params = {k: _to_ros(v) for k, v in params.items() if v is not None}

        def _run() -> list[dict[str, Any]]:
            return list(self._api(cmd, **params))

        async with self._lock:
            try:
                return await asyncio.to_thread(_run)
            except Exception as exc:  # librouteros wirft diverse Typen
                raise RouterOSError(f"{cmd}: {exc}") from exc

    async def close(self) -> None:
        await asyncio.to_thread(self._api.close)


def assert_tunnel_address(host: str) -> None:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise RouterOSError(f"Ungültige Adresse {host!r}") from exc
    if ip not in get_settings().wg_net:
        raise RouterOSError(f"{host} liegt nicht im Management-Tunnel – Zugriff verweigert")


def get_backend() -> str:
    return get_settings().routeros_backend


async def open_connection(host: str, username: str, password: str) -> Connection:
    assert_tunnel_address(host)
    s = get_settings()
    if s.routeros_backend == "simulator":
        from app.routeros.simulator import SimulatedConnection

        conn = SimulatedConnection(host)
        await conn.load()
        return conn

    def _connect() -> Any:
        from librouteros import connect

        return connect(host=host, username=username, password=password, port=s.routeros_api_port, timeout=s.routeros_timeout)

    try:
        api = await asyncio.wait_for(asyncio.to_thread(_connect), timeout=s.routeros_timeout + 2)
    except Exception as exc:
        raise RouterOSError(f"Verbindung zu {host} fehlgeschlagen: {exc}") from exc
    return LibRouterOSConnection(api)


@asynccontextmanager
async def connect_device(device: Any) -> AsyncIterator[DeviceAPI]:
    if not device.api_password_enc:
        raise RouterOSError("Gerät ist noch nicht gepairt (keine API-Zugangsdaten)")
    conn = await open_connection(device.tunnel_ip, get_settings().routeros_api_user, decrypt_secret(device.api_password_enc))
    try:
        yield DeviceAPI(conn)
    finally:
        try:
            await conn.close()
        except Exception:  # pragma: no cover
            pass


class DeviceAPI:
    """Komfortschicht mit idempotenten Operationen über einer RouterOS-Verbindung."""

    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    async def call(self, cmd: str, **params: Any) -> list[dict[str, Any]]:
        return await self.conn.call(cmd, **params)

    async def print(self, path: str, **match: Any) -> list[dict[str, Any]]:
        rows = await self.conn.call(f"{path}/print")
        if match:
            rows = [r for r in rows if all(_norm(r.get(k)) == _norm(v) for k, v in match.items())]
        return rows

    async def add(self, path: str, **attrs: Any) -> str:
        res = await self.conn.call(f"{path}/add", **attrs)
        return (res[0].get("ret") if res else "") or ""

    async def set(self, path: str, item_id: str, **attrs: Any) -> None:
        await self.conn.call(f"{path}/set", **{".id": item_id}, **attrs)

    async def remove(self, path: str, item_id: str) -> None:
        await self.conn.call(f"{path}/remove", **{".id": item_id})

    async def resource(self) -> dict[str, Any]:
        rows = await self.conn.call("/system/resource/print")
        return rows[0] if rows else {}

    async def sync_managed(
        self,
        path: str,
        tag: str,
        desired: list[dict[str, Any]],
        key: str = "comment",
        ordered: bool = False,
    ) -> dict[str, int]:
        """Gleicht alle Einträge unter ``path`` mit Kommentar ``sdwan:<tag>...`` an ``desired`` an.

        Jeder gewünschte Eintrag MUSS einen eindeutigen ``comment`` haben, der mit
        ``sdwan:<tag>`` beginnt. Nicht verwaltete Einträge (andere Kommentare) bleiben
        unangetastet. Bei ``ordered=True`` werden verwaltete Einträge komplett neu
        angelegt (Reihenfolge ist z. B. bei Firewall-Regeln relevant).
        """
        prefix = MANAGED_PREFIX + tag
        existing = [r for r in await self.print(path) if str(r.get(key, "")).startswith(prefix)]
        stats = {"added": 0, "updated": 0, "removed": 0}
        for d in desired:
            if not str(d.get(key, "")).startswith(prefix):
                raise ValueError(f"desired entry without managed comment: {d}")
        if ordered:
            for r in existing:
                await self.remove(path, r[".id"])
                stats["removed"] += 1
            for d in desired:
                await self.add(path, **d)
                stats["added"] += 1
            return stats
        by_key = {r.get(key): r for r in existing}
        wanted = {d[key] for d in desired}
        for d in desired:
            cur = by_key.get(d[key])
            if cur is None:
                await self.add(path, **d)
                stats["added"] += 1
            else:
                diff = {k: v for k, v in d.items() if _norm(cur.get(k)) != _norm(v)}
                if diff:
                    await self.set(path, cur[".id"], **diff)
                    stats["updated"] += 1
        for r in existing:
            if r.get(key) not in wanted:
                await self.remove(path, r[".id"])
                stats["removed"] += 1
        return stats
