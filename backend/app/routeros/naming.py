"""Namen, die die Plattform auf Routern anlegt – zentrale Regeln und ``routeros_safe_name()``.

RouterOS prüft Namen je Menü unterschiedlich streng:

* ``/system/logging/action``: nur Buchstaben und Ziffern – **auf Hardware bestätigt** („action name can contain only
  letters and numbers“, RB751G-2HnD, RouterOS 7).
* alle übrigen von der Plattform erzeugten Namen (Interface-Listen, Scheduler, Scripts, Benutzer/Gruppen,
  WireGuard/VRRP-Interfaces, WLAN-Konfiguration/-Kanal/-Datapath/-Security, Hotspot-Server/-Profile/html-directory,
  IP-Pools, DHCP-Server, Address-Lists, Routing-Tabellen): **ANNAHME (Labor)** Buchstaben, Ziffern, ``-``, ``_``, ``.``.
  Die Plattform erzeugt dort nur ``sdwan-…`` aus Slugs (``[a-z0-9-]``); LABORTEST 3a prüft die Namen auf Hardware.

``routeros_safe_name()`` lässt gültige Namen unverändert (bestehende Objekte auf Routern behalten ihren Namen) und
bereinigt nur ungültige Zeichen. Gekürzt wird nicht: Längengrenzen sind unbekannt (ANNAHME), eine Kürzung würde
bestehende Namen ändern.

Der Simulator bildet dieselben Regeln nach (``SIM_NAME_RULES``), damit Tests abgelehnte Namen erkennen.
"""

from __future__ import annotations

import re
import unicodedata

# Art -> erlaubtes Muster
RULES: dict[str, re.Pattern[str]] = {
    "logging_action": re.compile(r"^[A-Za-z0-9]+$"),  # auf Hardware bestätigt
    "generic": re.compile(r"^[A-Za-z0-9._-]+$"),  # ANNAHME (Labor)
}
_INVALID = {"logging_action": re.compile(r"[^A-Za-z0-9]+"), "generic": re.compile(r"[^A-Za-z0-9._-]+")}

# Simulator: Menü -> (Feld mit dem Namen, Art, Fehlermeldung wie RouterOS)
SIM_NAME_RULES: dict[str, tuple[str, str, str]] = {
    "/system/logging/action": ("name", "logging_action", "action name can contain only letters and numbers"),
    **{p: ("name", "generic", "invalid name (ANNAHME Simulator: nur Buchstaben, Ziffern, - _ .)") for p in (
        "/interface/list", "/system/scheduler", "/system/script", "/user/group", "/interface/wireguard", "/interface/vrrp",
        "/interface/wifi/configuration", "/interface/wifi/channel", "/interface/wifi/datapath", "/interface/wifi/security",
        "/ip/hotspot", "/ip/hotspot/profile", "/ip/hotspot/user/profile", "/ip/pool", "/ip/dhcp-server", "/routing/table")},
    "/ip/firewall/address-list": ("list", "generic", "invalid list name (ANNAHME Simulator)"),
    "/ipv6/firewall/address-list": ("list", "generic", "invalid list name (ANNAHME Simulator)"),
}


def is_valid(name: str, kind: str = "generic") -> bool:
    return bool(RULES[kind].match(str(name)))


def routeros_safe_name(name: str, kind: str = "generic") -> str:
    """Name für ein RouterOS-Menü: gültige Namen unverändert; sonst Umlaute umschreiben, ungültige Zeichen entfernen
    (``logging_action``) bzw. durch ``-`` ersetzen (``generic``)."""
    s = str(name)
    if is_valid(s, kind):
        return s
    s = s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue").replace("ß", "ss")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = _INVALID[kind].sub("" if kind == "logging_action" else "-", s).strip("-")
    if not s:
        raise ValueError(f"Kein gültiger RouterOS-Name aus {name!r}")
    return s
