"""Webhook-Benachrichtigung je Alert-Regel (Phase 11, zusätzlich zur E-Mail).

Formate:
* ``generic`` – flaches JSON (``event``, ``title``, ``text``, ``severity`` …); ``text`` wird von
  Slack/Mattermost direkt dargestellt.
* ``teams``   – Nachricht mit Adaptive Card (``type: message`` + ``attachments``), wie sie Teams-Workflows
  ("Send webhook alerts to a channel") und klassische Incoming Webhooks annehmen.

Sicherheit: nur ``https``, keine Zugangsdaten in der URL, Ziel darf nicht auf private/Loopback-/Link-Local-
Adressen auflösen (SSRF-Schutz, bei jedem Versand geprüft). Die URL wird verschlüsselt gespeichert, weil
Workflow-URLs eine Signatur enthalten.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)
FORMATS = ("generic", "teams")
COLORS = {"critical": "Attention", "warning": "Warning", "info": "Accent"}

# Tests: httpx.MockTransport einsetzen; jede gesendete Nachricht landet zusätzlich in ``sent``
transport: httpx.AsyncBaseTransport | None = None
sent: list[dict[str, Any]] = []


class WebhookError(ValueError):
    pass


def _bad_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified


def validate_url(url: str) -> str:
    u = urlsplit(url.strip())
    if u.scheme != "https" or not u.hostname:
        raise WebhookError("Webhook-URL muss mit https:// beginnen")
    if u.username or u.password:
        raise WebhookError("Keine Zugangsdaten in der Webhook-URL")
    host = u.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise WebhookError("Webhook-Ziel darf nicht intern sein")
    try:
        if _bad_ip(ipaddress.ip_address(host)):
            raise WebhookError("Webhook-Ziel darf nicht im privaten Netz liegen")
    except ValueError as exc:
        if isinstance(exc, WebhookError):
            raise
    return url.strip()


async def _check_resolved(host: str) -> None:
    infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    for info in infos:
        if _bad_ip(ipaddress.ip_address(info[4][0])):
            raise WebhookError(f"{host} löst auf eine interne Adresse auf")


def mask(url: str | None) -> str | None:
    if not url:
        return None
    u = urlsplit(url)
    return f"{u.scheme}://{u.hostname}/…"


def build_payload(fmt: str, *, title: str, text: str, severity: str, resolved: bool, facts: dict[str, str], link: str | None,
                  extra: dict[str, Any]) -> dict[str, Any]:
    if fmt == "teams":
        body: list[dict[str, Any]] = [
            {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True,
             "color": "Good" if resolved else COLORS.get(severity, "Default")},
            {"type": "TextBlock", "text": text, "wrap": True},
            {"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in facts.items() if v]},
        ]
        card: dict[str, Any] = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4", "body": body}
        if link:
            card["actions"] = [{"type": "Action.OpenUrl", "title": "Im Portal öffnen", "url": link}]
        return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}]}
    return {"title": title, "text": f"{title}\n{text}", "severity": severity, "status": "resolved" if resolved else "firing",
            "facts": facts, "url": link, **extra}


async def send(url: str, payload: dict[str, Any]) -> bool:
    try:
        validate_url(url)
        if transport is None:
            await _check_resolved(urlsplit(url).hostname or "")
        async with httpx.AsyncClient(transport=transport, timeout=10, follow_redirects=False) as client:
            r = await client.post(url, json=payload, headers={"User-Agent": "sdwan-alerts"})
        sent.append({"url": url, "payload": payload, "status": r.status_code})
        if r.status_code >= 300:
            log.warning("Webhook %s antwortet %s", mask(url), r.status_code)
            return False
        return True
    except (httpx.HTTPError, OSError, WebhookError) as exc:
        log.warning("Webhook %s fehlgeschlagen: %s", mask(url), exc)
        return False
