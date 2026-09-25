"""Generator für RouterOS-Onboarding-Scripts (.rsc) und Pairing-Logik.

Ablauf:
1. Techniker legt ein Device an -> einmaliger Pairing-Token (nur Hash in der DB).
2. Auf dem Router wird EIN Befehl ausgeführt::

       /tool fetch url="https://cloud/api/v1/onboard/<token>.rsc" dst-path=sdwan-onboard.rsc; /import sdwan-onboard.rsc

3. Das Script legt das WireGuard-Interface ``sdwan-mgmt`` an (RouterOS erzeugt den
   privaten Schlüssel lokal – er verlässt das Gerät nie), liest den Public-Key und
   sendet ihn samt Token, Seriennummer und Version per POST an ``/api/v1/pair``.
4. Die Antwort ist wiederum ein RSC-Script mit Tunnel-IP, Hub-Peer, API-Benutzer
   (zufälliges Passwort pro Gerät) und Firewall-Freigabe nur für die Hub-Adresse.
"""

from __future__ import annotations

import datetime as dt
import re

from app.config import get_settings
from app.models import Device
from app.routeros.schema import API_GROUP, API_POLICIES

_SAFE = re.compile(r"^[A-Za-z0-9._:/+=@, -]*$")


def _q(value: str) -> str:
    """Quote für RouterOS-Scripts. Nur unkritische Zeichen erlaubt (kein Injection-Risiko)."""
    if not _SAFE.match(value):
        raise ValueError(f"unsicherer Wert für RouterOS-Script: {value!r}")
    return f'"{value}"'


def onboarding_command(token: str) -> str:
    base = get_settings().public_url.rstrip("/")
    return (
        f'/tool fetch url="{base}/api/v1/onboard/{token}.rsc" dst-path=sdwan-onboard.rsc; '
        f":delay 2s; /import file-name=sdwan-onboard.rsc"
    )


def onboarding_script(token: str, device_name: str) -> str:
    s = get_settings()
    base = s.public_url.rstrip("/")
    iface = s.wg_device_interface
    return f"""# ==========================================================
# MikroTik SD-WAN Onboarding – Gerät: {device_name}
# Generiert: {dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
# Voraussetzung: RouterOS >= 7.1, ausgehende HTTPS- und UDP/{s.wg_hub_port}-Verbindung
# ==========================================================
:local token {_q(token)}
:local cloud {_q(base)}
:local iface {_q(iface)}

:local ver [/system resource get version]
:if ([:pick $ver 0 1] != "7") do={{ :error "SD-WAN: RouterOS 7 erforderlich" }}

:if ([:len [/interface wireguard find name=$iface]] = 0) do={{
  /interface wireguard add name=$iface listen-port={s.wg_device_listen_port} mtu=1420 comment="sdwan:mgmt"
  :delay 1s
}}
:local pub [/interface wireguard get [find name=$iface] public-key]

:local serial ""
:do {{ :set serial [/system routerboard get serial-number] }} on-error={{ }}
:if ([:len $serial] = 0) do={{ :do {{ :set serial [/system license get system-id] }} on-error={{ :set serial "unknown" }} }}
:local model [/system resource get board-name]
:local arch [/system resource get architecture-name]
:local ident [/system identity get name]

:local body ("{{\\"token\\":\\"" . $token . "\\",\\"public_key\\":\\"" . $pub . "\\",\\"serial\\":\\"" . $serial . "\\",\\"routeros_version\\":\\"" . $ver . "\\",\\"model\\":\\"" . $model . "\\",\\"architecture\\":\\"" . $arch . "\\",\\"identity\\":\\"" . $ident . "\\"}}")

:put "SD-WAN: registriere Gerät bei $cloud ..."
/tool fetch url=($cloud . "/api/v1/pair") http-method=post http-header-field="Content-Type: application/json" http-data=$body dst-path="sdwan-pair.rsc"
:delay 2s
/import file-name=sdwan-pair.rsc
/file remove [find name="sdwan-pair.rsc"]
/file remove [find name="sdwan-onboard.rsc"]
:put "SD-WAN: Onboarding abgeschlossen."
"""


def pair_response_script(device: Device, hub_public_key: str, api_password: str, extra: str = "") -> str:
    """Konfiguration, die der Router nach erfolgreichem Pairing importiert."""
    s = get_settings()
    iface = s.wg_device_interface
    prefix = s.wg_net.prefixlen
    hub_ip = s.wg_hub_ip
    return f"""# SD-WAN Pairing-Antwort für Device {device.id}
:local iface {_q(iface)}
/ip address remove [find comment="sdwan:mgmt"]
/ip address add address={device.tunnel_ip}/{prefix} interface=$iface comment="sdwan:mgmt"
/interface wireguard peers remove [find comment="sdwan:hub"]
/interface wireguard peers add interface=$iface public-key={_q(hub_public_key)} endpoint-address={_q(s.wg_hub_endpoint)} endpoint-port={s.wg_hub_port} allowed-address={hub_ip}/32 persistent-keepalive=25s comment="sdwan:hub"

# Eigene Gruppe mit genau den nötigen Rechten (nie "full"); vorhandene Gruppe wird nur aktualisiert
:if ([:len [/user group find name={_q(API_GROUP)}]] = 0) do={{
  /user group add name={_q(API_GROUP)} policy={",".join(API_POLICIES)} comment="sdwan:mgmt"
}} else={{
  /user group set [find name={_q(API_GROUP)}] policy={",".join(API_POLICIES)}
}}
# API-Benutzer nur aus dem Tunnel erreichbar
/user remove [find name={_q(s.routeros_api_user)}]
/user add name={_q(s.routeros_api_user)} group={_q(API_GROUP)} password={_q(api_password)} address={hub_ip}/32 comment="sdwan:mgmt"
/ip service set api disabled=no address={hub_ip}/32

# Firewall: Management-Zugriff ausschließlich vom Hub über das Tunnel-Interface
/ip firewall filter remove [find comment="sdwan:mgmt"]
:if ([:len [/ip firewall filter find]] > 0) do={{
  /ip firewall filter add chain=input in-interface=$iface src-address={hub_ip} action=accept comment="sdwan:mgmt" place-before=0
}} else={{
  /ip firewall filter add chain=input in-interface=$iface src-address={hub_ip} action=accept comment="sdwan:mgmt"
}}
/system note set note="Managed by SD-WAN Cloud – Device {device.id}" show-at-login=yes
{extra}
:put "SD-WAN: Tunnel-IP {device.tunnel_ip}, Device-ID {device.id}"
"""
