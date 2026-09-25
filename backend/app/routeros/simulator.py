"""In-Memory-Simulation eines RouterOS-7-Geräts.

Wird mit ``ROUTEROS_BACKEND=simulator`` aktiv. Ermöglicht einen vollständigen Demo-Betrieb
(Dashboard, Metriken, Mesh-Push, Backups, Updates) ohne echte Hardware und dient als
Test-Double in der Test-Suite. Der Zustand lebt pro Tunnel-IP im Prozess.
"""

from __future__ import annotations

import hashlib
import random
import time
from typing import Any

from app.routeros.client import RouterOSError

_STATE: dict[str, SimRouter] = {}

# Pfade, die wie Tabellen funktionieren (print/add/set/remove)
_TABLE_PATHS = {
    "/interface",
    "/interface/wireguard",
    "/interface/wireguard/peers",
    "/ip/address",
    "/ip/route",
    "/ip/firewall/filter",
    "/ip/firewall/nat",
    "/ip/firewall/mangle",
    "/ip/firewall/address-list",
    "/routing/table",
    "/ip/dns/static",
    "/tool/netwatch",
    "/system/script",
    "/system/scheduler",
    "/user",
    "/interface/list",
    "/interface/list/member",
    "/certificate",
    "/ip/dhcp-client",
    "/ip/service",
    "/interface/vrrp",
}


class SimRouter:
    def __init__(self, host: str) -> None:
        self.host = host
        seed = int(hashlib.sha256(host.encode()).hexdigest()[:8], 16)
        self.rng = random.Random(seed)
        self.boot = time.time() - self.rng.randint(3600, 3600 * 24 * 30)
        self.version = "7.15.3"
        self.channel = "stable"
        self.identity = f"sim-{host.replace('.', '-')}"
        self.board = self.rng.choice(["RB5009UG+S+", "hAP ax3", "CCR2004-16G-2S+", "CHR"])
        self.tables: dict[str, list[dict[str, Any]]] = {p: [] for p in _TABLE_PATHS}
        self._next_id = 1
        self.dns: dict[str, Any] = {"servers": "", "use-doh-server": "", "verify-doh-cert": "no", "allow-remote-requests": "yes"}
        self.fail_next: set[str] = set()  # Tests: Befehle, die einmal fehlschlagen sollen
        self.down_hosts: set[str] = set()  # Tests: Netwatch-Ziele, die als "down" gelten
        self.vrrp_master: set[str] = set()  # Namen der VRRP-Interfaces, die gerade Master sind
        self.counters: dict[str, list[int]] = {}
        for i, name in enumerate(["ether1", "ether2", "ether3", "ether4", "bridge", "sdwan-mgmt"]):
            iface_type = {"bridge": "bridge", "sdwan-mgmt": "wg"}.get(name, "ether")
            extra = {"default-name": name} if iface_type == "ether" else {}
            if name == "ether1":
                extra["comment"] = "Internet Glasfaser"
            self._insert("/interface", {"name": name, "type": iface_type, "running": "true", "disabled": "false", **extra})
            self.counters[name] = [self.rng.randint(10**6, 10**9), self.rng.randint(10**6, 10**9)]
        self._insert(
            "/interface/wireguard",
            {"name": "sdwan-mgmt", "listen-port": "13231", "public-key": _fake_key(host + "mgmt"), "private-key": "***"},
        )
        self._insert("/ip/address", {"address": "192.168.88.1/24", "interface": "bridge"})
        for svc, port in (("ssh", 22), ("winbox", 8291), ("www", 80), ("api", 8728)):
            self._insert("/ip/service", {"name": svc, "port": str(port), "disabled": "false", "address": ""})
        self._insert("/ip/route", {"dst-address": "0.0.0.0/0", "gateway": "100.64.0.1", "distance": "1"})
        # weitere Ports wie beim L009UiGS (ether5–ether8, z. B. 5G-Modem an ether8)
        for name in ("ether5", "ether6", "ether7", "ether8"):
            self._insert("/interface", {"name": name, "type": "ether", "running": "true", "disabled": "false", "default-name": name})
            self.counters[name] = [self.rng.randint(10**6, 10**9), self.rng.randint(10**6, 10**9)]

    # -- Hilfen --------------------------------------------------------------
    def _insert(self, path: str, attrs: dict[str, Any]) -> str:
        item_id = f"*{self._next_id:X}"
        self._next_id += 1
        row = {".id": item_id, **{k: _s(v) for k, v in attrs.items()}}
        if path == "/interface/wireguard" and "public-key" not in row:
            row["public-key"] = _fake_key(self.host + row.get("name", item_id))
        self.tables[path].append(row)
        if path in ("/interface/wireguard", "/interface/vrrp") and not any(r["name"] == row.get("name") for r in self.tables["/interface"]):
            itype = "wg" if path == "/interface/wireguard" else "vrrp"
            self.tables["/interface"].append({".id": f"*{self._next_id:X}", "name": row.get("name"), "type": itype, "running": "true", "disabled": "false"})
            self._next_id += 1
            self.counters[row.get("name", "")] = [0, 0]
        return item_id

    def _find(self, path: str, item_id: str) -> dict[str, Any]:
        for r in self.tables[path]:
            if r[".id"] == item_id:
                return r
        raise RouterOSError(f"no such item {item_id}")

    # -- VRRP ------------------------------------------------------------------
    def set_vrrp_master(self, name: str, master: bool) -> None:
        """Demo/Tests: VRRP-Zustandswechsel inkl. Ausführung von on-master/on-backup."""
        row = next((r for r in self.tables["/interface/vrrp"] if r.get("name") == name), None)
        if row is None:
            raise RouterOSError(f"no vrrp interface {name}")
        was = name in self.vrrp_master
        (self.vrrp_master.add if master else self.vrrp_master.discard)(name)
        if was != master:
            self.run_script(row.get("on-master" if master else "on-backup", ""))

    def run_script(self, src: str) -> None:
        """Mini-Interpreter für die von der Plattform erzeugten Route-Scripts (nicht allgemein)."""
        import re

        cond = re.search(r'netwatch get \[find where comment="(sdwan:wan:check:\d+)"\] status\] = "up"', src)
        if cond:
            nw = next((n for n in self.tables["/tool/netwatch"] if n.get("comment") == cond.group(1)), None)
            if nw is None or nw.get("host") in self.down_hosts:
                src = src[: cond.start()]  # Bedingung falsch -> nachfolgende Aktionen nicht ausführen
        for action, prefix in re.findall(r'/ip route (disable|enable) \[find where comment~"\^([^"]+)"\]', src):
            for r in self.tables["/ip/route"]:
                if str(r.get("comment", "")).startswith(prefix):
                    r["disabled"] = "yes" if action == "disable" else "no"

    # -- Kommandos -------------------------------------------------------------
    def call(self, cmd: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if getattr(self, "offline", False):
            raise RouterOSError("timeout (simuliert offline)")
        if cmd in self.fail_next:
            self.fail_next.discard(cmd)
            raise RouterOSError(f"{cmd}: simulated failure")
        path, _, action = cmd.rpartition("/")
        if path in self.tables:
            return self._table_op(path, action, params)
        handler = {
            "/system/resource/print": self._resource,
            "/system/identity/print": lambda p: [{"name": self.identity}],
            "/system/routerboard/print": lambda p: [{"serial-number": hashlib.md5(self.host.encode()).hexdigest()[:12].upper(), "model": self.board}],
            "/ping": self._ping,
            "/export": self._export,
            "/system/package/update/check-for-updates": self._check_updates,
            "/system/package/update/print": self._update_print,
            "/system/package/update/install": self._install,
            "/system/package/update/set": self._update_set,
            "/system/reboot": lambda p: [],
            "/ip/dns/print": lambda p: [dict(self.dns)],
            "/ip/dns/set": self._dns_set,
            "/ip/dns/cache/flush": lambda p: [],
            "/interface/monitor-traffic": self._monitor_traffic,
            "/tool/fetch": lambda p: [{"status": "finished"}],
            "/system/script/run": lambda p: [],
            "/certificate/settings/set": lambda p: [],
            "/certificate/import": lambda p: [{"certificates-imported": 140}],
        }.get(cmd)
        if handler is None:
            raise RouterOSError(f"unknown command {cmd}")
        return handler(params)

    def _table_op(self, path: str, action: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if action == "print":
            rows = [dict(r) for r in self.tables[path]]
            if path == "/interface":
                for r in rows:
                    c = self.counters.setdefault(r["name"], [0, 0])
                    c[0] += self.rng.randint(10**4, 10**7)
                    c[1] += self.rng.randint(10**4, 10**6)
                    r["rx-byte"], r["tx-byte"] = c
            if path == "/tool/netwatch":
                for r in rows:
                    down = r.get("host") in self.down_hosts
                    r["status"] = "down" if down else "up"
                    r["rtt-avg"] = f"{self.rng.uniform(8, 35):.1f}ms"
                    r["loss-percent"] = "100%" if down else "0%"
            if path == "/ip/route":
                down_slots = {
                    str(n.get("comment", "")).rsplit(":", 1)[-1]
                    for n in self.tables["/tool/netwatch"]
                    if n.get("host") in self.down_hosts
                }
                mains = [r for r in rows if str(r.get("comment", "")).count(":") == 3 and str(r.get("comment", "")).startswith("sdwan:wan:default:")]
                alive = [r for r in mains if str(r["comment"]).rsplit(":", 1)[1] not in down_slots and r.get("disabled") != "yes"]
                best = min((int(r.get("distance", 1)) for r in alive), default=None)
                for r in mains:
                    r["active"] = "true" if r in alive and int(r.get("distance", 1)) == best else "false"
            if path == "/interface/vrrp":
                for r in rows:
                    off = r.get("disabled") in ("yes", "true")
                    m = r.get("name") in self.vrrp_master and not off
                    r["master"], r["backup"], r["running"] = ("true" if m else "false"), ("false" if m or off else "true"), ("true" if m else "false")
            if path == "/interface/wireguard/peers":
                for r in rows:
                    r.setdefault("last-handshake", f"{self.rng.randint(1, 90)}s")
                    r.setdefault("rx", self.rng.randint(10**5, 10**8))
                    r.setdefault("tx", self.rng.randint(10**5, 10**8))
            return rows
        if action == "add":
            before = params.pop("place-before", None)
            if before is not None:
                item_id = self._insert(path, params)
                row = self.tables[path].pop()
                idx = next((i for i, r in enumerate(self.tables[path]) if r[".id"] == before), len(self.tables[path]))
                self.tables[path].insert(idx, row)
                return [{"ret": item_id}]
            if path == "/interface/wireguard" and any(r.get("name") == params.get("name") for r in self.tables[path]):
                raise RouterOSError("failure: already have interface with such name")
            return [{"ret": self._insert(path, params)}]
        if action == "set":
            row = self._find(path, params.pop(".id"))
            row.update({k: _s(v) for k, v in params.items()})
            return []
        if action == "renew":
            return []
        if action == "remove":
            row = self._find(path, params[".id"])
            self.tables[path].remove(row)
            return []
        raise RouterOSError(f"unknown action {action}")

    def _resource(self, _p: dict[str, Any]) -> list[dict[str, Any]]:
        up = int(time.time() - self.boot)
        total = 1024 * 1024 * 1024
        return [
            {
                "uptime": f"{up // 86400}d{(up % 86400) // 3600}h{(up % 3600) // 60}m{up % 60}s",
                "version": f"{self.version} ({self.channel})",
                "cpu-load": self.rng.randint(1, 45),
                "free-memory": total - self.rng.randint(150, 600) * 1024 * 1024,
                "total-memory": total,
                "free-hdd-space": 100 * 1024 * 1024,
                "total-hdd-space": 128 * 1024 * 1024,
                "architecture-name": "arm64",
                "board-name": self.board,
                "cpu-count": 4,
            }
        ]

    def _ping(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        count = int(p.get("count", 3))
        base = self.rng.uniform(5, 40)
        out = [{"seq": i, "host": p.get("address"), "time": f"{base + self.rng.uniform(0, 5):.1f}ms"} for i in range(count)]
        out.append({"sent": count, "received": count, "packet-loss": 0, "avg-rtt": f"{base + 2:.1f}ms"})
        return out

    def _monitor_traffic(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"name": p.get("interface"), "rx-bits-per-second": self.rng.randint(10**5, 10**8), "tx-bits-per-second": self.rng.randint(10**5, 10**7)}]

    def _export(self, _p: dict[str, Any]) -> list[dict[str, Any]]:
        lines = [f"# {time.strftime('%Y-%m-%d %H:%M:%S')} by RouterOS {self.version}", f"# model = {self.board}"]
        for path in sorted(self.tables):
            rows = [r for r in self.tables[path] if path != "/interface"]
            if not rows:
                continue
            lines.append("/" + path[1:].replace("/", " "))
            for r in rows:
                attrs = " ".join(f'{k}="{v}"' if " " in str(v) else f"{k}={v}" for k, v in r.items() if k not in (".id", "private-key", "password", "public-key"))
                lines.append(f"add {attrs}")
        lines.append("/system identity")
        lines.append(f"set name={self.identity}")
        return [{"ret": "\n".join(lines)}]

    def _check_updates(self, _p: dict[str, Any]) -> list[dict[str, Any]]:
        return self._update_print(_p)

    def _update_print(self, _p: dict[str, Any]) -> list[dict[str, Any]]:
        latest = "7.16.1"
        status = "New version is available" if self.version != latest else "System is already up to date"
        return [{"channel": self.channel, "installed-version": self.version, "latest-version": latest, "status": status}]

    def _update_set(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        self.channel = p.get("channel", self.channel)
        return []

    def _install(self, _p: dict[str, Any]) -> list[dict[str, Any]]:
        self.version = "7.16.1"
        self.boot = time.time()
        return []

    def _dns_set(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        self.dns.update({k: _s(v) for k, v in p.items()})
        return []


_PERSIST = ("version", "channel", "identity", "board", "tables", "_next_id", "dns", "counters", "boot", "down_hosts", "vrrp_master")


def _dump(r: SimRouter) -> str:
    import json

    data = {k: getattr(r, k) for k in _PERSIST}
    data["down_hosts"] = sorted(r.down_hosts)
    data["vrrp_master"] = sorted(r.vrrp_master)
    return json.dumps(data)


def _load(host: str, raw: str) -> SimRouter:
    import json

    r = SimRouter(host)
    for k, v in json.loads(raw).items():
        setattr(r, k, v)
    r.down_hosts = set(r.down_hosts)
    r.vrrp_master = set(getattr(r, "vrrp_master", []) or [])
    for p in _TABLE_PATHS:
        r.tables.setdefault(p, [])
    return r


def _s(v: Any) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


def _fake_key(seed: str) -> str:
    import base64

    raw = bytearray(hashlib.sha256(seed.encode()).digest())
    raw[31] &= 0x7F  # gültige Curve25519-Form, damit Validierung greift
    raw[0] &= 0xF8
    raw[31] |= 0x40
    return base64.b64encode(bytes(raw)).decode()


def get_router(host: str) -> SimRouter:
    if host not in _STATE:
        _STATE[host] = SimRouter(host)
    return _STATE[host]


def reset() -> None:
    _STATE.clear()


class SimulatedConnection:
    """Verbindung zum simulierten Router.

    Mit Redis wird der Zustand prozessübergreifend geteilt (API und Worker sehen denselben
    "Router"); ohne Redis (Tests) bleibt er im Prozess.
    """

    def __init__(self, host: str) -> None:
        self.host = host
        self.router = get_router(host)

    @staticmethod
    def _redis():
        from app.config import get_settings

        if not get_settings().use_redis:
            return None
        from app.events import _get_redis

        return _get_redis()

    async def load(self) -> None:
        r = self._redis()
        if r is None:
            return
        try:
            raw = await r.get(f"sdwan:sim:{self.host}")
        except Exception:  # noqa: BLE001 - Simulator soll auch ohne Redis laufen
            return
        if raw:
            self.router = _STATE[self.host] = _load(self.host, raw)

    async def call(self, cmd: str, **params: Any) -> list[dict[str, Any]]:
        return self.router.call(cmd, dict(params))

    async def close(self) -> None:
        r = self._redis()
        if r is None:
            return
        try:
            await r.set(f"sdwan:sim:{self.host}", _dump(self.router))
        except Exception:  # noqa: BLE001
            pass
