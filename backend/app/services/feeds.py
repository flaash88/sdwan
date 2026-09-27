"""Threat-Feeds (Phase 15): laden, prüfen, differenziell verteilen.

* Laden nur für Feeds mit mindestens einer Zuweisung; Größen- und Zeitlimit; Fehler lassen die letzte gültige
  Liste aktiv (``last_error`` wird gesetzt, ``last_ok_at`` bleibt).
* Prüfung: nur gültige IPv4/IPv6-Netze, nur öffentlich routbare (keine privaten/reservierten Netze – Schutz vor
  Selbstaussperrung), keine zu weit gefassten Präfixe, Obergrenze je Liste.
* Verteilung: Address-List ``sdwan-feed-<slug>`` (Kommentar ``sdwan:feed:<slug>``), IPv6 in
  ``/ipv6/firewall/address-list``; nur Differenzen (add/remove). Vorher RAM-Check je Gerät.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import system_session, utcnow
from app.models import Device, DeviceStatus, PairingStatus, ThreatFeed, ThreatFeedAssignment
from app.routeros import RouterOSError, connect_device
from app.routeros.client import DeviceAPI
from app.routeros.naming import routeros_safe_name

log = logging.getLogger(__name__)
PATH_V4 = "/ip/firewall/address-list"
PATH_V6 = "/ipv6/firewall/address-list"  # ANNAHME (Labor): gleiche Felder wie IPv4
MIN_PREFIX = {4: 8, 6: 16}  # breitere Netze werden verworfen


class FeedError(Exception):
    pass


def list_name(slug: str) -> str:
    return routeros_safe_name(f"sdwan-feed-{slug}")


def validate_net(value: str) -> str | None:
    """Gültiges, öffentlich routbares Netz → normalisierter Text; sonst None."""
    try:
        net = ipaddress.ip_network(value.strip(), strict=False)
    except ValueError:
        return None
    if not net.is_global or net.is_multicast or net.prefixlen < MIN_PREFIX[net.version]:
        return None
    return str(net)


def parse_feed(text: str, fmt: str = "lines", json_field: str = "cidr", comment_chars: str = "#;") -> tuple[list[str], int]:
    """→ (gültige Netze sortiert/eindeutig, Anzahl verworfener Zeilen)."""
    out: set[str] = set()
    rejected = 0
    for line in text.splitlines():
        raw = line.strip()
        if not raw:
            continue
        if fmt == "jsonl":
            try:
                obj = json.loads(raw)
            except ValueError:
                rejected += 1
                continue
            if not isinstance(obj, dict) or json_field not in obj:
                continue  # z. B. Metadatenzeile am Ende
            candidate = str(obj[json_field])
        else:
            for c in comment_chars:
                raw = raw.split(c, 1)[0]
            raw = raw.strip()
            if not raw:
                continue
            candidate = raw.split()[0]
        net = validate_net(candidate)
        if net is None:
            rejected += 1
        else:
            out.add(net)
    return sorted(out, key=lambda n: (ipaddress.ip_network(n).version, ipaddress.ip_network(n))), rejected


async def _download(url: str) -> str:
    import httpx

    limit = get_settings().feed_max_download_mb * 1024 * 1024
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        async with client.stream("GET", url, headers={"User-Agent": "MikroTik-Fleet-Management/threat-feed"}) as r:
            r.raise_for_status()
            chunks, size = [], 0
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    raise FeedError(f"Download größer als {get_settings().feed_max_download_mb} MB")
                chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


async def refresh_feed(feed: ThreatFeed) -> bool:
    """Lädt und prüft; True, wenn sich die Liste geändert hat. Fehler setzen nur ``last_error``."""
    feed.last_fetch_at = utcnow()
    try:
        text = await _download(feed.url)
        nets, rejected = parse_feed(text, feed.fmt, feed.json_field, feed.comment_chars)
        if not nets:
            raise FeedError("Keine gültigen Einträge gefunden")
        if len(nets) > feed.max_entries:
            raise FeedError(f"{len(nets)} Einträge – mehr als die Obergrenze {feed.max_entries}")
    except Exception as exc:  # noqa: BLE001 - jeder Fehler lässt die alte Liste aktiv
        feed.last_error = str(exc)[:500]
        log.warning("Threat-Feed %s: %s", feed.name, exc)
        return False
    digest = hashlib.sha256("\n".join(nets).encode()).hexdigest()
    changed = digest != feed.entries_hash
    feed.entries, feed.entries_hash, feed.rejected = nets, digest, rejected
    feed.last_ok_at, feed.last_error = utcnow(), None
    return changed


def _norm(addr: str) -> str:
    a = str(addr)
    return a[:-3] if a.endswith("/32") else a[:-4] if a.endswith("/128") else a


def memory_needed(entries: int) -> int:
    s = get_settings()
    return entries * s.feed_bytes_per_entry + s.feed_min_free_mb * 1024 * 1024


async def sync_list(api: DeviceAPI, slug: str, nets: list[str]) -> dict[str, int]:
    """Nur Differenzen: fehlende Einträge anlegen, überzählige (nur verwaltete dieses Feeds) entfernen."""
    name, tag = list_name(slug), f"sdwan:feed:{slug}"
    stats = {"added": 0, "removed": 0}
    want_by_path: dict[str, set[str]] = {PATH_V4: set(), PATH_V6: set()}
    for n in nets:
        want_by_path[PATH_V6 if ":" in n else PATH_V4].add(_norm(n))
    for path, want in want_by_path.items():
        try:
            rows = await api.print(path)
        except RouterOSError:
            if want:
                raise
            continue
        have: dict[str, list[str]] = {}
        for r in rows:
            if r.get("list") == name and str(r.get("comment", "")) == tag:
                have.setdefault(_norm(str(r.get("address"))), []).append(str(r[".id"]))
        for addr in sorted(want - set(have)):
            await api.add(path, list=name, address=addr, comment=tag)
            stats["added"] += 1
        for addr, ids in have.items():
            for i in ids if addr not in want else ids[1:]:  # auch Duplikate entfernen
                await api.remove(path, i)
                stats["removed"] += 1
    return stats


async def sync_assignment(db: AsyncSession, feed: ThreatFeed, a: ThreatFeedAssignment, device: Device, force: bool = False) -> None:
    if not force and a.status == "ok" and a.synced_hash == feed.entries_hash:
        return
    if not feed.entries_hash:
        return  # noch nie erfolgreich geladen
    a.last_sync_at = utcnow()
    try:
        async with connect_device(device) as api:
            res = await api.resource()
            free = int(str(res.get("free-memory") or 0))
            need = memory_needed(len(feed.entries))
            if free < need:
                a.status = "skipped_memory"
                a.last_error = f"Zu wenig freier Speicher: {free // 2**20} MB frei, benötigt ca. {need // 2**20} MB – Gerät übersprungen"
                return
            stats = await sync_list(api, feed.slug, feed.entries)
        a.status, a.synced_hash, a.synced_count, a.last_error = "ok", feed.entries_hash, len(feed.entries), None
        log.info("Feed %s -> %s: %s", feed.slug, device.name, stats)
    except RouterOSError as exc:
        a.status, a.last_error = "error", str(exc)[:500]


async def remove_from_device(device: Device, slug: str) -> None:
    async with connect_device(device) as api:
        await sync_list(api, slug, [])


async def feeds_tick() -> None:
    """Worker-Job: fällige Feeds laden und an zugewiesene Geräte verteilen."""
    now = utcnow()
    async with system_session() as db:
        assigns = (await db.execute(select(ThreatFeedAssignment))).scalars().all()
        by_feed: dict[Any, list[ThreatFeedAssignment]] = {}
        for a in assigns:
            by_feed.setdefault(a.feed_id, []).append(a)
        for feed in (await db.execute(select(ThreatFeed).where(ThreatFeed.enabled.is_(True)))).scalars().all():
            items = by_feed.get(feed.id)
            if not items:
                continue
            due = feed.last_fetch_at is None or (now - feed.last_fetch_at).total_seconds() >= feed.interval_min * 60
            if due:
                await refresh_feed(feed)
            for a in items:
                dev = await db.get(Device, a.device_id)
                if dev and dev.pairing_status == PairingStatus.paired and dev.status == DeviceStatus.online:
                    await sync_assignment(db, feed, a, dev)
            await db.commit()
