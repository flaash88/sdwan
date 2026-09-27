"""WireGuard-Hub-Agent: Peer-Sync gegen den Ist-Zustand des Interfaces (nicht gegen die Datei), Healthcheck, Alarm."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import time

import pytest

AGENT = pathlib.Path(__file__).resolve().parents[2] / "hub" / "agent.py"
PEERS = [{"device_id": "d1", "public_key": "K1" + "A" * 41 + "=", "allowed_ips": "10.100.0.2/32"},
         {"device_id": "d2", "public_key": "K2" + "A" * 41 + "=", "allowed_ips": "10.100.0.3/32"}]


class FakeWg:
    """Kernel-Interface: Peers leben nur im Speicher (nach Neustart leer); ``drop`` simuliert einen unvollständigen Sync."""

    def __init__(self) -> None:
        self.peers: dict[str, str] = {}
        self.syncs = 0
        self.drop = 0

    def sh(self, *args: str, check: bool = True, inp: str | None = None) -> str:
        if args[:3] == ("wg", "show", "wg0") and args[3] == "dump":
            head = "PRIVKEY\tPUBKEY\t51820\toff"
            return "\n".join([head, *(f"{k}\t(none)\t(none)\t{v}\t0\t0\t0\toff" for k, v in self.peers.items())])
        if args[:2] == ("wg", "syncconf"):
            self.syncs += 1
            text = pathlib.Path(args[3]).read_text()
            keys = [line.split(" = ", 1)[1] for line in text.splitlines() if line.startswith("PublicKey")]
            ips = [line.split(" = ", 1)[1] for line in text.splitlines() if line.startswith("AllowedIPs")]
            self.peers = dict(list(zip(keys, ips))[: len(keys) - self.drop])
            return ""
        raise AssertionError(f"unerwarteter Befehl {args}")


@pytest.fixture
def agent(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("hub_agent_test", AGENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    (tmp_path / "hub.key").write_text("PRIVKEY")
    monkeypatch.setattr(mod, "KEY_FILE", str(tmp_path / "hub.key"))
    monkeypatch.setattr(mod, "CONF_FILE", str(tmp_path / "wg0.conf"))
    monkeypatch.setattr(mod, "HEALTH_FILE", str(tmp_path / "health.json"))
    wg = FakeWg()
    monkeypatch.setattr(mod, "sh", wg.sh)
    monkeypatch.setattr(mod, "api", lambda method, path, body=None: [dict(p) for p in PEERS])
    mod._wg = wg
    return mod


def test_file_present_but_interface_empty_triggers_syncconf(agent):
    wg = agent._wg
    agent.sync_peers(51820)
    assert wg.syncs == 1 and len(wg.peers) == 2
    # Hub-Neustart: Datei im Volume unverändert, Interface leer → trotzdem syncconf (früher: nie wieder)
    wg.peers = {}
    assert agent.sync_peers(51820) is True
    assert wg.syncs == 2 and len(wg.peers) == 2
    assert agent.health()[0] is True


def test_interface_correct_no_syncconf_and_start_forces(agent):
    wg = agent._wg
    agent.sync_peers(51820)
    assert agent.sync_peers(51820) is False and wg.syncs == 1  # Ist = Soll → kein syncconf
    assert agent.sync_peers(51820, force=True) is True and wg.syncs == 2  # beim Start immer einmal
    # geänderte AllowedIPs werden ebenfalls erkannt
    wg.peers[PEERS[0]["public_key"]] = "10.100.0.99/32"
    assert agent.sync_peers(51820) is True and wg.peers[PEERS[0]["public_key"]] == "10.100.0.2/32"


def test_peer_count_mismatch_resyncs_and_health(agent):
    wg = agent._wg
    wg.drop = 1  # syncconf übernimmt nur einen der zwei Peers
    agent.sync_peers(51820)
    assert wg.syncs == 1 and len(wg.peers) == 1 and agent.STATE.resync is True
    ok, text = agent.health()
    assert ok is False and "1" in text and "2" in text
    wg.drop = 0
    assert agent.sync_peers(51820) is True and wg.syncs == 2 and len(wg.peers) == 2  # erneuter Sync
    assert agent.STATE.resync is False and agent.health()[0] is True
    # letzter erfolgreicher Sync zu alt → unhealthy
    st = json.loads(pathlib.Path(agent.HEALTH_FILE).read_text())
    assert agent.health(now=st["last_ok"] + 181)[0] is False
    assert agent.health(now=time.time())[0] is True


def test_health_without_status_file(agent):
    assert agent.health()[0] is False


async def test_platform_alert_hub_no_peers(client, msp, hub):
    from app.db import system_session
    from app.models import PlatformAlert
    from tests.conftest import make_paired_device, make_tenant, make_tenant_admin

    hdr = {"X-Hub-Token": "hubtoken"}
    # ohne gekoppelte Geräte: 0 Peers ist normal
    assert (await client.post("/api/v1/internal/hub/stats", headers=hdr, json=[])).status_code == 200
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    dev = await make_paired_device(client, h)
    await client.post("/api/v1/internal/hub/stats", headers=hdr, json=[])
    alerts = (await client.get("/api/v1/platform/alerts", headers=msp)).json()
    items = alerts if isinstance(alerts, list) else alerts.get("alerts", alerts.get("items", []))
    firing = [a for a in items if a["type"] == "hub_no_peers" and a["status"] == "firing"]
    assert len(firing) == 1 and "1 Geräte" in firing[0]["message"]
    await client.post("/api/v1/internal/hub/stats", headers=hdr, json=[])  # kein zweiter Alarm
    await client.post("/api/v1/internal/hub/stats", headers=hdr, json=[
        {"public_key": dev["wg_public_key"], "endpoint": None, "latest_handshake": 0, "rx_bytes": 0, "tx_bytes": 0}])
    async with system_session() as db:
        from sqlalchemy import select

        rows = (await db.execute(select(PlatformAlert).where(PlatformAlert.type == "hub_no_peers"))).scalars().all()
    assert [r.status for r in rows] == ["resolved"]


# ----------------------------------------------------------------------------- Route im Hub-Namespace
ENTRYPOINT = pathlib.Path(__file__).resolve().parents[1] / "docker-entrypoint.sh"


def _run_entrypoint(tmp_path, wg_present: bool) -> tuple[str, str]:
    import os
    import subprocess

    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "ip.log"
    (bindir / "ip").write_text(f'#!/bin/sh\necho "$@" >> {log}\n'
                               f'if [ "$1" = "link" ]; then exit {0 if wg_present else 1}; fi\nexit 0\n')
    (bindir / "ip").chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "HUB_INTERNAL_IP": "172.30.0.10", "WG_NETWORK": "10.100.0.0/16",
           "RUN_MIGRATIONS": "false"}
    res = subprocess.run(["sh", str(ENTRYPOINT), "true"], env=env, capture_output=True, text=True, check=True)
    return res.stdout, log.read_text() if log.exists() else ""


def test_entrypoint_skips_route_in_hub_namespace(tmp_path):
    out, calls = _run_entrypoint(tmp_path, wg_present=True)
    assert "im Hub-Namespace – Route übersprungen" in out and "route replace" not in calls
    out, calls = _run_entrypoint(tmp_path, wg_present=False)  # api/worker: Route wie bisher
    assert "route replace 10.100.0.0/16 via 172.30.0.10" in calls and "route 10.100.0.0/16 via 172.30.0.10" in out


def test_compose_syslog_flows_without_route_env_and_net_admin():
    import yaml

    d = yaml.safe_load((pathlib.Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text())
    for name in ("syslog", "flows"):
        svc = d["services"][name]
        assert svc["network_mode"] == "service:wireguard-hub" and svc["cap_add"] == []
        assert svc["environment"]["HUB_INTERNAL_IP"] == "" and svc["environment"]["WG_NETWORK"] == ""
    assert d["services"]["api"]["cap_add"] == ["NET_ADMIN"]  # api/worker brauchen die Route weiterhin


def test_empty_wg_network_falls_back_to_default():
    from app.config import Settings

    assert str(Settings(wg_network="").wg_net) == "10.100.0.0/16"


class FakeRoutes:
    def __init__(self, route: str) -> None:
        self.route = route
        self.replaced: list[tuple[str, ...]] = []

    def sh(self, *args: str, check: bool = True, inp: str | None = None) -> str:
        if args[:3] == ("ip", "route", "show"):
            return self.route
        if args[:3] == ("ip", "route", "replace"):
            self.replaced.append(args)
            self.route = f"{args[3]} dev {args[5]} scope link src {args[7]}"
            return ""
        raise AssertionError(args)


def test_agent_corrects_wrong_route_and_health(agent, monkeypatch):
    fr = FakeRoutes("10.100.0.0/16 via 172.30.0.10 dev eth0")  # vom Backend-Entrypoint umgebogen
    monkeypatch.setattr(agent, "sh", fr.sh)
    assert agent.ensure_route("10.100.0.1/16") is True
    assert fr.replaced == [("ip", "route", "replace", "10.100.0.0/16", "dev", "wg0", "src", "10.100.0.1")]
    # korrekte Route → nichts tun
    assert agent.ensure_route("10.100.0.1/16") is True and len(fr.replaced) == 1
    # Korrektur scheitert → Healthcheck unhealthy
    class Stuck(FakeRoutes):
        def sh(self, *args, **kw):
            if args[:3] == ("ip", "route", "replace"):
                raise RuntimeError("RTNETLINK answers: Operation not permitted")
            return super().sh(*args, **kw)

    st = Stuck("10.100.0.0/16 via 172.30.0.10 dev eth0")
    monkeypatch.setattr(agent, "sh", st.sh)
    assert agent.ensure_route("10.100.0.1/16") is False and agent.STATE.route_ok is False
    monkeypatch.setattr(agent, "sh", agent._wg.sh)
    agent.sync_peers(51820)  # schreibt den Status
    ok, text = agent.health()
    assert ok is False and "Route" in text
