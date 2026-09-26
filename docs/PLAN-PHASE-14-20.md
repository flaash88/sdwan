# Plan: Phasen 14–20 (Firewall-Editor, Threat-Feeds, Compliance/Suche, Scripts, Betrieb, WLAN, Hotspot)

## Context
Die Plattform soll vom Konfigurations-Werkzeug für Einzelfunktionen (WAN, VRRP, Mesh, Policies im Rohformat)
zum allgemeinen MSP-Werkzeug wachsen. Der Auftrag umfasst sieben Phasen. Arbeitsweise laut Auftrag:
- Dieser Plan wird als `docs/PLAN-PHASE-14-20.md` committet.
- Danach werden die Phasen ohne Rückfragen nacheinander umgesetzt, je Phase mit Tests grün,
  `npm run build` fehlerfrei, eigenem Commit und Push, und einem Abschnitt „Stand Phase X“.
- Entscheidungen stehen unten mit Begründung.
- Anhalten nur, wenn Tests nicht grün werden, bestehende Router-Konfigurationen oder ausgerollte Policies
  verändert würden, oder eine Migration Daten löscht.

Branch: `claude/mikrotik-sdwan-platform-qs2sti`. Migrationen ab 0020.

## Querschnitt (gilt für alle Phasen)
- **Seed-Daten statt Code:** `backend/app/seeds/*.json` (Dienste, Zonen, Bausteine, Feeds,
  MSP-Baseline, Portal-Vorlagen, Beispiel-Scripts).
  - `seeds.apply()` läuft beim Start (wie `bootstrap`) und legt globale Datensätze (`tenant_id NULL`,
    `builtin=True`, eindeutiger `seed_key`) idempotent an bzw. aktualisiert sie. Vom Benutzer geänderte
    oder kopierte Einträge werden nie überschrieben.
  - Builtins sind schreibgeschützt; „Kopieren“ erzeugt einen eigenen Eintrag.
  - Beispielwerte (IPs, VLANs) nur in Beschreibungen, markiert als „Beispiel“.
- **Global/mandantenweit:** Muster `GlobalOrTenantScoped` wie `FirewallPolicy`. Global anlegen darf nur der
  MSP-Admin, mandantenweit nur der Admin. RBAC über die bestehenden `ReadCtx/TechCtx/AdminCtx/SuperCtx`,
  Änderungen immer mit `ctx.audit`.
- **Ziele „Geräte/Standorte/Tags“:** neue gemeinsame Hilfe `services/targets.py`.
  - `resolve_targets(db, tenant, {device_ids, site_ids, tags})` liefert Geräte.
  - Nutzung ab Phase 16 (Compliance, Scripts, WLAN, Feeds). Policy-Zuweisungen bleiben unverändert (Gerät).
- **Verwaltete Objekte:** Kommentar-Präfixe `sdwan:zone:`, `sdwan:feed:`, `sdwan:syslog`, `sdwan:wifi:`,
  `sdwan:hs:`, `sdwan:speedtest`. Immer über `DeviceAPI.sync_managed` oder eigene Differenz-Logik.
  Nicht verwaltete Einträge werden nie angefasst.
- **RouterOS-Unsicherheit:** Neue Pfade kommen in `routeros/schema.py` `PATH_SPECS` (Selbsttest), der
  Simulator bekommt passende Handler, `docs/LABORTEST.md` einen Prüfschritt. Im Code mit
  `# ANNAHME (Labor):` markieren.
- **Frontend:** Designsystem aus `components/ui.tsx`; neue Seiten in `Layout`-Navigation und
  `deviceTabs.ts`. Tabs nur anzeigen, wenn es Daten gibt (WLAN, Log).
- **Je Phase:**
  - Doku: ARCHITECTURE-Abschnitt, README-Zeile, LABORTEST-Schritte, Plan-Abschnitt „Stand Phase X“.
  - Prüfung: `pytest` (alle), `npm run build`, dazu ein Screenshot-Check im Simulator.

## Phase 14 – Firewall-Editor (vereinfacht)
**Modelle (Migration 0020, nur neue Tabellen/Spalten):**
- `FirewallPolicy.mode` (`expert` | `simple`, Default `expert` → bestehende Policies unverändert).
- `FirewallPolicy.spec` (JSON, nur bei `simple`).
- `FwNetObject` (host/network/range/group, `members`), `FwService` (Protokoll + Ports, Gruppe),
  `FwZone` (Name, Slug, `source`: manual | wan), `FwBlock` (Baustein-Spec).
  Alle `GlobalOrTenantScoped`, mit `builtin`/`seed_key`.
- `DeviceZoneMember` (`device_id`, `zone_id`, `interface`), `TenantScoped`.

**Compiler `services/fw_compile.py`:** `compile_spec(spec, objects) -> content`, im bestehenden
Format `{address_lists, filter, nat}`. Danach läuft `validate_content()`. Push, Versionierung, Rollback und
`render()` bleiben unangetastet.
- **Netzobjekte** werden zur Address-List `sdwan-obj-<slug>`. Bereiche gehen als `a.b.c.d-e.f.g.h`
  (RouterOS erlaubt Ranges in Address-Lists, ANNAHME Labor).
- **Zonen** werden zu `in/out-interface-list=sdwan-zone-<slug>`. Die Zone „WAN“ mit `source=wan` nutzt die
  bestehende Liste `sdwan-wan`.
- **Regeln:**
  - `forward`, wenn eine Zielzone gesetzt ist; `input`, wenn das Ziel „Router selbst“ ist.
  - Dienste mit mehreren Protokollen erzeugen je Protokoll eine Regel.
  - Aktionen: erlauben → `accept`, verwerfen → `drop`, ablehnen → `reject`.
  - `log=yes` plus `log-prefix`.
- **NAT:** Masquerade je WAN-Zone (`srcnat out-interface-list`). Portweiterleitung wird zu `dstnat`,
  dazu eine Lint-Prüfung auf die passende Forward-Regel.
- **Grundregeln** (grau, nicht editierbar), in fester Reihenfolge:
  1. **Plattform-Zugänge**, immer und nicht abschaltbar:
     - Hub über das Management-Interface;
     - Mesh-WireGuard-Port;
     - VRRP-Protokoll;
     - ICMP.
  2. `established,related` accept.
  3. `invalid` drop.
  4. Management-Dienste (winbox/ssh/api/www) nur aus der Zone Management und dem Tunnel.
  5. Am Ende Default-Drop `input`/`forward` (Option `default_drop`, Default an, Abschalten mit Warnung).
- **Umwandlung:**
  - einfach → Experte: `content` übernehmen, `mode=expert`.
  - Experte → einfach: nur mit Warnung; nicht abbildbare Regeln bleiben als „Rohregeln“-Block erhalten.

**Zonen auf Geräten** (`services/zones.py`):
- `/interface/list` und `/interface/list/member` mit `sdwan:zone:`.
- Wird beim Speichern der Zuordnung im Gerätedetail (neuer Abschnitt im Firewall-Tab) und aus der
  ZTP-Vorlage (`zones: {slug: [interfaces]}`) angewendet.
- Beim Deploy einer einfachen Policy zusätzlich vorab `ensure_zones` für die Zielgeräte, damit die
  Listen existieren. Rein additiv in `run_deployment`, nur für `mode=simple`.

**Lint `services/fw_lint.py`** (Stufen info/warn/error):
- verdeckte Regeln (Teilmengen-Prüfung Quelle/Ziel/Dienst);
- any→any accept;
- doppelte Regeln;
- leere Objekte;
- Zonen ohne Interface auf zugewiesenen Geräten;
- Portweiterleitung ohne Forward-Regel;
- Default-Drop-Policy nicht zuletzt zugewiesen (Reihenfolge mehrerer Policies).

Errors blockieren das Deploy nicht automatisch, der Deploy-Dialog verlangt dann aber eine Bestätigung.
Weitere Prüfungen (Entscheidungen 17, 18, 20):
- Management-Zone ohne Interface auf einem Zielgerät → Lint-Fehler.
- Vor dem Deploy: Vorprüfung auf nicht verwaltete Regeln hinter dem Default-Drop. Betroffene Geräte
  werden ohne Bestätigung übersprungen.
- Hinweis „Änderungen nicht ausgerollt“ mit den betroffenen Geräten.

**Vorschau:** `POST /policies/{id}/preview` liefert `/ip firewall … add …`-Befehle aus dem kompilierten
`content` und den Diff zur zuletzt ausgerollten Version aus `PolicyVersion`, mit `make_diff` aus
`backup.py`.

**Trefferzähler:**
- Poll-Hook liest `bytes`/`packets` der `sdwan:fw:`-Regeln, alle 5 min statt bei jedem Poll.
- Er speichert `facts.fw_hits` samt `last_hit_at`-Historie in der Tabelle `FwRuleHit` (je Gerät,
  Kommentar-Schlüssel, Zähler, zuletzt > 0).
- „0 Treffer seit X Tagen“ entspricht `now - last_hit_at`.
- Reset per `POST /devices/{id}/firewall/reset-counters` (Techniker+, Audit) über
  `/ip/firewall/filter/reset-counters` mit `.id` (ANNAHME Labor).

**Bausteine (Seed):**
- Standard-Härtung;
- Gäste vom LAN isolieren;
- Nur Internet für Zone X;
- Zone nur zu definierten Zielen (Beispiele: Kassen, IoT, Drucker, Kameras);
- DNS erzwingen (dstnat 53 → Router).

Parameter wie Zone oder Objekt werden beim Einfügen per Auswahl gesetzt.

**UI:**
- `pages/Policies.tsx` bekommt einen Editor `components/fw/*`:
  - Regeltabelle mit HTML5-Drag & Drop plus Tasten-Buttons (hoch/runter, Alt+Pfeile);
  - Auswahllisten statt Freitext, Inline-Anlegen, Suche/Filter;
  - Bausteine-Menü, Lint-Leiste, Vorschau-Modal, Treffer-Spalte.
- Objekte/Dienste/Zonen als Tab „Objekte“.
- Expertenmodus = die bisherige Ansicht.

## Phase 15 – Threat-Feeds
- **Modelle (0021):**
  - `ThreatFeed` (`GlobalOrTenantScoped`): Name/Slug, URL, Kommentarzeichen, Intervall, `max_entries`,
    `enabled`, Status (`last_ok_at`, `count`, `error`).
  - `ThreatFeedEntry` (feed_id, net; letzte gültige Liste).
  - `ThreatFeedAssignment` mit Targets.
- **Seed:** Spamhaus DROP und EDROP (URLs als Daten).
- **Worker-Job** alle 5 min: fällige Feeds laden (httpx, Timeout, Größenlimit), validieren
  (`ipaddress`, nur Netze, keine privaten/reservierten Netze, Obergrenze), speichern.
  - Bei Fehler bleibt die letzte gültige Liste aktiv.
- **Verteilung:** je Gerät die Liste `sdwan-feed-<slug>` (Kommentar `sdwan:feed:<slug>`), nur
  Differenzen (add/remove einzeln).
- **RAM-Check** vor dem Verteilen: `free-memory` aus der Resource. Schätzung Einträge × 200 Byte plus
  Reserve (konfigurierbar, Default 32 MB). Reicht es nicht, wird das Gerät übersprungen, mit Warnung im
  Status.
- **Firewall-Editor:** Feed ist ein Netzobjekt-Typ `feed`, der auf `sdwan-feed-<slug>` verweist.
  Baustein „Threat-Feeds eingehend verwerfen“.
- **Alarmtyp `feed_stale`:** letzte erfolgreiche Aktualisierung älter als 3 × Intervall.
- **UI:** Seite „Threat-Feeds“ (Status, Einträge, Fehler, Zuweisung).

## Phase 16 – Compliance und Config-Suche
- **Modelle (0022):**
  - `ComplianceRuleSet` (`GlobalOrTenantScoped`, Regeln als JSON-Liste).
  - `ComplianceAssignment` (Targets).
  - `ComplianceResult` (Gerät, Set, Zeitpunkt, je Regel ok/fail/unknown, Backup-ID).
- **Regeltypen:**
  - Text gegen das letzte Backup: contains, not_contains, regex.
  - Strukturiert, geparst aus dem `/export terse`-Text, dazu Live-Werte aus `facts`: service_disabled,
    no_user, ntp_enabled, service_restricted_to_tunnel, channel_in, min_version.
- **Seed „MSP-Baseline“:** genau die genannten Prüfungen.
- **Auswertung:** nach jedem Backup (Hook am Ende von `take_backup`, best effort) und manuell.
- **Flottenbericht:**
  - Matrix Geräte × Regeln, CSV und PDF (reportlab wie `sla.py`), Trend (Anteil bestanden pro Tag).
  - Alarmtyp `compliance_failed`, in `DEFAULT_RULES` deaktiviert.
- **Config-Suche** `GET /config-search`:
  - Durchsucht die jeweils letzten Backups des Mandanten; MSP-Admin mit `all_tenants`.
  - Literal oder Regex, Regex max. 200 Zeichen, Auswertung im Thread mit Zeitlimit.
  - Kontextzeilen und Link aufs Gerät.
  - Geheimnisse werden maskiert (`password=`, `secret=`, `private-key=`, `preshared-key=`,
    `passphrase=`, `authentication-key=` …), auch bevor die Regex läuft; sie sind nie durchsuchbar.

## Phase 17 – Script-Bibliothek und Massen-Ausführung
- **Modelle (0023):**
  - `Script` (`GlobalOrTenantScoped`): Name, Beschreibung, Kategorie `read`/`change`, Version, Inhalt.
  - `ScriptVersion`.
  - `ScriptRun` (wie `FirmwareJob`: Batches, `max_failures`, Status) und `ScriptRunItem`
    (Gerät, gerenderter Text, Ausgabe, Status, Dauer).
- **Variablen:** nur eine Allowlist (`device.name/identity/tunnel_ip/serial/model`, `site.name`,
  `tenant.name/slug`). Eigener Ersetzer, kein Jinja. Unbekannte Variablen sind ein Fehler.
- **Ausführung:**
  - Über SSH (asyncssh wie `export_config`), Ausgabe erfasst, Timeout.
  - Im Simulator über einen `script`-Handler.
  - ANNAHME Labor: Mehrzeilige Scripts laufen per SSH wie im Terminal.
- **Ablauf:** Vorschau je Gerät → bei `change` Pflicht-Bestätigung durch Eintippen des Script-Namens →
  Backup (`pre-script`, neuer Auslöser) → Batches mit Abbruch bei Fehlern (Worker-Tick wie Firmware).
- **Rechte:** `read` ab Techniker, `change` nur Admin/MSP-Admin. Audit mit vollem Script-Text.
- **Warnung**, wenn der Script-Text `sdwan:` enthält (verwaltete Objekte).
- **Ausgaben** sind durchsuchbar (einfache ILIKE-Suche über `ScriptRunItem.output`).

## Phase 18 – Wartungsfenster, Speedtest, Syslog
- **Wartungsfenster (0024):** `MaintenanceWindow` (Mandant, optional Site/Gerät; einmalig oder
  wöchentlich mit Wochentagen, Start, Dauer; Zeitzone des Mandanten).
  - `alerts.evaluate`: Bedingungen innerhalb eines Fensters erzeugen keinen Alarm, als unterdrückt
    markiert (`Alert.suppressed_reason='maintenance'`, sichtbar).
  - Firmware-Job-Option `only_in_window`: Batches starten nur innerhalb eines Fensters.
- **Speedtest:**
  - `/tool/bandwidth-test` (ANNAHME Labor) gegen einen konfigurierbaren btest-Server
    (`SPEEDTEST_SERVER`, z. B. ein MSP-CHR). Ohne Konfiguration ist der Button deaktiviert, mit
    Erklärung.
  - **Je WAN:** vorübergehende /32-Route zum Server über das Gateway dieses WAN
    (Kommentar `sdwan:speedtest`). Sie wird danach entfernt, der Worker räumt Reste auf.
  - Vor dem Start: erwarteter Datenverbrauch (Dauer × letzte Rate bzw. Obergrenze) und Warnung bei
    WAN-Volumenlimit.
  - Ergebnisse als `SpeedtestResult` mit Verlauf im WAN-Tab. Geplant optional (wöchentlich), Standard aus.
- **Syslog:**
  - Neuer Container `syslog` mit `network_mode: service:wireguard-hub`. Er empfängt UDP 514 auf der
    Hub-Tunnel-IP und schreibt in die Tabelle `SyslogMessage` (Gerät über die Quell-IP = Tunnel-IP).
  - Im Test läuft der Empfänger als asyncio-Protokoll, direkt testbar.
  - **Pro Gerät/Mandant opt-in** (Standard aus). Die Plattform legt dann
    `/system/logging/action` `sdwan-syslog` (target=remote) und `/system/logging` für ausgewählte Topics
    an, jeweils mit `sdwan:syslog`.
  - Aufbewahrung je Mandant (Default 30 Tage), Lösch-Job.
  - Tab „Log“ mit Filter nach Zeit, Topic und Text. Absprung „Log um diesen Zeitpunkt“ aus
    Metriken/VRRP-Verlauf per Query `?around=`.

## Phase 19 – WLAN-Verwaltung
- **Erkennung** im Poll-Hook: `/interface/wifi/print` (Treiber `wifi`, RouterOS 7) oder
  `/interface/wireless/print` (Treiber `wireless`). Ergebnis `facts.wlan = {driver, capsman_role, radios}`.
  Fehlschläge gelten als „kein WLAN“.
- **Konfiguration nur für `wifi`** (lokal und CAPsMAN-Controller). `wireless` ist sichtbar gekennzeichnet
  „nur Anzeige, Konfiguration nicht unterstützt“. Es werden niemals Befehle an den falschen Treiber
  geschickt.
- **`WlanProfile` (0025):** SSID, Sicherheit (WPA2/WPA3-PSK, Enterprise mit RADIUS-Server/Secret
  verschlüsselt), Band, Kanalbreite, Ländercode (Default vom Mandanten), VLAN, Client-Isolation, versteckt,
  Zeitplan.
  - Zeitplan per verwaltetem `/system/scheduler` `sdwan:wifi:sched:<id>`.
  - Dazu `WlanAssignment` (Targets, bei CAPsMAN Controller + CAPs).
- **`Tenant.country_code`:** Default `AT` beim Anlegen, pro Profil überschreibbar.
- **Push:** `/interface/wifi/configuration`, `/security`, `/datapath` bzw. `/interface/wifi/provisioning`
  für CAPsMAN, jeweils mit `sdwan:wifi:`. Alle Pfade ANNAHME Labor.
- **Status:** `/interface/wifi/registration-table` (MAC, Signal, Rate, SSID, AP) bzw. `wireless`
  analog nur lesend. Nachbarnetze/Kanäle nur, soweit ohne Scan-Unterbrechung lesbar; ein aktiver Scan
  wird nicht ausgelöst.
- **Gäste-PSK rotieren:** neues PSK, Push, QR-Code mit `WIFI:T:WPA;S:..;P:..;;` und Druckansicht.
  QR-Erzeugung im Backend mit `segno` (reine Python-Bibliothek) als SVG.
- **Tab „WLAN“** nur, wenn `facts.wlan` Radios hat.

## Phase 20 – Hotspot / Gäste-Portal
- **Modelle (0026):**
  - `HotspotPortal` (`GlobalOrTenantScoped`-Vorlagen + mandantenweite Portale): Design, Texte DE/EN,
    AGB, Anmeldeart (voucher/click/form), Formularfelder.
  - `HotspotInstance` (Gerät, Interface/VLAN, Portal, Walled Garden, Session-Timeout).
  - `VoucherProfile`, `Voucher` (Code, Status, Profil, Batch).
  - `GuestSession` (minimale Daten, Aufbewahrung).
- **Seed-Portalvorlagen:** Hotel, Gastronomie, Veranstaltung, Büro-Gäste.
- **Router:**
  - `/ip/hotspot`, `/ip/hotspot/profile` (html-directory `sdwan-hs-<id>`), `/ip/hotspot/user/profile`
    (rate-limit, shared-users), `/ip/hotspot/user` (Voucher mit limit-uptime/limit-bytes-total),
    `/ip/hotspot/walled-garden`, alle mit `sdwan:hs:`.
  - Login-Seiten werden aus Vorlagen erzeugt (`login.html`, `alogin.html` …) und per SFTP (asyncssh)
    hochgeladen.
  - Klick-Durchgang über den Hotspot-Trial-Login.
  - Formular: Die Feldwerte gehen vom Gast-Browser an `POST /api/v1/portal/{id}/register`; die
    Plattform-Domain wird automatisch in den Walled Garden aufgenommen. Der Endpunkt hat Rate-Limit,
    Größenlimit und nimmt nur definierte Felder an (Entscheidung 19).
  - Alle Pfade und Upload-Ziele: ANNAHME Labor.
- **Unabhängig vom AP-Hersteller** (Doku): Der Hotspot hängt am Interface/VLAN des MikroTik.
- **Voucher:** Stapel erzeugen, Druckansicht A4-Karten mit QR (Login-URL + Code), CSV, Status aus
  `/ip/hotspot/user` (uptime/bytes) bzw. Ablauf, Sperren = `disabled=yes`.
- **Live:** `/ip/hotspot/active` (Volumen, trennen = remove active; sperren = Voucher deaktivieren).
- **DSGVO:**
  - Aufbewahrung `Tenant.guest_retention_days` (Default 30), Lösch-Job.
  - Gespeichert werden nur die Formularfelder des Portals und Sitzungsmetadaten, keine Browserdaten.
  - Hinweis im Designer: Der Betreiber verantwortet Nutzungsbedingungen und Datenschutz.
- **Firewall-Editor:** Hat ein Gerät einen Hotspot, schlägt der Editor den Baustein „Gäste isolieren“
  für die Gäste-Zone vor (Hinweis in der Lint-Leiste).

## Entscheidungen
1. **Kompilieren in das bestehende Format:** Push, Rollback, Versionen und `render()` unverändert.
   Bestehende Policies werden `mode=expert` (Migration mit Default, keine Datenänderung).
2. **Plattform-Zugänge** (Hub/Tunnel, Mesh-Port, VRRP, ICMP) erzeugt der Compiler immer vor jedem Drop,
   nicht abschaltbar. Grund: Ein Default-Drop darf die Plattform, das Mesh oder VRRP nie aussperren. Die
   Reihenfolge verwalteter Regeln verschiedener Tags ist nicht garantiert.
3. **Default-Drop** standardmäßig an (wie gefordert). Lint warnt, wenn eine Policy mit Default-Drop auf
   einem Gerät nicht als letzte zugewiesen ist.
4. **Zonen-Listen** werden beim Speichern der Zuordnung bzw. beim Deploy einfacher Policies gesetzt.
   Zone „WAN“ nutzt die bestehende `sdwan-wan`-Liste statt einer zweiten.
5. **Objekte ändern** erzeugt für referenzierende einfache Policies eine neue Version (neu kompiliert),
   aber kein automatisches Deploy. Grund: kein ungewollter Push.
6. **Trefferzähler** alle 5 min statt bei jedem Poll. Grund: Last bei großen Regelwerken.
7. **Threat-Feeds:** letzte gültige Liste bleibt bei Ladefehlern aktiv. Private/reservierte Netze werden
   verworfen (Schutz vor Selbstaussperrung).
8. **Config-Suche:** Geheimnisse werden vor der Suche maskiert und sind nie Suchtreffer. Regex mit Länge-
   und Zeitlimit (ReDoS).
9. **Scripts per SSH** statt API: Ausgabe erfassbar, wie Terminal. Nur Variablen-Allowlist, kein
   Template-Motor. `change` nur Admin.
10. **Speedtest** braucht einen externen btest-Server. Der Hub ist Linux und kein btest-Server, daher kein
    eingebauter Server. Ohne Konfiguration deaktiviert. Die temporäre /32-Route je WAN wird immer
    entfernt.
11. **Syslog und Speedtest-Planung** sind opt-in (Standard aus). Grund: keine Änderung bestehender
    Router-Konfiguration ohne Auftrag.
12. **WLAN** konfiguriert nur den `wifi`-Treiber. `wireless` bleibt nur lesend. Kein aktiver Scan
    (würde Clients trennen).
13. **Hotspot-Formulardaten** werden an die Plattform gesendet, weil der Router sie nicht speichern kann.
    Nur definierte Felder, Aufbewahrung je Mandant.
14. **QR-Codes** im Backend (`segno`, reine Python-Bibliothek) statt neuer Frontend-Abhängigkeit.
15. **Seeds als JSON** mit `seed_key`, idempotent, Builtins schreibgeschützt (Kopieren statt Ändern).
17. **Default-Drop vs. nicht verwaltete Regeln:**
    - **Vor dem Deploy prüfen:** Vor jedem Deploy einer einfachen Policy mit Default-Drop liest die
      Plattform die nicht verwalteten Filterregeln der Zielgeräte (`/ip/firewall/filter`, Kommentar ohne
      `sdwan:`, nicht dynamisch).
    - **Einfügeposition:** Verwaltete Regeln kommen per `place_first` vor die erste nicht verwaltete
      Regel, also **oben**. Nicht verwaltete Regeln der betroffenen Chains (`input`/`forward`) landen
      damit hinter dem Default-Drop und greifen nie mehr.
    - **Anzeige:** Vorschau und Deploy-Dialog listen das pro Gerät auf („diese X Regeln würden nie mehr
      greifen“, mit Chain/Action/Kommentar).
    - **Standard:** Diese Geräte werden **übersprungen** (Ergebnis `skipped_unmanaged_rules`). Nur mit
      ausdrücklicher Bestätigung je Gerät (`confirm_devices` im Deploy-Aufruf) wird ausgerollt.
    - Umsetzung als Vorprüfung in einem neuen `services/fw_deploy_check.py`; `run_deployment` bekommt
      nur eine additive Geräteausnahme.
    - **Editor:** zeigt die Einfügeposition sichtbar an („Verwaltete Regeln stehen oben, vor allen
      manuellen Regeln“). ARCHITECTURE dokumentiert sie.
    - **Test:** Gerät mit manueller Regel → Deploy ohne Bestätigung überspringt das Gerät, die manuelle
      Regel und das gesamte Regelwerk bleiben unverändert.
    - Grund: Bestehende Router-Konfigurationen dürfen nicht stillschweigend außer Kraft gesetzt werden.
18. **Management-Zugriff:** Hat ein Zielgerät der Zone „Management“ kein Interface zugeordnet, meldet Lint
    einen Fehler: „Lokaler Zugriff (WinBox/SSH im LAN) nach dem Deploy nicht mehr möglich – nur noch über
    den Tunnel“. Der Deploy-Dialog verlangt dann eine Bestätigung. Der Tunnelzugang bleibt durch die
    Plattform-Zugänge (Entscheidung 2) immer erhalten.
19. **Hotspot-Registrierung** `POST /api/v1/portal/{id}/register` (öffentlich):
    - Rate-Limit je Quell-IP und Portal (Redis-Zähler, In-Memory-Fallback), Body-Größenlimit (z. B. 4 KB).
    - Nur die im Portal definierten Felder werden angenommen, Typ- und Längenprüfung; unbekannte Felder
      werden verworfen.
    - ARCHITECTURE dokumentiert: Der Walled Garden kann bei HTTPS nur nach Host freigeben, Gäste erreichen
      damit auch die Plattform-Oberfläche (Login-geschützt). Deshalb nur die öffentlichen Portal-Endpunkte
      ohne Anmeldung.
20. **Nicht ausgerollte Änderungen:** Liste und Editor zeigen bei einfachen Policies deutlich
    „Änderungen nicht ausgerollt“, wenn `version > deployed_version` einer Zuweisung ist, samt betroffenen
    Geräten.
21. **Umfangsgrenzen:** Was sich ohne Labor nicht sicher umsetzen lässt, bleibt als „im Labor zu
    verifizieren“ markiert statt geraten. Beispiele: CAPsMAN-Provisioning-Details, Hotspot-Upload-Pfad,
    reset-counters.

## Wiederverwendung
- `DeviceAPI.sync_managed`, `connect_device`: `routeros/client.py`
- `validate_content`, `render`, `run_deployment`, `snapshot/restore`: `services/policy.py`
- `make_diff`, `take_backup`, `export_config` (SSH): `services/backup.py`
- Batch-/Tick-Muster: `services/firmware.py`
- Alarmtypen/`DEFAULT_RULES`: `services/alerts.py`
- PDF: `services/sla.py`
- Poll-Hooks: `services/registry.py`
- `PATH_SPECS`: `routeros/schema.py`
- Simulator-Tabellen/Handler: `routeros/simulator.py`
- UI-Bausteine: `components/ui.tsx`, `components/fleet.tsx`

## Verifikation
- Pro Phase neue Testdatei(en) (`tests/test_phase14_fw_editor.py` …).
- Compiler/Lint als reine Unit-Tests.
- Push-Pfade gegen den Simulator, dazu RBAC, Audit und Isolation zwischen Mandanten.
- Regressionstest: Bestehende Policy-Tests bleiben unverändert grün, bestehende Policies deployen
  identisch (Snapshot-Vergleich von `render()` vor/nach Migration).
- `npm run build`; Screenshots der neuen Seiten hell/dunkel im Simulator.
- Selbsttest im Simulator bleibt grün mit allen neuen `PATH_SPECS`.

## Stand der Phasen
(wird nach jeder Phase ergänzt)
