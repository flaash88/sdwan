"""Zentrale Firewall-Policies (Phase 5): Validierung, Rendering, Push mit Rollback."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app.audit import audit
from app.db import system_session, utcnow
from app.models import Device, FirewallPolicy, PairingStatus, PolicyAssignment, PolicyDeployment
from app.routeros import RouterOSError, connect_device
from app.routeros.client import DeviceAPI

log = logging.getLogger(__name__)

PATHS = {"address_lists": "/ip/firewall/address-list", "filter": "/ip/firewall/filter", "nat": "/ip/firewall/nat"}
ORDERED = {"/ip/firewall/filter", "/ip/firewall/nat"}
READ_ONLY = {".id", "bytes", "packets", "dynamic", "invalid", "creation-time", ".nextid", ".about"}

CHAINS = {"filter": {"input", "forward", "output"}, "nat": {"srcnat", "dstnat"}}
ACTIONS = {
    "filter": {"accept", "drop", "reject", "fasttrack-connection", "log", "passthrough", "return", "add-src-to-address-list", "add-dst-to-address-list", "tarpit", "jump"},
    "nat": {"accept", "masquerade", "src-nat", "dst-nat", "redirect", "netmap", "same", "return", "passthrough"},
}
RULE_KEYS = {
    "chain", "action", "protocol", "src-address", "dst-address", "src-port", "dst-port", "port",
    "src-address-list", "dst-address-list", "in-interface", "out-interface", "in-interface-list", "out-interface-list",
    "connection-state", "connection-nat-state", "icmp-options", "tcp-flags", "src-address-type", "dst-address-type",
    "to-addresses", "to-ports", "address-list", "address-list-timeout", "reject-with", "log", "log-prefix",
    "limit", "connection-limit", "layer7-protocol", "content", "disabled", "comment", "hw-offload",
    "jump-target", "ipsec-policy", "connection-mark", "packet-mark", "routing-mark", "new-connection-mark",
    "src-mac-address", "in-bridge-port", "out-bridge-port", "ttl", "packet-size", "dscp", "psd", "nth",
}
_SAFE_VALUE = re.compile(r"^[\w .:/,!\-+*=@]{0,200}$")
_NAME = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")


class PolicyError(ValueError):
    pass


def _check_value(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    v = str(value)
    if not _SAFE_VALUE.match(v):
        raise PolicyError(f"Ungültiger Wert für {key}: {v!r}")
    return v


def validate_content(content: dict[str, Any]) -> dict[str, Any]:
    """Prüft und normalisiert eine Policy. Nur bekannte Felder, sichere Werte, gültige Adressen."""
    out: dict[str, list[dict[str, Any]]] = {"address_lists": [], "filter": [], "nat": []}
    unknown = set(content) - set(out)
    if unknown:
        raise PolicyError(f"Unbekannte Abschnitte: {sorted(unknown)}")
    for e in content.get("address_lists") or []:
        lst, addr = str(e.get("list", "")), str(e.get("address", ""))
        if not _NAME.match(lst):
            raise PolicyError(f"Ungültiger Listenname {lst!r}")
        try:
            addr = str(ipaddress.ip_network(addr, strict=False)) if "/" in addr else str(ipaddress.ip_address(addr))
        except ValueError:
            if not re.match(r"^[a-z0-9.\-]{1,253}$", addr):  # RouterOS erlaubt auch DNS-Namen
                raise PolicyError(f"Ungültige Adresse {addr!r}") from None
        item = {"list": lst, "address": addr}
        if e.get("comment"):
            item["comment"] = _check_value("comment", e["comment"])
        out["address_lists"].append(item)
    for kind in ("filter", "nat"):
        for i, r in enumerate(content.get(kind) or []):
            bad = set(r) - RULE_KEYS
            if bad:
                raise PolicyError(f"{kind}[{i}]: unbekannte Felder {sorted(bad)}")
            if r.get("chain") not in CHAINS[kind]:
                raise PolicyError(f"{kind}[{i}]: chain muss {sorted(CHAINS[kind])} sein")
            if r.get("action") not in ACTIONS[kind]:
                raise PolicyError(f"{kind}[{i}]: action muss {sorted(ACTIONS[kind])} sein")
            if r.get("action") in ("dst-nat", "src-nat", "netmap") and not r.get("to-addresses"):
                raise PolicyError(f"{kind}[{i}]: {r['action']} braucht to-addresses")
            out[kind].append({k: _check_value(k, v) for k, v in r.items() if v not in (None, "")})
    return out


def _tag(policy_id: uuid.UUID) -> str:
    return policy_id.hex[:8]


def render(policies: list[tuple[FirewallPolicy, dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """Sollzustand aller zugewiesenen Policies eines Geräts (in Zuweisungsreihenfolge)."""
    cfg: dict[str, list[dict[str, Any]]] = {p: [] for p in PATHS.values()}
    seen_addr: set[tuple[str, str]] = set()
    for pol, content in policies:
        t = _tag(pol.id)
        for i, e in enumerate(content.get("address_lists") or []):
            if (e["list"], e["address"]) in seen_addr:
                continue
            seen_addr.add((e["list"], e["address"]))
            cfg[PATHS["address_lists"]].append({**e, "comment": _comment(t, "a", i, e.get("comment"))})
        for kind, short in (("filter", "f"), ("nat", "n")):
            for i, r in enumerate(content.get(kind) or []):
                cfg[PATHS[kind]].append({**r, "comment": _comment(t, short, i, r.get("comment"))})
    return cfg


def _comment(tag: str, kind: str, idx: int, user: str | None) -> str:
    c = f"sdwan:fw:{tag}:{kind}{idx}"
    return f"{c} {user}" if user else c


async def snapshot(api: DeviceAPI) -> dict[str, list[dict[str, Any]]]:
    snap: dict[str, list[dict[str, Any]]] = {}
    for path in PATHS.values():
        rows = await api.print(path)
        snap[path] = [
            {k: v for k, v in r.items() if k not in READ_ONLY}
            for r in rows
            if str(r.get("comment", "")).startswith("sdwan:fw:")
        ]
    return snap


async def push(api: DeviceAPI, cfg: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    stats = {}
    # Address-List-Einträge, die manuell schon existieren, nicht doppelt anlegen (RouterOS lehnt Duplikate ab)
    manual = {
        (str(r.get("list")), str(r.get("address")))
        for r in await api.print(PATHS["address_lists"])
        if not str(r.get("comment", "")).startswith("sdwan:") and str(r.get("dynamic", "false")).lower() not in ("true", "yes")
    }
    wanted_al = [e for e in cfg[PATHS["address_lists"]] if (e["list"], e["address"]) not in manual]
    # Address-Lists zuerst anlegen (Regeln referenzieren sie)
    stats[PATHS["address_lists"]] = await api.sync_managed(PATHS["address_lists"], "fw:", wanted_al)
    for path in (PATHS["filter"], PATHS["nat"]):
        stats[path] = await api.sync_managed(path, "fw:", cfg[path], ordered=True, place_first=True)
    return stats


async def restore(api: DeviceAPI, snap: dict[str, list[dict[str, Any]]]) -> None:
    for path in (PATHS["filter"], PATHS["nat"], PATHS["address_lists"]):
        await api.sync_managed(path, "fw:", snap.get(path, []), ordered=path in ORDERED, place_first=path in ORDERED)


async def device_policies(db: AsyncSession, device_id: uuid.UUID) -> list[tuple[PolicyAssignment, FirewallPolicy]]:
    rows = await db.execute(
        select(PolicyAssignment, FirewallPolicy)
        .join(FirewallPolicy, PolicyAssignment.policy_id == FirewallPolicy.id)
        .where(PolicyAssignment.device_id == device_id)
        .order_by(PolicyAssignment.position, FirewallPolicy.name)
    )
    return [(a, p) for a, p in rows.all()]


async def run_deployment(deployment_id: uuid.UUID, device_ids: list[uuid.UUID]) -> None:
    """Pusht die Policies auf alle Geräte parallel; Fehler -> Rollback (pro Gerät bzw. atomar)."""
    async with system_session() as db:
        dep = await db.get(PolicyDeployment, deployment_id)
        assert dep is not None
        dep.status = "running"
        await db.commit()
        devices = [d for d in (await db.execute(select(Device).where(Device.id.in_(device_ids)))).scalars()]
        results: dict[str, Any] = {}
        snaps: dict[uuid.UUID, dict[str, Any]] = {}
        sem = asyncio.Semaphore(10)
        # DB-Zugriffe vorab (AsyncSession ist nicht für parallele Nutzung gedacht)
        assigned = {d.id: await device_policies(db, d.id) for d in devices}

        async def one(dev: Device) -> None:
            res: dict[str, Any] = {"name": dev.name, "ok": False}
            results[str(dev.id)] = res
            if dev.pairing_status != PairingStatus.paired:
                res["error"] = "Gerät nicht gepairt"
                return
            pols = assigned[dev.id]
            cfg = render([(p, p.content) for _a, p in pols])
            async with sem:
                try:
                    async with connect_device(dev) as api:
                        snaps[dev.id] = await snapshot(api)
                        try:
                            res["stats"] = await push(api, cfg)
                            res["ok"] = True
                        except RouterOSError as exc:
                            res["error"] = str(exc)
                            try:
                                await restore(api, snaps[dev.id])
                                res["rolled_back"] = True
                            except RouterOSError as exc2:
                                res["rollback_error"] = str(exc2)
                except RouterOSError as exc:
                    res["error"] = str(exc)
            for a, p in pols:
                if res["ok"]:
                    a.status, a.deployed_version, a.deployed_at, a.last_error = "deployed", p.version, utcnow(), None
                else:
                    a.status, a.last_error = ("rolled_back" if res.get("rolled_back") else "failed"), res.get("error")

        await asyncio.gather(*(one(d) for d in devices))
        ok = [d for d in devices if results[str(d.id)]["ok"]]
        failed = [d for d in devices if not results[str(d.id)]["ok"]]

        if dep.atomic and failed and ok:
            # Atomar: auch erfolgreiche Geräte auf den Stand vor dem Push zurücksetzen
            for dev in ok:
                try:
                    async with connect_device(dev) as api:
                        await restore(api, snaps[dev.id])
                    results[str(dev.id)].update({"ok": False, "rolled_back": True, "error": "atomarer Rollback"})
                except RouterOSError as exc:
                    results[str(dev.id)]["rollback_error"] = str(exc)
                for a, _p in assigned[dev.id]:
                    a.status, a.last_error = "rolled_back", "atomarer Rollback wegen Fehlern auf anderen Geräten"
            dep.status = "rolled_back"
        elif not failed:
            dep.status = "success"
        elif ok:
            dep.status = "partial"
        else:
            dep.status = "rolled_back" if all(results[str(d.id)].get("rolled_back") for d in failed) else "failed"
        dep.results = results
        dep.finished_at = utcnow()
        await audit(db, "policy.deploy.finished", tenant_id=dep.tenant_id, target_type="deployment", target_id=dep.id,
                    success=dep.status == "success", details={"status": dep.status, "devices": len(devices), "failed": len(failed)})
        await db.commit()
        await events.publish(dep.tenant_id, "policy.deployment", {"id": str(dep.id), "status": dep.status})


# ----------------------------------------------------------------------------- Bestehende Router-Regeln
def _is_dynamic(r: dict[str, Any]) -> bool:
    return str(r.get("dynamic", "false")).lower() in ("true", "yes")


async def read_router_firewall(api: DeviceAPI) -> dict[str, list[dict[str, Any]]]:
    """Aktuelle Filter-/NAT-Regeln und Address-Lists des Routers (ohne dynamische Einträge)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for key, path in PATHS.items():
        rows = []
        for r in await api.print(path):
            if _is_dynamic(r):
                continue
            comment = str(r.get("comment", "") or "")
            rows.append({
                **{k: v for k, v in r.items() if k not in READ_ONLY or k == ".id"},
                "managed": comment.startswith("sdwan:"),
                "managed_by": comment.split(":")[1] if comment.startswith("sdwan:") and comment.count(":") >= 1 else None,
            })
        out[key] = rows[:2000]
    return out


def import_rules(fw: dict[str, list[dict[str, Any]]], sections: list[str]) -> tuple[dict[str, Any], list[str], dict[str, list[str]]]:
    """Wandelt manuelle Router-Regeln in einen Policy-Inhalt um.

    Rückgabe: (content, Warnungen, übernommene .ids je Abschnitt). Nicht übertragbare Regeln
    (unbekannte Felder/Chains/Actions) werden übersprungen und als Warnung gemeldet.
    """
    content: dict[str, list[dict[str, Any]]] = {"address_lists": [], "filter": [], "nat": []}
    warnings: list[str] = []
    ids: dict[str, list[str]] = {"address_lists": [], "filter": [], "nat": []}
    for key in ("address_lists", "filter", "nat"):
        if key not in sections:
            continue
        for idx, r in enumerate(fw.get(key, [])):
            if r.get("managed"):
                continue
            if key == "address_lists":
                if r.get("timeout"):
                    continue  # temporäre Einträge nicht übernehmen
                item = {"list": r.get("list"), "address": r.get("address")}
                if r.get("comment"):
                    item["comment"] = r["comment"]
                cand = {"address_lists": [item]}
            else:
                rule = {k: v for k, v in r.items() if k in RULE_KEYS and v not in (None, "")}
                dropped = sorted(k for k in r if k not in RULE_KEYS and k not in READ_ONLY and k not in (".id", "managed", "managed_by"))
                if dropped:
                    warnings.append(f"{key} #{idx}: Felder ignoriert: {', '.join(dropped)}")
                cand = {key: [rule]}
            try:
                norm = validate_content(cand)
            except PolicyError as exc:
                if key != "address_lists" and "comment" in str(exc):
                    # Kommentar mit Sonderzeichen -> ohne Kommentar übernehmen
                    cand[key][0].pop("comment", None)
                    try:
                        norm = validate_content(cand)
                    except PolicyError as exc2:
                        warnings.append(f"{key} #{idx} übersprungen: {exc2}")
                        continue
                else:
                    warnings.append(f"{key} #{idx} übersprungen: {exc}")
                    continue
            content[key] += norm[key]
            ids[key].append(str(r[".id"]))
    return content, warnings, ids
