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

import ipaddress
import logging
from typing import Any
from urllib.parse import urlsplit

import httpx

from app import net_guard

log = logging.getLogger(__name__)
FORMATS = ("generic", "teams")
COLORS = {"critical": "Attention", "warning": "Warning", "info": "Accent"}

# Tests: httpx.MockTransport einsetzen; jede gesendete Nachricht landet zusätzlich in ``sent``
transport: httpx.AsyncBaseTransport | None = None
sent: list[dict[str, Any]] = []


class WebhookError(ValueError):
    pass


def _bad_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Nicht öffentlich = gesperrt (inkl. CGNAT 100.64/10, Dokumentationsnetze; AUDIT-012)."""
    return not net_guard.is_public(ip)


def validate_url(url: str) -> str:
    u = urlsplit(url.strip())
    if u.scheme != "https" or not u.hostname:
        raise WebhookError("Webhook-URL muss mit https:// beginnen")
    if u.username or u.password:
        raise WebhookError("Keine Zugangsdaten in der Webhook-URL")
    try:
        net_guard.check_url(url, ("https",))  # intern/privat/CGNAT, Dienstnamen ohne Punkt (AUDIT-012)
    except net_guard.GuardError as exc:
        raise WebhookError(f"Webhook-Ziel: {exc}") from exc
    return url.strip()


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
        # Verbindung an die geprüfte Adresse gepinnt (kein DNS-Rebinding, AUDIT-012); keine Redirects
        async with net_guard.stream("POST", url, schemes=("https",), max_redirects=0, timeout=10, json=payload,
                                    headers={"User-Agent": "sdwan-alerts"}, transport=transport) as r:
            status = r.status_code
        sent.append({"url": url, "payload": payload, "status": status})
        if status >= 300:
            log.warning("Webhook %s antwortet %s", mask(url), status)
            return False
        return True
    except (httpx.HTTPError, OSError, WebhookError, net_guard.GuardError) as exc:
        log.warning("Webhook %s fehlgeschlagen: %s", mask(url), exc)
        return False
