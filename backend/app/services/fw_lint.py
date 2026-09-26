"""Firewall-Editor (Phase 14): Prüfung (Lint) einer einfachen Policy.

Stufen: ``error`` (Deploy nur nach Bestätigung), ``warn``, ``info``. Reine Funktion; gerätebezogene Prüfungen
bekommen den Kontext der Zielgeräte übergeben (``DeviceCtx``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.fw_compile import ROUTER, Catalog, SpecError, addresses, normalize_spec, service_entries


@dataclass
class DeviceCtx:
    name: str
    zone_ids: set[str] = field(default_factory=set)  # Zonen mit mindestens einem Interface auf dem Gerät
    has_wan: bool = False  # Interface-List sdwan-wan existiert (WAN konfiguriert)
    later_policies: int = 0  # Policies, die auf diesem Gerät NACH dieser Policy kommen
    has_hotspot: bool = False  # Gäste-Portal (Phase 20) auf dem Gerät


def _i(level: str, code: str, msg: str, rule: str | None = None) -> dict[str, Any]:
    return {"level": level, "code": code, "message": msg, "rule_id": rule}


def _covers(a: list[str], b: list[str]) -> bool:
    """a (leer = alles) umfasst b?"""
    return not a or (bool(b) and set(b) <= set(a))


def _zone_covers(a: str | None, b: str | None) -> bool:
    return a is None or a == b


def lint_spec(spec: dict[str, Any], cat: Catalog, devices: list[DeviceCtx] | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        spec = normalize_spec(spec)
    except SpecError as exc:
        return [_i("error", "invalid", str(exc))]
    rules = [r for r in spec["rules"] if r["enabled"]]
    zname = {zid: z["name"] for zid, z in cat.zones.items()}

    def label(r: dict[str, Any]) -> str:
        return r["comment"] or f"Regel {r['id']}"

    # verdeckte und doppelte Regeln
    for j, b in enumerate(rules):
        for a in rules[:j]:
            same_chain = (a["dst_zone"] == ROUTER) == (b["dst_zone"] == ROUTER)
            if not same_chain:
                continue
            if (_zone_covers(a["src_zone"], b["src_zone"]) and _zone_covers(a["dst_zone"], b["dst_zone"])
                    and _covers(a["src"], b["src"]) and _covers(a["dst"], b["dst"]) and _covers(a["services"], b["services"])):
                exact = (a["src_zone"], a["dst_zone"], sorted(a["src"]), sorted(a["dst"]), sorted(a["services"])) == \
                        (b["src_zone"], b["dst_zone"], sorted(b["src"]), sorted(b["dst"]), sorted(b["services"]))
                if exact and a["action"] == b["action"]:
                    out.append(_i("warn", "duplicate", f"„{label(b)}“ ist doppelt (gleich wie „{label(a)}“)", b["id"]))
                else:
                    out.append(_i("warn", "shadowed", f"„{label(b)}“ wird nie erreicht – „{label(a)}“ davor trifft alle Pakete", b["id"]))
                break
    # any -> any accept
    for r in rules:
        if r["action"] == "accept" and r["dst_zone"] != ROUTER and not any((r["src_zone"], r["dst_zone"], r["src"], r["dst"], r["services"])):
            out.append(_i("error", "any_any", f"„{label(r)}“ erlaubt alles von überall nach überall", r["id"]))
    # leere Objekte / Dienste
    used = {i for r in spec["rules"] for i in r["src"] + r["dst"]} | {n["host"] for n in spec["nat"] if n.get("host")}
    for oid in sorted(used):
        o = cat.objects.get(oid)
        if o is None:
            out.append(_i("error", "missing_object", f"Objekt {oid} existiert nicht mehr"))
            continue
        if o["kind"] == "feed":
            out.append(_i("info", "feed_used", f"„{o['name']}“: Der Threat-Feed muss den Zielgeräten zugewiesen sein (Seite Threat-Feeds), "
                                               "sonst ist die Liste auf dem Gerät leer"))
        try:
            if o["kind"] != "feed" and not addresses(cat, oid):
                out.append(_i("warn", "empty_object", f"Objekt „{o['name']}“ enthält keine Adressen – Regeln damit treffen nie"))
        except SpecError as exc:
            out.append(_i("error", "invalid_object", str(exc)))
    for sid in sorted({s for r in spec["rules"] for s in r["services"]}):
        if sid not in cat.services:
            out.append(_i("error", "missing_service", f"Dienst {sid} existiert nicht mehr"))
        elif not service_entries(cat, sid):
            out.append(_i("warn", "empty_service", f"Dienst „{cat.services[sid]['name']}“ ist leer"))
    # Portweiterleitung ohne passende Forward-Regel
    for n in spec["nat"]:
        if n["type"] != "portforward" or not n["enabled"]:
            continue
        ok = any(r["action"] == "accept" and r["dst_zone"] != ROUTER and (not r["dst"] or n["host"] in r["dst"])
                 and _zone_covers(r["src_zone"], n.get("in_zone")) for r in rules)
        if not ok and spec["options"]["default_drop"]:
            out.append(_i("warn", "portforward_no_rule", f"Portweiterleitung {n['ext_port']} → Host hat keine erlaubende Forward-Regel "
                                                         "(wird vom Default-Drop verworfen)", n["id"]))
    # Router-Dienste (DNS/DHCP) bei Default-Drop
    if spec["options"]["default_drop"] and not any(r["dst_zone"] == ROUTER and r["action"] == "accept" for r in rules):
        out.append(_i("warn", "no_router_access", "Default-Drop aktiv, aber keine Regel erlaubt Zugriffe auf den Router – "
                                                  "DHCP/DNS aus internen Netzen funktionieren dann nicht (Baustein „Standard-Härtung“)"))
    if not spec["options"]["baseline"] and spec["options"]["default_drop"]:
        out.append(_i("error", "drop_without_baseline", "Default-Drop ohne Grundregeln: auch bestehende Verbindungen würden verworfen"))
    if not spec["options"]["default_drop"] and not spec["raw"]["filter"]:
        out.append(_i("info", "no_default_drop", "Default-Drop ist abgeschaltet – nicht ausdrücklich verbotener Verkehr ist erlaubt"))

    # gerätebezogen
    zones_used = {z for r in spec["rules"] for z in (r["src_zone"], r["dst_zone"]) if z and z != ROUTER}
    zones_used |= {z for n in spec["nat"] for z in (n.get("zone"), n.get("in_zone")) if z}
    mgmt_zones = {zid for zid, z in cat.zones.items() if z.get("management")}
    # Gäste isoliert? = Regel verwirft Verkehr aus einer Zone mit Kürzel "guest" in eine andere Zone
    guest_isolated = any(r["action"] in ("drop", "reject") and r["src_zone"] and (cat.zones.get(r["src_zone"]) or {}).get("slug") == "guest"
                         and r["dst_zone"] and r["dst_zone"] != ROUTER for r in rules)
    for d in devices or []:
        for zid in sorted(zones_used):
            z = cat.zones.get(zid)
            if z is None:
                continue
            if z.get("source") == "wan":
                if not d.has_wan:
                    out.append(_i("error", "zone_no_wan", f"{d.name}: Zone „{z['name']}“ nutzt die WAN-Konfiguration, das Gerät hat keine"))
            elif zid not in d.zone_ids:
                out.append(_i("warn", "zone_empty", f"{d.name}: Zone „{zname[zid]}“ hat kein Interface – Regeln damit greifen nicht"))
        if spec["options"]["baseline"] and mgmt_zones and not (mgmt_zones & d.zone_ids):
            out.append(_i("error", "mgmt_zone_missing", f"{d.name}: Lokaler Zugriff (WinBox/SSH im LAN) nach dem Deploy nicht mehr möglich – "
                                                         "nur noch über den Tunnel"))
        if spec["options"]["default_drop"] and d.later_policies:
            out.append(_i("warn", "drop_not_last", f"{d.name}: Nach dieser Policy folgen {d.later_policies} weitere – deren Regeln stehen hinter "
                                                    "dem Default-Drop und greifen nicht"))
        if d.has_hotspot and not guest_isolated:
            out.append(_i("info", "hotspot_isolation", f"{d.name}: Gerät betreibt ein Gäste-Portal – Baustein „Gäste vom LAN isolieren“ "
                                                        "für die Gäste-Zone empfohlen"))
    return out
