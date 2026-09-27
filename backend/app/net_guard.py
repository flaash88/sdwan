"""SSRF-Schutz für ausgehende HTTP-Abrufe mit vom Benutzer gewählter URL (AUDIT-004/012): Threat-Feeds, Webhooks.

* Nur ``http``/``https`` (Webhooks: nur https), keine Zugangsdaten in der URL, Hostname voll qualifiziert (mit Punkt) –
  ``localhost``, ``*.local``/``*.internal`` und Docker-Dienstnamen (``api``, ``influxdb`` …) sind ausgeschlossen.
* Jede aufgelöste Adresse muss **global** sein (``ipaddress.is_global``): kein Loopback, privat, Link-Local, CGNAT
  (100.64/10), Dokumentations- oder reservierte Netze – damit auch nicht das Management-Netz der Router oder das Docker-Netz.
* Die Verbindung geht an genau die geprüfte Adresse (kein erneutes Auflösen durch httpx → kein DNS-Rebinding). Bei https
  bleiben SNI und Zertifikatsprüfung auf den Hostnamen bezogen (``sni_hostname``), der ``Host``-Header ist der Originalname.
* Redirects werden nicht automatisch verfolgt; ``fetch`` folgt höchstens ``max_redirects`` und prüft jeden Hop neu.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

IP = ipaddress.IPv4Address | ipaddress.IPv6Address
_INTERNAL_SUFFIX = (".localhost", ".local", ".internal", ".lan", ".home", ".corp", ".intranet")


class GuardError(ValueError):
    pass


def is_public(ip: IP) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not ip.is_multicast


def check_url(url: str, schemes: tuple[str, ...] = ("http", "https")) -> tuple[str, str, int]:
    """Statische Prüfung → (scheme, host, port). Hostname-Auflösung prüft :func:`resolve_public`."""
    u = urlsplit(url.strip())
    if u.scheme not in schemes or not u.hostname:
        raise GuardError(f"URL muss mit {' oder '.join(s + '://' for s in schemes)} beginnen")
    if u.username or u.password:
        raise GuardError("Keine Zugangsdaten in der URL")
    host = u.hostname.lower().rstrip(".")
    try:
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError as exc:
        raise GuardError("Ungültiger Port") from exc
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if host == "localhost" or host.endswith(_INTERNAL_SUFFIX) or "." not in host:
            raise GuardError("Ziel darf nicht intern sein (voll qualifizierten Hostnamen verwenden)") from None
        return u.scheme, host, port
    if not is_public(ip):
        raise GuardError("Ziel darf nicht im privaten/internen Netz liegen")
    return u.scheme, host, port


async def resolve_public(host: str, port: int) -> str:
    """Hostname auflösen; ALLE Adressen müssen öffentlich sein. Liefert die Adresse, mit der verbunden wird."""
    literal: IP | None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not is_public(literal):
            raise GuardError(f"{host}: interne Adresse")
        return str(literal)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise GuardError(f"{host} nicht auflösbar: {exc}") from exc
    addrs = [ipaddress.ip_address(i[4][0]) for i in infos]
    if not addrs or not all(is_public(a) for a in addrs):
        raise GuardError(f"{host} löst auf eine interne Adresse auf")
    return str(addrs[0])


async def validate(url: str, schemes: tuple[str, ...] = ("http", "https"), resolve: bool = True) -> str:
    """Für Formulare: statisch prüfen und – wenn auflösbar – auf öffentliche Adressen prüfen. Nicht auflösbare Namen
    werden hier akzeptiert (Prüfung beim Abruf)."""
    _scheme, host, port = check_url(url, schemes)
    if resolve:
        try:
            await resolve_public(host, port)
        except GuardError as exc:
            if "nicht auflösbar" not in str(exc):
                raise
    return url.strip()


def _pinned(url: str, ip: str) -> tuple[str, dict[str, str], dict[str, Any]]:
    u = urlsplit(url)
    host = u.hostname or ""
    netloc_ip = f"[{ip}]" if ":" in ip else ip
    if u.port:
        netloc_ip += f":{u.port}"
    target = urlunsplit((u.scheme, netloc_ip, u.path or "/", u.query, ""))
    host_header = host + (f":{u.port}" if u.port else "")
    ext = {"sni_hostname": host} if u.scheme == "https" else {}
    return target, {"Host": host_header}, ext


@asynccontextmanager
async def stream(method: str, url: str, *, schemes: tuple[str, ...] = ("http", "https"), max_redirects: int = 3,
                 timeout: float = 30, headers: dict[str, str] | None = None, json: Any = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> AsyncIterator[httpx.Response]:
    """Wie ``httpx.AsyncClient.stream`` – mit Prüfung und IP-Pinning je Hop. ``transport`` (Tests): ohne DNS/Pinning."""
    current = url
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=transport) as client:
        for _hop in range(max_redirects + 1):
            _scheme, host, port = check_url(current, schemes)
            if transport is None:
                ip = await resolve_public(host, port)
                target, extra_headers, ext = _pinned(current, ip)
            else:
                target, extra_headers, ext = current, {}, {}
            req = client.build_request(method, target, headers={**(headers or {}), **extra_headers}, json=json, extensions=ext)
            resp = await client.send(req, stream=True)
            if resp.is_redirect and resp.headers.get("location"):
                nxt = urljoin(current, resp.headers["location"])
                await resp.aclose()
                current = nxt
                method, json = ("GET", None) if resp.status_code in (301, 302, 303) else (method, json)
                continue
            try:
                yield resp
            finally:
                await resp.aclose()
            return
    raise GuardError(f"Zu viele Weiterleitungen (> {max_redirects})")
