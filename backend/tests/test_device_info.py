"""Übersicht: IP-Adressen je Interface (inkl. DHCP-Kennzeichnung) und Sensorwerte nur, wenn vorhanden."""

from __future__ import annotations

from app.routeros.simulator import get_router
from app.services.device_info import parse_health, pick_health
from app.services.poller import poll_all
from tests.conftest import make_paired_device, make_tenant, make_tenant_admin


async def _setup(client, msp):
    t = await make_tenant(client, msp)
    h = await make_tenant_admin(client, msp, t["id"])
    return h, await make_paired_device(client, h, name="lab")


async def test_addresses_live_with_dhcp_and_cache(client, msp, hub):
    h, dev = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    rt._insert("/ip/dhcp-client", {"interface": "ether1", "status": "bound", "disabled": "false"})
    rt._insert("/ip/address", {"address": "100.64.7.2/24", "network": "100.64.7.0", "interface": "ether1", "dynamic": "true"})
    rt._insert("/ip/address", {"address": "10.9.9.1/24", "interface": "ether3", "disabled": "true", "comment": "alt"})
    r = await client.get(f"/api/v1/devices/{dev['id']}/addresses", headers=h)
    assert r.status_code == 200 and r.json()["source"] == "live"
    by = {a["address"]: a for a in r.json()["addresses"]}
    assert by["100.64.7.2/24"]["dhcp"] is True and by["100.64.7.2/24"]["dynamic"] is True
    assert by["192.168.88.1/24"]["dhcp"] is False and by["192.168.88.1/24"]["interface"] == "bridge"
    assert by["10.9.9.1/24"]["disabled"] is True and by["10.9.9.1/24"]["comment"] == "alt"
    # offline -> Stand der letzten Abfrage
    await poll_all()
    rt.offline = True
    await poll_all()
    await poll_all()
    r = (await client.get(f"/api/v1/devices/{dev['id']}/addresses", headers=h)).json()
    assert r["source"] == "cache" and any(a["address"] == "100.64.7.2/24" and a["dhcp"] for a in r["addresses"])


async def test_health_only_when_model_provides_it(client, msp, hub):
    h, dev = await _setup(client, msp)
    rt = get_router(dev["tunnel_ip"])
    rt.board = "CHR"
    await poll_all()
    d = (await client.get(f"/api/v1/devices/{dev['id']}", headers=h)).json()
    assert d["facts"]["health"] == []  # Kacheln ausgeblendet
    rt.board = "L009UiGS"
    await poll_all()
    d = (await client.get(f"/api/v1/devices/{dev['id']}", headers=h)).json()
    names = {s["name"]: s for s in d["facts"]["health"]}
    assert names["cpu-temperature"]["value"] == 47 and names["cpu-temperature"]["unit"] == "C"
    assert names["voltage"]["unit"] == "V"


def test_parse_health_formats():
    v7 = [{"name": "cpu-temperature", "value": "52", "type": "C"}, {"name": "board-temperature1", "value": "41", "type": "°C"},
          {"name": "fan1-speed", "value": "3200", "type": "RPM"}, {"name": "psu1-state", "value": "ok", "type": ""},
          {"name": "voltage", "value": "23.9", "type": "V"}]
    s = parse_health(v7)
    assert [x["name"] for x in s] == ["cpu-temperature", "board-temperature1", "voltage"]
    assert pick_health(s)["temperature"]["value"] == 52 and pick_health(s)["voltage"]["value"] == 23.9
    flat = [{"temperature": "38", "voltage": "12.1", "fan-mode": "auto"}]
    s = parse_health(flat)
    assert {x["name"]: x["unit"] for x in s} == {"temperature": "C", "voltage": "V"}
    only_board = parse_health([{"name": "board-temperature1", "value": "40", "type": "C"}])
    assert pick_health(only_board)["temperature"]["name"] == "board-temperature1" and "voltage" not in pick_health(only_board)
    assert parse_health([]) == [] and pick_health([]) == {}


def test_network_computed_when_missing():
    from app.services.device_info import build_addresses

    (a,) = build_addresses([{"address": "10.20.30.40/22", "interface": "vlan20"}], [])
    assert a["network"] == "10.20.28.0" and a["dhcp"] is False and a["dynamic"] is False
