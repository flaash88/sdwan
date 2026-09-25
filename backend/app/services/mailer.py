"""E-Mail-Versand (SMTP). Ohne SMTP_HOST landen Mails nur im Log/``outbox`` (Dev/Tests).

Mit ``html`` wird eine multipart/alternative-Mail erzeugt: Klartext zuerst (Fallback für Clients ohne
HTML), dann die HTML-Variante; Anhänge machen daraus multipart/mixed.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.config import get_settings

log = logging.getLogger(__name__)
outbox: list[EmailMessage] = []


def _send_sync(msg: EmailMessage) -> None:
    s = get_settings()
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
        if s.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password)
        smtp.send_message(msg)


async def send_mail(to: list[str], subject: str, body: str, attachments: list[tuple[str, bytes, str]] | None = None,
                    html: str | None = None) -> bool:
    if not to:
        return False
    s = get_settings()
    msg = EmailMessage()
    msg["From"] = s.smtp_from
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    for name, data, mime in attachments or []:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    if not s.smtp_host:
        outbox.append(msg)
        del outbox[:-200]
        log.info("SMTP nicht konfiguriert – Mail an %s nur protokolliert: %s", to, subject)
        return True
    try:
        await asyncio.to_thread(_send_sync, msg)
        return True
    except (OSError, smtplib.SMTPException) as exc:
        log.error("Mailversand an %s fehlgeschlagen: %s", to, exc)
        return False
