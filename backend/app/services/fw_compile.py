"""Firewall-Editor (Phase 14): einfache Policy (``spec``) → bestehendes Policy-Format ``{address_lists, filter, nat}``.

Der Compiler ist die einzige neue Ebene: Push, Versionierung, Rollback und ``policy.render()`` bleiben unverändert.
Das Ergebnis läuft zusätzlich durch ``policy.validate_content``.

Spec-Format::

    {"options": {"baseline": true, "default_drop": true},
     "rules": [{"id", "enabled", "src_zone", "src": [obj], "dst_zone" | "router", "dst": [obj],
                "services": [svc], "action": accept|drop|reject, "log", "comment"}],
     "nat": [{"id", "type": masquerade|portforward|redirect, ...}],
     "raw": {"address_lists": [], "filter": [], "nat": []}}   # aus dem Expertenmodus übernommen, unverändert

Namen auf dem Router: Objekte ``sdwan-obj-<slug>``, Threat-Feeds ``sdwan-feed-<slug>``, Zonen
``sdwan-zone-<slug>`` (Zone mit source=wan → bestehende Liste ``sdwan-wan``). Jede Regel trägt im Kommentar
``r:<id>`` (Zuordnung der Trefferzähler), Grundregeln ``base:<name>``.
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings
from app.routeros.naming import routeros_safe_name

ACTIONS = ("accept", "drop", "reject")
PROTOCOLS = ("tcp", "udp", "icmp", "gre", "esp", "ah", "")
_PORTS = re.compile(r"^\d{1,5}(-\d{1,5})?(,\d{1,5}(-\d{1,5})?)*$")
_SLUG = re.compile(r"[^a-z0-9]+")
_COMMENT = re.compile(r"^[\w .:/,!\-+*=@]{0,120}$")  # wie policy._SAFE_VALUE
# Verwaltungsdienste des Routers (RouterOS-Standardports: ftp, ssh, telnet, www, www-ssl, api, api-ssl, winbox)
LOCAL_ACCESS_LIST = "sdwan-local-access"
LOCAL_ACCESS_PORTS = "22,8291"  # SSH, WinBox
MGMT_PORTS = "21,22,23,80,443,8728,8729,8291"
ROUTER = "router"  # Zielzone „Router selbst“ -> chain=input


class SpecError(ValueError):
    pass


def slugify(name: str) -> str:
    s = _SLUG.sub("-", name.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")).strip("-")
    return s[:40] or "x"


def new_id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class Catalog:
    """Sichtbare Objekte/Dienste/Zonen als Dicts (id -> {…}), unabhängig von der DB testbar."""

    objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    services: dict[str, dict[str, Any]] = field(default_factory=dict)
    zones: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_rows(cls, objects: list[Any], services: list[Any], zones: list[Any]) -> Catalog:
        def d(r: Any, keys: tuple[str, ...]) -> dict[str, Any]:
            return {"id": str(r.id), "tenant_id": str(r.tenant_id) if r.tenant_id else None, **{k: getattr(r, k) for k in keys}}

        return cls(
            objects={str(o.id): d(o, ("name", "slug", "kind", "values", "members")) for o in objects},
            services={str(s.id): d(s, ("name", "slug", "entries", "members")) for s in services},
            zones={str(z.id): d(z, ("name", "slug", "source", "management")) for z in zones},
        )


# ----------------------------------------------------------------------------- Namen
def zone_list(zone: dict[str, Any]) -> str:
    return "sdwan-wan" if zone.get("source") == "wan" else routeros_safe_name(f"sdwan-zone-{zone['slug']}")


def object_list(obj: dict[str, Any]) -> str:
    return routeros_safe_name(f"sdwan-feed-{obj['slug']}" if obj["kind"] == "feed" else f"sdwan-obj-{obj['slug']}")


# ----------------------------------------------------------------------------- Validierung der Bausteine
def validate_object(kind: str, values: list[str]) -> list[str]:
    out = []
    for v in values:
        v = str(v).strip()
        try:
            if kind == "host":
                out.append(str(ipaddress.ip_address(v)))
            elif kind == "network":
                out.append(str(ipaddress.ip_network(v, strict=False)))
            elif kind == "range":
                a, b = (ipaddress.ip_address(x.strip()) for x in v.split("-", 1))
                if a.version != b.version or a > b:
                    raise ValueError
                out.append(f"{a}-{b}")  # ANNAHME (Labor): RouterOS-Address-Lists akzeptieren a.b.c.d-e.f.g.h
            else:
                raise SpecError(f"Objekttyp {kind} hat keine Adressen")
        except ValueError as exc:
            raise SpecError(f"Ungültige Adresse {v!r} für Typ {kind}") from exc
    return out


def validate_entries(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    out = []
    for e in entries:
        proto, ports = str(e.get("protocol", "")).lower(), str(e.get("ports", "") or "").replace(" ", "")
        if proto not in PROTOCOLS:
            raise SpecError(f"Protokoll {proto!r} nicht unterstützt ({', '.join(p for p in PROTOCOLS if p)})")
        if ports and (proto not in ("tcp", "udp") or not _PORTS.match(ports)):
            raise SpecError(f"Ungültige Ports {ports!r} (nur für tcp/udp, z. B. 80,443 oder 5000-5100)")
        out.append({"protocol": proto, "ports": ports})
    return out


# ----------------------------------------------------------------------------- Auflösen
def addresses(cat: Catalog, obj_id: str, seen: set[str] | None = None) -> list[str]:
    obj = cat.objects.get(obj_id)
    if obj is None:
        raise SpecError(f"Objekt {obj_id} nicht gefunden")
    seen = seen or set()
    if obj_id in seen:
        raise SpecError(f"Objektgruppe {obj['name']} enthält sich selbst")
    if obj["kind"] == "group":
        out: list[str] = []
        for m in obj.get("members") or []:
            if cat.objects.get(m, {}).get("kind") == "feed":
                raise SpecError(f"Gruppe {obj['name']}: Threat-Feeds können nicht in Gruppen stehen")
            out += [a for a in addresses(cat, m, seen | {obj_id}) if a not in out]
        return out
    return list(obj.get("values") or [])


def service_entries(cat: Catalog, svc_id: str, seen: set[str] | None = None) -> list[dict[str, str]]:
    svc = cat.services.get(svc_id)
    if svc is None:
        raise SpecError(f"Dienst {svc_id} nicht gefunden")
    seen = seen or set()
    if svc_id in seen:
        raise SpecError(f"Dienstgruppe {svc['name']} enthält sich selbst")
    out = list(svc.get("entries") or [])
    for m in svc.get("members") or []:
        out += service_entries(cat, m, seen | {svc_id})
    return out


def _by_protocol(entries: list[dict[str, str]]) -> dict[str, str]:
    """Einträge je Protokoll zusammenfassen; ein Eintrag ohne Ports = alle Ports dieses Protokolls."""
    ports: dict[str, list[str] | None] = {}
    for e in entries:
        p = e["protocol"]
        if not e["ports"] or ports.get(p, []) is None:
            ports[p] = None
        else:
            ports.setdefault(p, [])
            ports[p] = [*ports[p], *[x for x in e["ports"].split(",") if x not in ports[p]]]  # type: ignore[index]
    if "" in ports:  # „alle Protokolle“ schluckt den Rest
        return {"": ""}
    return {p: ",".join(v) if v else "" for p, v in ports.items()}


def normalize_spec(spec: dict[str, Any]) -> dict[str, Any]:
    opts = spec.get("options") or {}
    rules = []
    for r in spec.get("rules") or []:
        action = r.get("action", "accept")
        if action not in ACTIONS:
            raise SpecError(f"Aktion {action!r} (erlauben=accept, verwerfen=drop, ablehnen=reject)")
        comment = str(r.get("comment") or "").strip()
        if comment and not _COMMENT.match(comment):
            raise SpecError(f"Kommentar enthält unzulässige Zeichen: {comment!r}")
        rid = str(r.get("id") or new_id())
        if not re.match(r"^[a-z0-9]{4,12}$", rid):
            raise SpecError(f"Ungültige Regel-ID {rid!r}")
        rules.append({
            "id": rid, "enabled": bool(r.get("enabled", True)), "src_zone": r.get("src_zone") or None,
            "src": [str(x) for x in r.get("src") or []], "dst_zone": r.get("dst_zone") or None,
            "dst": [str(x) for x in r.get("dst") or []], "services": [str(x) for x in r.get("services") or []],
            "action": action, "log": bool(r.get("log", False)), "comment": comment,
        })
    if len({r["id"] for r in rules}) != len(rules):
        raise SpecError("Regel-IDs doppelt")
    nat = []
    for n in spec.get("nat") or []:
        t = n.get("type")
        item = {"id": str(n.get("id") or new_id()), "type": t, "enabled": bool(n.get("enabled", True)),
                "comment": str(n.get("comment") or "").strip()}
        if t == "masquerade":
            item["zone"] = n.get("zone")
        elif t == "portforward":
            proto = str(n.get("protocol", "tcp"))
            if proto not in ("tcp", "udp"):
                raise SpecError("Portweiterleitung: Protokoll tcp oder udp")
            for k in ("ext_port", "int_port"):
                if n.get(k) and not _PORTS.match(str(n[k])):
                    raise SpecError(f"Portweiterleitung: ungültiger Port {n[k]!r}")
            if not n.get("ext_port") or not n.get("host"):
                raise SpecError("Portweiterleitung braucht externen Port und internen Host")
            item.update(in_zone=n.get("in_zone"), protocol=proto, ext_port=str(n["ext_port"]), host=str(n["host"]),
                        int_port=str(n.get("int_port") or ""))
        elif t == "redirect":
            proto = str(n.get("protocol", "udp"))
            if proto not in ("tcp", "udp") or not _PORTS.match(str(n.get("port", ""))):
                raise SpecError("Umleitung: Protokoll tcp/udp und Port nötig")
            item.update(in_zone=n.get("in_zone"), protocol=proto, port=str(n["port"]))
        else:
            raise SpecError(f"NAT-Typ {t!r} unbekannt (masquerade, portforward, redirect)")
        nat.append(item)
    raw = spec.get("raw") or {}
    return {
        "options": {"baseline": bool(opts.get("baseline", True)), "default_drop": bool(opts.get("default_drop", True))},
        "rules": rules, "nat": nat,
        "raw": {k: list(raw.get(k) or []) for k in ("address_lists", "filter", "nat")},
    }


def referenced(spec: dict[str, Any]) -> dict[str, set[str]]:
    """IDs aller referenzierten Objekte/Dienste/Zonen (für Neukompilierung bei Änderungen)."""
    objs, svcs, zones = set(), set(), set()
    for r in spec.get("rules") or []:
        objs |= set(r.get("src") or []) | set(r.get("dst") or [])
        svcs |= set(r.get("services") or [])
        zones |= {z for z in (r.get("src_zone"), r.get("dst_zone")) if z and z != ROUTER}
    for n in spec.get("nat") or []:
        zones |= {z for z in (n.get("zone"), n.get("in_zone")) if z}
        if n.get("host"):
            objs.add(n["host"])
    return {"objects": objs, "services": svcs, "zones": zones}


# ----------------------------------------------------------------------------- Kompilieren
def baseline_input() -> list[dict[str, str]]:
    """Plattform-Zugänge: immer vor jedem Drop, nicht abschaltbar (Hub/Tunnel, Mesh, VRRP, Ping)."""
    s = get_settings()
    return [
        {"chain": "input", "action": "accept", "in-interface": s.wg_device_interface, "src-address": s.wg_hub_ip, "comment": "base:platform-hub"},
        {"chain": "input", "action": "accept", "protocol": "udp", "dst-port": str(s.mesh_listen_port), "comment": "base:platform-mesh"},
        # ANNAHME (Labor): RouterOS akzeptiert protocol=vrrp (IP-Protokoll 112)
        {"chain": "input", "action": "accept", "protocol": "vrrp", "comment": "base:platform-vrrp"},
        {"chain": "input", "action": "accept", "protocol": "icmp", "comment": "base:platform-icmp"},
    ]


def compile_spec(spec: dict[str, Any], cat: Catalog) -> dict[str, list[dict[str, Any]]]:
    spec = normalize_spec(spec)
    opts = spec["options"]
    lists: dict[str, list[str]] = {}  # Listenname -> Adressen
    filt: list[dict[str, Any]] = []
    nat: list[dict[str, Any]] = []

    def zone(zid: str | None) -> str | None:
        if not zid:
            return None
        z = cat.zones.get(zid)
        if z is None:
            raise SpecError(f"Zone {zid} nicht gefunden")
        return zone_list(z)

    def obj_ref(ids: list[str], rid: str, side: str) -> str | None:
        if not ids:
            return None
        objs = []
        for i in ids:
            o = cat.objects.get(i)
            if o is None:
                raise SpecError(f"Regel {rid}: Objekt {i} nicht gefunden")
            objs.append(o)
        if len(objs) == 1:
            o = objs[0]
            name = object_list(o)
            if o["kind"] != "feed":
                lists.setdefault(name, addresses(cat, o["id"]))
            return name
        if any(o["kind"] == "feed" for o in objs):
            raise SpecError(f"Regel {rid}: Threat-Feed-Objekte nur einzeln verwenden")
        name = routeros_safe_name(f"sdwan-r-{rid}-{side}")
        merged: list[str] = []
        for o in objs:
            merged += [a for a in addresses(cat, o["id"]) if a not in merged]
        lists[name] = merged
        return name

    if opts["baseline"]:
        filt += baseline_input()
        filt += [
            {"chain": "input", "action": "accept", "connection-state": "established,related,untracked", "comment": "base:input-established"},
            {"chain": "input", "action": "drop", "connection-state": "invalid", "comment": "base:input-invalid"},
            {"chain": "forward", "action": "accept", "connection-state": "established,related,untracked", "comment": "base:forward-established"},
            {"chain": "forward", "action": "drop", "connection-state": "invalid", "comment": "base:forward-invalid"},
        ]
        # Phase 24: Vor-Ort-Zugang – WinBox/SSH (und DHCP für den Service-Port) aus der Liste sdwan-local-access.
        # Die Liste enthält nur Interfaces der Zonen Management/LAN (nie WAN) und ist leer, solange der Zugang auf dem
        # Gerät nicht aktiv ist – dann wirkungslos, d. h. keine Verhaltensänderung für bestehende Geräte.
        filt += [
            {"chain": "input", "action": "accept", "protocol": "tcp", "dst-port": LOCAL_ACCESS_PORTS, "in-interface-list": LOCAL_ACCESS_LIST,
             "comment": "base:local-access"},
            {"chain": "input", "action": "accept", "protocol": "udp", "dst-port": "67", "in-interface-list": LOCAL_ACCESS_LIST,
             "comment": "base:local-access-dhcp"},
        ]
        for z in sorted(cat.zones.values(), key=lambda z: z["slug"]):
            if z.get("management"):
                filt.append({"chain": "input", "action": "accept", "protocol": "tcp", "dst-port": MGMT_PORTS,
                             "in-interface-list": zone_list(z), "comment": f"base:mgmt-{z['slug']}"})
        filt.append({"chain": "input", "action": "drop", "protocol": "tcp", "dst-port": MGMT_PORTS, "comment": "base:mgmt-only"})

    for r in spec["rules"]:
        chain = "input" if r["dst_zone"] == ROUTER else "forward"
        base: dict[str, Any] = {"chain": chain, "action": r["action"], "comment": f"r:{r['id']} {r['comment']}".strip()}
        if (z := zone(r["src_zone"])) is not None:
            base["in-interface-list"] = z
        if chain == "forward" and (z := zone(r["dst_zone"])) is not None:
            base["out-interface-list"] = z
        if (lst := obj_ref(r["src"], r["id"], "src")) is not None:
            base["src-address-list"] = lst
        if chain == "forward" and (lst := obj_ref(r["dst"], r["id"], "dst")) is not None:
            base["dst-address-list"] = lst
        if r["log"]:
            base.update({"log": "yes", "log-prefix": f"sdwan-{r['id']}"})
        if not r["enabled"]:
            base["disabled"] = "yes"
        entries: list[dict[str, str]] = []
        for sid in r["services"]:
            entries += service_entries(cat, sid)
        groups = _by_protocol(entries) if entries else {"": ""}
        for proto, ports in groups.items():
            rule = dict(base)
            if proto:
                rule["protocol"] = proto
            if ports:
                rule["dst-port"] = ports
            filt.append(rule)

    for n in spec["nat"]:
        c = f"r:{n['id']} {n['comment']}".strip()
        extra = {} if n["enabled"] else {"disabled": "yes"}
        if n["type"] == "masquerade":
            z = zone(n.get("zone"))
            if not z:
                raise SpecError("Masquerade braucht eine WAN-Zone")
            nat.append({"chain": "srcnat", "action": "masquerade", "out-interface-list": z, "comment": c, **extra})
        elif n["type"] == "portforward":
            host = cat.objects.get(n["host"])
            if host is None or host["kind"] != "host" or len(host.get("values") or []) != 1:
                raise SpecError("Portweiterleitung: Ziel muss ein Host-Objekt mit genau einer Adresse sein")
            rule = {"chain": "dstnat", "action": "dst-nat", "protocol": n["protocol"], "dst-port": n["ext_port"],
                    "to-addresses": host["values"][0], "comment": c, **extra}
            if (z := zone(n.get("in_zone"))) is not None:
                rule["in-interface-list"] = z
            if n["int_port"]:
                rule["to-ports"] = n["int_port"]
            nat.append(rule)
        elif n["type"] == "redirect":
            rule = {"chain": "dstnat", "action": "redirect", "protocol": n["protocol"], "dst-port": n["port"], "to-ports": n["port"], "comment": c, **extra}
            if (z := zone(n.get("in_zone"))) is not None:
                rule["in-interface-list"] = z
            nat.append(rule)

    filt += [dict(r) for r in spec["raw"]["filter"]]
    nat += [dict(r) for r in spec["raw"]["nat"]]
    if opts["default_drop"]:
        filt += [
            {"chain": "input", "action": "drop", "comment": "base:default-drop-input"},
            {"chain": "forward", "action": "drop", "comment": "base:default-drop-forward"},
        ]
    address_lists = [{"list": name, "address": a} for name, addrs in lists.items() for a in addrs]
    address_lists += [dict(a) for a in spec["raw"]["address_lists"]]
    return {"address_lists": address_lists, "filter": filt, "nat": nat}


def expert_to_simple(content: dict[str, Any]) -> dict[str, Any]:
    """Experte -> einfach: bestehende Regeln bleiben unverändert als Rohregeln erhalten (nicht alles abbildbar).
    Grundregeln und Default-Drop sind aus, damit sich das Verhalten nicht ändert."""
    return {"options": {"baseline": False, "default_drop": False}, "rules": [], "nat": [],
            "raw": {k: list(content.get(k) or []) for k in ("address_lists", "filter", "nat")}}


def rule_key_from_comment(comment: str) -> str | None:
    """``sdwan:fw:<tag>:f3 r:ab12cd Text`` -> ``<tag>:ab12cd``; Grundregeln -> ``<tag>:base:<name>``."""
    m = re.match(r"^sdwan:fw:([0-9a-f]{8}):[fna]\d+(?: (r:[a-z0-9]+|base:[a-z0-9\-]+))?", comment or "")
    if not m or not m.group(2):
        return None
    ref = m.group(2)
    return f"{m.group(1)}:{ref[2:]}" if ref.startswith("r:") else f"{m.group(1)}:{ref}"


def to_commands(cfg: dict[str, list[dict[str, Any]]]) -> list[str]:
    """Sollzustand aus ``policy.render()`` als RouterOS-Befehle (nur Anzeige/Vorschau)."""
    def q(v: Any) -> str:
        v = str(v)
        return f'"{v}"' if re.search(r"[\s;\"'=]", v) or v == "" else v

    lines: list[str] = []
    for path, rows in cfg.items():
        if not rows:
            continue
        lines.append("/" + path.strip("/").replace("/", " "))
        for r in rows:
            lines.append("add " + " ".join(f"{k}={q(v)}" for k, v in r.items()))
    return lines
