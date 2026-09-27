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
    "logging_action": re.compile(r"^[A-Za-z0-9]+\Z"),  # auf Hardware bestätigt
    "generic": re.compile(r"^[A-Za-z0-9._-]+\Z"),  # ANNAHME (Labor)
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


# --------------------------------------------------------------------------- Freitext in RouterOS-Scripts (AUDIT-001/002/051)
# Zeichen mit Bedeutung in der RouterOS-Scriptsprache: Anführungszeichen, Escape, Variablen, Befehls-Substitution,
# Blöcke und Befehlstrenner. In Namen (Gerät, WAN-Leitung, …) nie nötig – werden abgelehnt.
_LABEL_FORBIDDEN = re.compile(r'[\x00-\x1f\x7f"\\$\[\]{};`]')
_COMMENT_UNSAFE = re.compile(r"[^A-Za-z0-9 ._:/+=@,()-]")


def validate_label(value: str, what: str = "Name", max_len: int = 64) -> str:
    """Freitext-Namen, die (auch) in RouterOS-Scripts landen: 1..max_len Zeichen, keine Steuerzeichen (Zeilenumbruch)
    und keine Script-Sonderzeichen ``" \\ $ [ ] { } ; ` ``. Löst ``ValueError`` aus."""
    bad = _LABEL_FORBIDDEN.search(str(value))  # vor dem Trimmen: auch ein abschließender Zeilenumbruch ist unzulässig
    v = str(value).strip()
    if not v or len(v) > max_len:
        raise ValueError(f"{what}: 1 bis {max_len} Zeichen")
    if bad:
        ch = bad.group(0)
        shown = repr(ch) if ch.isprintable() else "Steuerzeichen/Zeilenumbruch"
        raise ValueError(f"{what}: Zeichen {shown} nicht erlaubt (Sonderzeichen der RouterOS-Scriptsprache)")
    return v


def routeros_str(value: str) -> str:
    """RouterOS-String in Anführungszeichen mit maskierten ``\\``, ``"`` und ``$``. Steuerzeichen werden abgelehnt
    (ein Zeilenumbruch beendet sonst den Befehl)."""
    v = str(value)
    if re.search(r"[\x00-\x1f\x7f]", v):
        raise ValueError(f"Steuerzeichen in RouterOS-String nicht erlaubt: {v!r}")
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


def script_comment(value: str, max_len: int = 80) -> str:
    """Text für eine ``#``-Kommentarzeile bzw. ``:log``-Meldung: nur unkritische Zeichen, einzeilig, gekürzt."""
    return _COMMENT_UNSAFE.sub("-", str(value))[:max_len]
