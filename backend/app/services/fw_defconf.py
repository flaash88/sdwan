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

from typing import Any

from app.routeros.client import DeviceAPI, _norm

DEFCONF_PREFIX = "defconf"
DROP_CHAINS = ("input", "forward")
# defconf-Interface-Lists → Zonen-Kürzel (Vorschlag)
LIST_TO_ZONE = {"WAN": "wan", "LAN": "lan"}


def is_defconf(rule: dict[str, Any]) -> bool:
    return str(rule.get("comment", "")).strip().lower().startswith(DEFCONF_PREFIX)


def _active(rule: dict[str, Any]) -> bool:
    return _norm(rule.get("dynamic", "false")) != "true" and _norm(rule.get("disabled", "false")) != "true"


def summary(r: dict[str, Any]) -> dict[str, Any]:
    return {"id": r.get(".id"), "chain": r.get("chain"), "action": r.get("action"), "comment": r.get("comment") or "",
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


async def enable(api: DeviceAPI, remembered: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Nur gemerkte Regeln wieder aktivieren – und nur, wenn ``.id`` und Kommentar noch passen."""
    rows = {r.get(".id"): r for r in await api.print("/ip/firewall/filter")}
    out: dict[str, list[str]] = {"enabled": [], "missing": [], "already_active": []}
    for rec in remembered:
        r = rows.get(rec["rule_id"])
        if r is None or str(r.get("comment", "")) != rec["comment"]:
            out["missing"].append(rec["rule_id"])  # gelöscht oder verändert – nicht anfassen
            continue
        if _norm(r.get("disabled", "false")) != "true":
            out["already_active"].append(rec["rule_id"])
            continue
        await api.set("/ip/firewall/filter", rec["rule_id"], disabled="no")
        out["enabled"].append(rec["rule_id"])
    return out


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
