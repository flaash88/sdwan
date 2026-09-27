"""Phase 25 – Nachbarn/Topologie, Top-Verbraucher (IPFIX), Inventar/EOL, ZTP-Massenimport."""

from __future__ import annotations

import datetime as dt
import ipaddress
import struct

from app.db import system_session, utcnow
from app.models import FlowAggregate
from app.routeros.simulator import get_router
from app.services import flows
from app.services.poller import poll_all
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin

WAN = [{"name": "Glasfaser", "interface": "ether1", "gateway": "192.0.2.1", "priority": 1, "check_target": "1.1.1.1"}]


async def _setup(client, msp, name="n1"):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    site = (await client.post("/api/v1/sites", json={"name": "Zentrale"}, headers=h)).json()
    dev = await make_paired_device(client, h, site_id=site["id"], name=name)
    return t, h, site, dev


# ----------------------------------------------------------------------------- Nachbarn
async def test_neighbors_and_topology(client, msp, hub):
    t, h, site, dev = await _setup(client, msp)
    dev2 = await make_paired_device(client, h, site_id=site["id"], name="n2")
    rt, rt2 = get_router(dev["tunnel_ip"]), get_router(dev2["tunnel_ip"])
    # n2 sieht n1 per Discovery (Identity) → Verknüpfung in der Topologie
    rt2.tables["/ip/neighbor"].append({".id": "*N9", "interface": "ether3", "identity": rt.identity, "platform": "MikroTik",
                                       "board": "CHR", "mac-address": "aa:bb:cc:00:00:01", "address": "192.168.88.1"})
    assert (await client.get(f"/api/v1/devices/{dev['id']}/neighbors", headers=h)).json() == []
    await poll_all()
    nb = (await client.get(f"/api/v1/devices/{dev['id']}/neighbors", headers=h)).json()
    assert [(n["interface"], n["identity"], n["mac_address"]) for n in nb] == [("ether2", "switch-lager", "48:A9:8A:00:11:22")]
    assert nb[0]["device"] is None
    topo = (await client.get(f"/api/v1/sites/{site['id']}/topology", headers=h)).json()
    n2 = next(d for d in topo["devices"] if d["name"] == "n2")
    linked = next(n for n in n2["neighbors"] if n["identity"] == rt.identity)
    assert linked["device"]["name"] == "n1" and linked["interface"] == "ether3"
    # zweiter Poll innerhalb von 10 min liest nicht erneut; ersetzt wird erst beim nächsten Intervall
    rt.tables["/ip/neighbor"].clear()
    await poll_all()
    assert len((await client.get(f"/api/v1/devices/{dev['id']}/neighbors", headers=h)).json()) == 1


# ----------------------------------------------------------------------------- IPFIX
def _ipfix(domain: int, records: list[tuple[str, str, int, int, int, int]], template: bool = True) -> bytes:
    fields = [(8, 4), (12, 4), (1, 8), (2, 8), (10, 4), (14, 4), (7, 2)]  # inkl. Port (wird ignoriert)
    sets = b""
    if template:
        tbody = struct.pack("!HH", 256, len(fields)) + b"".join(struct.pack("!HH", ie, ln) for ie, ln in fields)
        sets += struct.pack("!HH", 2, 4 + len(tbody)) + tbody
    if records:
        dbody = b"".join(ipaddress.IPv4Address(s).packed + ipaddress.IPv4Address(d).packed + struct.pack("!QQIIH", o, p, i, e, 443)
                         for s, d, o, p, i, e in records)
        sets += struct.pack("!HH", 256, 4 + len(dbody)) + dbody
    return struct.pack("!HHIII", 10, 16 + len(sets), 0, 1, domain) + sets


def test_ipfix_parser_templates_and_orientation():
    p = flows.IpfixParser()
    # Daten vor dem Template werden verworfen
    assert p.parse("10.100.0.2", _ipfix(1, [("192.168.88.10", "1.1.1.1", 100, 1, 2, 1)], template=False)) == []
    recs = p.parse("10.100.0.2", _ipfix(1, [("192.168.88.10", "1.1.1.1", 1500, 3, 2, 1), ("9.9.9.9", "192.168.88.20", 9000, 7, 1, 2)]))
    assert len(recs) == 2 and recs[0][8] == "192.168.88.10" and recs[0][1] == 1500 and recs[1][10] == 1
    assert flows.orient(recs[0]) == ("192.168.88.10", "1.1.1.1", 1)  # Upload: WAN = egress
    assert flows.orient(recs[1]) == ("192.168.88.20", "9.9.9.9", 1)  # Download: WAN = ingress
    assert flows.orient({8: "192.168.88.1", 12: "192.168.88.2"}) is None  # intern
    # Template gilt je Quelle/Domain
    assert p.parse("10.100.0.3", _ipfix(1, [("192.168.88.10", "1.1.1.1", 1, 1, 2, 1)], template=False)) == []
    assert p.parse("10.100.0.2", b"\x00\x09" + b"\x00" * 30) == []  # NetFlow v9 wird ignoriert


async def test_flows_opt_in_collect_top_and_retention(client, msp, hub):
    t, h, site, dev = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    assert (await client.get(f"/api/v1/devices/{dev['id']}/flows", headers=h)).json()["enabled"] is False
    assert (await client.put(f"/api/v1/devices/{dev['id']}/flows", json={"enabled": True}, headers=h)).status_code == 422  # ohne WAN
    await client.put(f"/api/v1/devices/{dev['id']}/wan", json={"mode": "failover", "links": WAN}, headers=h)
    r = await client.put(f"/api/v1/devices/{dev['id']}/flows", json={"enabled": True}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "active", r.text
    assert rt.traffic_flow["enabled"] == "yes" and rt.traffic_flow["interfaces"] == "ether1"
    (tgt,) = rt.tables["/ip/traffic-flow/target"]
    assert tgt["version"] == "ipfix" and tgt["port"] == "2055" and tgt["comment"] == "sdwan:flow"
    ether1_idx = str(int(next(i for i in rt.tables["/interface"] if i["name"] == "ether1")[".id"].lstrip("*"), 16))
    # Collector: Datagramme der Tunnel-IP → Aggregate
    from app.flow_collector import Receiver, flush

    rx = Receiver(flows.DeviceMap())
    async with system_session() as db:
        await rx.dmap.refresh(db)
    idx = int(ether1_idx)
    rx.datagram_received(_ipfix(0, [("192.168.88.10", "1.1.1.1", 5000, 5, 2, idx), ("1.1.1.1", "192.168.88.10", 20000, 20, idx, 2),
                                    ("192.168.88.11", "8.8.8.8", 1000, 1, 2, idx)]), (dev["tunnel_ip"], 4739))
    rx.datagram_received(_ipfix(0, [("192.168.88.10", "1.1.1.1", 5000, 5, 2, idx)]), ("203.0.113.99", 4739))  # unbekannte Quelle
    assert await flush(rx) == 2
    top = (await client.get(f"/api/v1/devices/{dev['id']}/flows/top", params={"period": "1h"}, headers=h)).json()
    assert top["hosts"][0] == {"address": "192.168.88.10", "bytes": 25000, "packets": 25}
    assert [d["address"] for d in top["destinations"]] == ["1.1.1.1", "8.8.8.8"] and top["wans"] == ["ether1"] and top["total_bytes"] == 26000
    assert (await client.get(f"/api/v1/devices/{dev['id']}/flows/top", params={"wan": "ether8"}, headers=h)).json()["total_bytes"] == 0
    # Top-N: Rest wird als „andere“ zusammengefasst
    import uuid

    agg = flows.Aggregator()
    agg.add(uuid.UUID(dev["id"]), utcnow(), [{8: "192.168.88.10", 12: f"1.1.{i // 250}.{i % 250 + 1}", 1: 1000 - i, 2: 1, 14: idx}
                                              for i in range(flows.TOP_PER_BUCKET + 5)])
    async with system_session() as db:
        dm = flows.DeviceMap()
        await dm.refresh(db)
        assert await flows.store(db, dm, agg.take()) == flows.TOP_PER_BUCKET + 1
        await db.commit()
    top = (await client.get(f"/api/v1/devices/{dev['id']}/flows/top", params={"period": "1h", "limit": 100}, headers=h)).json()
    assert "andere" in [d["address"] for d in top["destinations"]]
    # Aufbewahrung je Mandant
    async with system_session() as db:
        await db.execute(FlowAggregate.__table__.update().values(bucket=utcnow() - dt.timedelta(days=8)))
        await db.commit()
    assert await flows.purge_old() > 0
    assert (await client.get(f"/api/v1/devices/{dev['id']}/flows/top", params={"period": "7d"}, headers=h)).json()["total_bytes"] == 0
    # Abschalten stellt den Vorzustand wieder her
    r = await client.put(f"/api/v1/devices/{dev['id']}/flows", json={"enabled": False}, headers=h)
    assert r.json()["enabled"] is False and rt.traffic_flow["enabled"] == "no" and rt.traffic_flow["interfaces"] == "all"
    assert rt.tables["/ip/traffic-flow/target"] == []


# ----------------------------------------------------------------------------- Inventar
async def test_inventory_eol_and_csv(client, msp, hub):
    t, h, site, dev = await _setup(client, msp)
    eol = (await client.get("/api/v1/eol-models", headers=h)).json()
    assert len(eol) == 1 and eol[0]["builtin"] and not eol[0]["enabled"]  # Seed: nur deaktiviertes Beispiel
    rows = (await client.get("/api/v1/inventory", headers=h)).json()
    assert rows[0]["serial"] and rows[0]["model"] == "CHR" and rows[0]["warnings"] == []
    assert (await client.post("/api/v1/eol-models", json={"model": "chr"}, headers=h)).status_code == 403  # nur MSP
    assert (await client.post("/api/v1/eol-models", json={"model": " chr ", "status": "end_of_sale", "successor": "CHR2"}, headers=msp)).status_code == 201
    soon = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    r = await client.put(f"/api/v1/devices/{dev['id']}/inventory", json={"purchase_date": "2024-01-10", "warranty_until": soon,
                                                                       "supplier": "Beispiel-Distributor", "notes": "Rack 2"}, headers=h)
    assert r.status_code == 200, r.text
    assert {w["type"] for w in r.json()["warnings"]} == {"eol", "warranty_soon"} and "CHR2" in r.json()["warnings"][0]["text"]
    assert (await client.put(f"/api/v1/devices/{dev['id']}/inventory", json={"purchase_date": "2024-01-10", "warranty_until": "2023-01-01"},
                             headers=h)).status_code == 422
    await client.put(f"/api/v1/devices/{dev['id']}/inventory", json={"purchase_date": "2020-01-10", "warranty_until": "2022-01-10"}, headers=h)
    csv_r = await client.get("/api/v1/inventory.csv", headers=h)
    text = csv_r.content.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("Gerät;Seriennummer;Modell") and "2022-01-10" in text and "Garantie abgelaufen" in text
    assert "nicht mehr lieferbar" in text


# ----------------------------------------------------------------------------- ZTP-Import
CSV = """name;serial;model;site;template;tags
filiale-01;SN1001;hAP ax3;Zentrale;;kasse|nord
filiale-02;SN1002;;Unbekannt;;
filiale-01;SN1003;;;;
filiale-04;SN1001;;;;
filiale-05;bad serial!;;;;
filiale-06;SN1006;;;Keine Vorlage;
filiale-07;SN1007;;;;
"""


async def test_ztp_import_preview_and_commit(client, msp, hub):
    t, h, site, dev = await _setup(client, msp)
    r = await client.post("/api/v1/ztp/import/preview", json={"csv": "foo;bar\n1;2"}, headers=h)
    assert r.status_code == 422 and "Spalte" in r.text
    r = await client.post("/api/v1/ztp/import/preview", json={"csv": CSV}, headers=h)
    assert r.status_code == 200, r.text
    p = r.json()
    rows = {x["line"]: x for x in p["rows"]}
    assert p["valid"] == 2 and p["invalid"] == 5
    assert rows[2]["ok"] and rows[2]["site"] == "Zentrale" and rows[2]["tags"] == ["kasse", "nord"]
    assert "Standort „Unbekannt“ unbekannt" in rows[3]["errors"]
    assert any("Gerätename doppelt" in e for e in rows[4]["errors"])
    assert any("Seriennummer doppelt (Zeile 2)" in e for e in rows[5]["errors"])
    assert any("Seriennummer:" in e for e in rows[6]["errors"])
    assert any("Vorlage" in e for e in rows[7]["errors"])
    assert rows[8]["ok"]
    # nichts angelegt ohne Bestätigung
    assert (await client.post("/api/v1/ztp/import/commit", json={"csv": CSV}, headers=h)).status_code == 422
    assert len((await client.get("/api/v1/ztp/devices", headers=h)).json()) == 0
    r = await client.post("/api/v1/ztp/import/commit", json={"csv": CSV, "confirm": True}, headers=h)
    assert r.status_code == 201, r.text
    res = r.json()
    assert [c["device"]["serial"] for c in res["created"]] == ["SN1001", "SN1007"] and len(res["skipped"]) == 5
    assert "SN1001" in res["created"][0]["bootstrap_script"]
    ztp = {d["serial"]: d for d in (await client.get("/api/v1/ztp/devices", headers=h)).json()}
    assert set(ztp) == {"SN1001", "SN1007"} and ztp["SN1001"]["site_id"] == site["id"]
    # erneuter Import: bereits registriert
    p2 = (await client.post("/api/v1/ztp/import/preview", json={"csv": CSV}, headers=h)).json()
    assert p2["valid"] == 0 and "Seriennummer bereits registriert" in {e for x in p2["rows"] for e in x["errors"]}
