"""Werks-Firewall (defconf) bei einfachen Policies (Nachtrag Phase 14).

Die MikroTik-Werkskonfiguration legt Filterregeln mit Kommentar ``defconf: …`` an. Die Grundregeln der Plattform
(established/related, invalid drop, Zugriff nur aus LAN/Management, Default-Drop) decken sie ab. Beim Deploy einer
einfachen Policy mit Default-Drop können sie daher **deaktiviert** werden (``disabled=yes``, nie gelöscht) und gelten
dann nicht als Hinderungsgrund. Die Plattform merkt sich je Gerät, welche Regeln *sie* deaktiviert hat
(``fw_defconf_disabled``), und aktiviert nur diese wieder – beim Entfernen der letzten Policy mit Default-Drop oder
per Button.

Zusätzlich liefern die defconf-Interface-Lists ``WAN``/``LAN`` einen Vorschlag für die Zonen-Zuordnung.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.routeros.client import DeviceAPI, _norm

DEFCONF_PREFIX = "defconf"
DROP_CHAINS = ("input", "forward")
# defconf-Interface-Lists → Zonen-Kürzel (Vorschlag)
LIST_TO_ZONE = {"WAN": "wan", "LAN": "lan"}


def is_defconf(rule: dict[str, Any]) -> bool:
    return str(rule.get("comment", "")).strip().lower().startswith(DEFCONF_PREFIX)


def _active(rule: dict[str, Any]) -> bool:
    return _norm(rule.get("dynamic", "false")) != "true" and _norm(rule.get("disabled", "false")) != "true"


# nicht Teil des Fingerabdrucks: Identität, Zähler, Zustand, Kommentar
_FP_IGNORE = {".id", ".nextid", ".dead", "packets", "bytes", "disabled", "dynamic", "invalid", "comment"}


def fingerprint(r: dict[str, Any]) -> str:
    """Fingerabdruck einer Filterregel: chain, action und alle Match-Felder, normalisiert (bool/yes/no, Reihenfolge)."""
    fields = {k: _norm(v) for k, v in r.items() if k not in _FP_IGNORE and _norm(v) != ""}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def summary(r: dict[str, Any]) -> dict[str, Any]:
    return {"id": r.get(".id"), "fingerprint": fingerprint(r), "chain": r.get("chain"), "action": r.get("action"), "comment": r.get("comment") or "",
            "summary": " ".join(f"{k}={v}" for k, v in r.items()
                                if k in ("protocol", "src-address", "dst-address", "dst-port", "in-interface", "in-interface-list",
                                         "connection-state", "connection-nat-state"))}


async def active_defconf(api: DeviceAPI) -> list[dict[str, Any]]:
    """Aktive defconf-Regeln der Chains input/forward (diese lägen hinter dem Default-Drop)."""
    return [summary(r) for r in await api.print("/ip/firewall/filter")
            if is_defconf(r) and _active(r) and r.get("chain") in DROP_CHAINS]


async def disable(api: DeviceAPI) -> list[dict[str, Any]]:
    """Aktive defconf-Regeln deaktivieren; liefert die tatsächlich deaktivierten (zum Merken)."""
    done = []
    for r in await active_defconf(api):
        await api.set("/ip/firewall/filter", r["id"], disabled="yes")
        done.append(r)
    return done


async def enable(api: DeviceAPI, remembered: list[dict[str, Any]]) -> dict[str, Any]:
    """Nur gemerkte Regeln wieder aktivieren.

    1. über ``.id`` – wenn Kommentar und (falls gespeichert) Fingerabdruck passen;
    2. sonst per Fingerabdruck unter den **deaktivierten** defconf-Regeln – nur bei genau einem Treffer
       (z. B. neue ``.id`` nach Import/Reset). Mehrere Treffer → ``ambiguous``, nichts tun; keiner → ``missing``.
    Listen enthalten die gespeicherte ``rule_id``; ``relocated`` ordnet alte → neue ``.id`` zu.
    """
    rows = await api.print("/ip/firewall/filter")
    by_id = {r.get(".id"): r for r in rows}
    out: dict[str, Any] = {"enabled": [], "missing": [], "already_active": [], "ambiguous": [], "relocated": {}}
    claimed: set[str] = set()
    for rec in remembered:
        fp = rec.get("fingerprint")
        r = by_id.get(rec["rule_id"])
        if r is not None and (str(r.get("comment", "")) != rec["comment"] or (fp and fingerprint(r) != fp)):
            r = None
        if r is None and fp:
            cands = [x for x in rows if is_defconf(x) and _norm(x.get("disabled", "false")) == "true" and x.get(".id") not in claimed
                     and fingerprint(x) == fp]
            if len(cands) > 1:
                out["ambiguous"].append(rec["rule_id"])
                continue
            if len(cands) == 1:
                r = cands[0]
                out["relocated"][rec["rule_id"]] = r.get(".id")
        if r is None:
            out["missing"].append(rec["rule_id"])  # gelöscht oder verändert – nicht anfassen
            continue
        claimed.add(str(r.get(".id")))
        if _norm(r.get("disabled", "false")) != "true":
            out["already_active"].append(rec["rule_id"])
            continue
        await api.set("/ip/firewall/filter", str(r.get(".id")), disabled="no")
        out["enabled"].append(rec["rule_id"])
    return out


def records(rows: list[Any]) -> list[dict[str, Any]]:
    """DB-Zeilen (``FwDefconfDisabled``) → Eingabe für ``enable``."""
    return [{"rule_id": x.rule_id, "comment": x.comment, "fingerprint": x.fingerprint} for x in rows]


async def remembered_rows(db: AsyncSession, device_id: uuid.UUID) -> list[Any]:
    from app.models import FwDefconfDisabled

    return list((await db.execute(select(FwDefconfDisabled).where(FwDefconfDisabled.device_id == device_id))).scalars())


async def apply_result(db: AsyncSession, device_id: uuid.UUID, res: dict[str, Any]) -> None:
    """Gemerkte Einträge nach ``enable`` fortschreiben: erledigte löschen, nicht eindeutige markieren."""
    done = set(res.get("enabled", [])) | set(res.get("missing", [])) | set(res.get("already_active", []))
    ambiguous = set(res.get("ambiguous", []))
    for x in await remembered_rows(db, device_id):
        if x.rule_id in done:
            await db.delete(x)
        elif x.rule_id in ambiguous:
            x.status = "ambiguous"


async def zone_suggestions(api: DeviceAPI) -> list[dict[str, Any]]:
    """defconf-Interface-Lists WAN/LAN mit ihren Mitgliedern als Vorschlag für die Zonen WAN/LAN."""
    lists = {str(x.get("name")): x for x in await api.print("/interface/list")}
    members = await api.print("/interface/list/member")
    out = []
    for name, slug in LIST_TO_ZONE.items():
        lst = lists.get(name)
        if lst is None:
            continue
        ifaces = sorted({str(m.get("interface")) for m in members if m.get("list") == name and m.get("interface")
                         and _norm(m.get("disabled", "false")) != "true"})
        out.append({"list": name, "zone_slug": slug, "interfaces": ifaces, "defconf": is_defconf(lst)})
    return out
