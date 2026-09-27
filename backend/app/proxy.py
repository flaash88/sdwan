"""Client-Adresse hinter Reverse-Proxys (AUDIT-003).

``X-Forwarded-For`` wird nur ausgewertet, wenn die direkte Gegenstelle in ``TRUSTED_PROXIES`` steht (IPs/CIDR, kommagetrennt;
leer = keinem Proxy vertrauen). Die Liste wird dann von rechts nach links gelesen: vertrauenswürdige Hops werden
übersprungen, der erste fremde Eintrag ist der Client. Einträge weiter links hat der Client selbst geschrieben – sie
zählen nie (nginx hängt mit ``$proxy_add_x_forwarded_for`` nur an). ``X-Forwarded-Proto`` wird ebenfalls nur von einem
vertrauenswürdigen Proxy übernommen.

Uvicorn läuft deshalb mit ``--no-proxy-headers``: Die Auswertung passiert ausschließlich hier. Wirkt auf alle Stellen,
die ``request.client.host`` lesen (Login-Limit, Hotspot-Registrierung, Audit-IP, API-Token ``last_used_ip``, Default
der Fernzugriffs-Bindung).
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Iterable
from typing import Any

log = logging.getLogger(__name__)

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_trusted(value: str | Iterable[str]) -> list[Network]:
    items = value.split(",") if isinstance(value, str) else list(value)
    out: list[Network] = []
    for raw in items:
        item = raw.strip()
        if not item:
            continue
        try:
            out.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            log.error("TRUSTED_PROXIES: '%s' ist keine IP-Adresse/kein Netz – ignoriert", item)
    return out


def _is_trusted(host: str | None, trusted: list[Network]) -> bool:
    if not host or not trusted:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in n for n in trusted)


def client_from_headers(peer: str | None, xff: str | None, trusted: list[Network]) -> str | None:
    """Echte Client-Adresse: ``peer`` selbst, außer ``peer`` ist ein vertrauenswürdiger Proxy."""
    if not _is_trusted(peer, trusted) or not xff:
        return peer
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    for hop in reversed(hops):
        if _is_trusted(hop, trusted):
            continue
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            return peer  # unlesbarer Eintrag: nicht raten, sondern den Proxy selbst nehmen
        return hop
    return hops[0] if hops else peer  # alle Hops vertrauenswürdig (interner Aufruf)


class TrustedProxyMiddleware:
    def __init__(self, app: Any, trusted: str | Iterable[str] = "") -> None:
        self.app = app
        self.trusted = parse_trusted(trusted)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") in ("http", "websocket"):
            client = scope.get("client")
            peer = client[0] if client else None
            if _is_trusted(peer, self.trusted):
                headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
                real = client_from_headers(peer, headers.get("x-forwarded-for"), self.trusted)
                scope = dict(scope)
                if real and real != peer:
                    scope["client"] = (real, 0)
                proto = (headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
                if proto in ("http", "https"):
                    scope["scheme"] = ("wss" if proto == "https" else "ws") if scope["type"] == "websocket" else proto
        await self.app(scope, receive, send)
