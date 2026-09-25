"""Zentrale Liste der RouterOS-Pfade und Felder, die die Plattform liest.

Einzige Quelle für den Hardware-Selbsttest (``services/selftest.py``) und den Simulator-Test
(``tests/test_selftest.py``). Wird Code ergänzt, der ein neues Feld liest, gehört es hier dazu.

* ``fields``: Pflichtfelder – fehlt eines in einer Zeile, funktioniert ein Teil der Plattform nicht (rot).
* ``optional``: Felder, die RouterOS weglässt, wenn sie leer/ungesetzt sind (z. B. ``comment``) oder die
  modell-/versionsabhängig sind. Fehlen sie in allen Zeilen, gibt es einen Hinweis (grün) bzw. eine
  Warnung (orange), wenn ``warn_if_missing`` gesetzt ist.
* ``must_have_rows``: Tabelle darf auf einem verbundenen Gerät nicht leer sein (sonst orange).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PathSpec:
    key: str
    label: str
    command: str  # vollständiger API-Befehl, nur lesend
    fields: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    used_by: str = ""
    must_have_rows: bool = False
    params: dict[str, str] = field(default_factory=dict)
    warn_if_missing: dict[str, str] = field(default_factory=dict)  # optionales Feld -> Warnhinweis
    hints: dict[str, str] = field(default_factory=dict)  # optionales Feld -> Hinweis (grün)


PATH_SPECS: tuple[PathSpec, ...] = (
    PathSpec("resource", "Systemressourcen", "/system/resource/print",
             fields=("uptime", "version", "cpu-load", "free-memory", "total-memory", "board-name", "architecture-name"),
             optional=("cpu-count",), used_by="poller, metrics, firmware", must_have_rows=True),
    PathSpec("routerboard", "RouterBOARD", "/system/routerboard/print",
             optional=("serial-number", "model", "current-firmware", "upgrade-firmware"), used_by="firmware (RouterBOARD-Upgrade)",
             hints={"upgrade-firmware": "Kein RouterBOARD (z. B. CHR) – Bootloader-Upgrade entfällt"}),
    PathSpec("identity", "Identity", "/system/identity/print", fields=("name",), used_by="poller", must_have_rows=True),
    PathSpec("clock", "Systemuhr", "/system/clock/print", fields=("time", "date"), optional=("time-zone-name", "gmt-offset"),
             used_by="Selbsttest (Zeitabweichung)", must_have_rows=True,
             warn_if_missing={"gmt-offset": "Ohne gmt-offset kann die Abweichung nicht in UTC umgerechnet werden"}),
    PathSpec("interface", "Interfaces", "/interface/print", fields=("name", "type", "running", "rx-byte", "tx-byte"),
             optional=("comment", "default-name", "disabled"), used_by="metrics, Interface-Auswahl, WAN-Volumen", must_have_rows=True),
    PathSpec("wireguard", "WireGuard-Interfaces", "/interface/wireguard/print", fields=("name", "public-key"),
             optional=("listen-port", "comment"), used_by="Management-Tunnel, VPN-Mesh", must_have_rows=True),
    PathSpec("wireguard_peers", "WireGuard-Peers", "/interface/wireguard/peers/print", fields=("interface", "rx", "tx"),
             optional=("comment", "last-handshake", "public-key"), used_by="VPN-Mesh-Status", must_have_rows=True,
             hints={"last-handshake": "Fehlt bis zum ersten Handshake"}),
    PathSpec("vrrp", "VRRP", "/interface/vrrp/print", fields=("name", "running"),
             optional=("master", "backup", "disabled", "comment"), used_by="VRRP-Status",
             warn_if_missing={"master": "Status-Flag 'master' nicht als Feld geliefert – Rolle wird über 'running' abgeleitet",
                              "backup": "Status-Flag 'backup' nicht als Feld geliefert – Rolle wird über 'running' abgeleitet"}),
    PathSpec("ip_address", "IP-Adressen", "/ip/address/print", fields=("address", "interface"),
             optional=("network", "dynamic", "disabled", "invalid", "comment"), used_by="WAN/VRRP/Mesh-Adressen, Übersicht", must_have_rows=True),
    PathSpec("ip_route", "Routen", "/ip/route/print", fields=("dst-address",),
             optional=("gateway", "distance", "active", "disabled", "routing-table", "comment", "check-gateway", "scope"),
             used_by="WAN-Failover, Routen-Tabelle", must_have_rows=True,
             warn_if_missing={"active": "Feld 'active' fehlt – aktive Default-Route (Backup-Betrieb) nicht erkennbar"}),
    PathSpec("netwatch", "Netwatch", "/tool/netwatch/print", fields=("host", "status"),
             optional=("rtt-avg", "loss-percent", "comment", "type"), used_by="WAN-Health-Checks",
             warn_if_missing={"rtt-avg": "Keine Latenz aus Netwatch (ältere RouterOS-7-Version?) – Latenz-Alarme wirkungslos",
                              "loss-percent": "Kein Paketverlust aus Netwatch – Verlust-Anzeige leer"}),
    PathSpec("dhcp_client", "DHCP-Client", "/ip/dhcp-client/print", fields=("interface",), optional=("gateway", "status", "use-peer-dns"),
             used_by="WAN (gateway=dhcp), Content-Filter"),
    PathSpec("dns", "DNS", "/ip/dns/print", optional=("servers", "dynamic-servers", "use-doh-server", "allow-remote-requests"),
             used_by="Content-Filter", must_have_rows=True),
    PathSpec("fw_filter", "Firewall-Filter", "/ip/firewall/filter/print", fields=("chain", "action"),
             optional=("comment", "dynamic", "disabled"), used_by="Policies, Firewall-Ansicht"),
    PathSpec("fw_nat", "Firewall-NAT", "/ip/firewall/nat/print", fields=("chain", "action"),
             optional=("comment", "dynamic", "to-addresses", "to-ports"), used_by="Policies, WAN-Masquerade"),
    PathSpec("fw_address_list", "Address-Lists", "/ip/firewall/address-list/print", fields=("list", "address"),
             optional=("comment", "dynamic", "timeout"), used_by="Policies"),
    PathSpec("fw_connection", "Verbindungstabelle", "/ip/firewall/connection/print",
             optional=("protocol", "dst-address", "reply-dst-address", "connection-mark"),
             used_by="Verbindungs-Flush bei WAN-Ausfall (Skripte)",
             warn_if_missing={"reply-dst-address": "Feld 'reply-dst-address' fehlt – der Failover-Flush findet keine Verbindungen"}),
    PathSpec("package_update", "Paket-Update", "/system/package/update/print", fields=("installed-version",),
             optional=("channel", "latest-version", "status"), used_by="Firmware", must_have_rows=True,
             hints={"latest-version": "Erst nach einer Update-Prüfung befüllt (Firmware → Nach Updates suchen)"}),
    PathSpec("health", "Sensoren", "/system/health/print", optional=("name", "value", "type"), used_by="Übersicht (Temperatur/Spannung)",
             hints={"name": "Modell liefert keine Sensorwerte – Kacheln werden ausgeblendet"}),
    PathSpec("ip_service", "IP-Dienste", "/ip/service/print", fields=("name", "port"), optional=("disabled", "address"),
             used_by="API-Zugriff, Backup-Export (SSH), Fernzugriff", must_have_rows=True),
    PathSpec("user", "API-Benutzer", "/user/print", fields=("name", "group"), used_by="Selbsttest (Rechte)", must_have_rows=True),
    PathSpec("user_group", "Benutzergruppen", "/user/group/print", fields=("name", "policy"), used_by="Selbsttest (Rechte)", must_have_rows=True),
    PathSpec("ping", "Ping", "/ping", fields=("sent", "received"), optional=("time", "packet-loss", "host"),
             used_by="Leitungstest, VRRP-Gegenstelle", must_have_rows=True, params={"count": "1"}),
)

SPEC_BY_KEY = {s.key: s for s in PATH_SPECS}

# ----------------------------------------------------------------------------- Benutzergruppen
# Einzige Definition der Policies. Onboarding-Skript, „Rechte einschränken“, Fernzugriff und Selbsttest
# lesen diese Werte; nichts davon ist an anderer Stelle hart kodiert.
API_GROUP = "sdwan-api"
# Kern-Policies: ohne sie funktioniert ein Teil der Plattform nicht (Selbsttest rot)
API_CORE_POLICIES: dict[str, str] = {
    "api": "Zugriff der Plattform",
    "read": "alle Anzeigen",
    "write": "Konfiguration schreiben",
    "policy": "temporäre Fernzugriffs-Benutzer und Gruppen anlegen",
    "reboot": "Neustart und Firmware-Update",
    "test": "Ping (Leitungstest, VRRP-Gegenstelle)",
    "ssh": "Backup-Export",
}
# Empfohlen: fehlen sie, ist der Selbsttest orange
API_RECOMMENDED_POLICIES: dict[str, str] = {
    "sensitive": "ohne 'sensitive' fehlen Schlüssel/Passwörter im Export – das Backup ist für eine Wiederherstellung unvollständig",
    "winbox": "ohne 'winbox' kann die Gruppe für Fernzugriffs-Benutzer nicht angelegt werden – Fernzugriff funktioniert ohne diese Policies nicht",
    "web": "ohne 'web' kann die Gruppe für Fernzugriffs-Benutzer nicht angelegt werden – Fernzugriff funktioniert ohne diese Policies nicht",
}
API_POLICIES: tuple[str, ...] = ("read", "write", "api", "policy", "reboot", "test", "ssh", "sensitive", "winbox", "web")
assert set(API_POLICIES) == set(API_CORE_POLICIES) | set(API_RECOMMENDED_POLICIES)

# Gruppe der temporären Fernzugriffs-Benutzer: bewusst ohne 'policy' (keine Benutzerverwaltung) und ohne 'api'
REMOTE_GROUP = "sdwan-remote"
REMOTE_POLICIES: tuple[str, ...] = ("local", "ssh", "read", "write", "test", "winbox", "web", "reboot", "sensitive")


def policy_set(value: object) -> set[str]:
    """RouterOS-Policy-Liste (``read,write,!ftp,…``) → Menge der aktiven Policies."""
    return {p.strip() for p in str(value or "").split(",") if p.strip() and not p.strip().startswith("!")}


KNOWN_ARCHITECTURES = ("arm", "arm64", "mipsbe", "mmips", "smips", "tile", "ppc", "x86", "x86_64")
