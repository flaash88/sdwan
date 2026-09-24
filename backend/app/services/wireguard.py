"""WireGuard-Hilfsfunktionen: Keys, IP-Vergabe im Management- und Mesh-Netz."""

from __future__ import annotations

import base64
import ipaddress
import re

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Device, SystemSetting

_WG_KEY_RE = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$")


def is_valid_wg_key(key: str | None) -> bool:
    return bool(key) and bool(_WG_KEY_RE.match(key))  # type: ignore[arg-type]


def generate_keypair() -> tuple[str, str]:
    """Erzeugt (private, public) als Base64 – z. B. für Hub oder Tests."""
    priv = X25519PrivateKey.generate()
    priv_b = priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    pub_b = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(priv_b).decode(), base64.b64encode(pub_b).decode()


def generate_psk() -> str:
    import os

    return base64.b64encode(os.urandom(32)).decode()


async def allocate_tunnel_ip(db: AsyncSession) -> str:
    """Nächste freie Management-Tunnel-IP (global über alle Tenants, da ein gemeinsamer Hub)."""
    s = get_settings()
    net = s.wg_net
    rows = await db.execute(select(Device.tunnel_ip).execution_options(skip_tenant_filter=True))
    used = {r[0] for r in rows}
    used.add(s.wg_hub_ip)
    for host in net.hosts():
        ip = str(host)
        if ip not in used:
            return ip
    raise RuntimeError("Management-Netz erschöpft")


async def get_hub_public_key(db: AsyncSession) -> str | None:
    row = await db.get(SystemSetting, "hub")
    return (row.value or {}).get("public_key") if row else None


def mesh_subnet_for_index(index: int) -> str:
    """Tenant-Transfernetz: das index-te /24 aus settings.mesh_network."""
    net = ipaddress.ip_network(get_settings().mesh_network)
    subnets = list(net.subnets(new_prefix=24))
    return str(subnets[index])
