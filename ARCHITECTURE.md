# Architektur & Design-Entscheidungen

Dieses Dokument beschreibt den Aufbau der Plattform und hält alle Design-Entscheidungen
fest, die nicht eindeutig aus den Anforderungen hervorgingen. Jede Phase ergänzt einen
eigenen Abschnitt.

## Überblick

```
                    ┌──────────────────────── Cloud (Docker Compose) ────────────────────────┐
  Techniker ──HTTPS──► frontend (nginx, React) ──/api──► api (FastAPI) ◄──Redis Pub/Sub── worker
                    │                                     │  │                          │ (APScheduler)
                    │                          PostgreSQL ◄┘  └► InfluxDB ◄─ Grafana    │
                    │                                     │                              │
                    │                          route 10.100.0.0/16 via 172.30.0.10       │
                    │                                     ▼                              │
                    │                           wireguard-hub (wg0 10.100.0.1) ◄─────────┘
                    └──────────────────────────────────────┬────────────────────────────────┘
                                                           │ UDP 51820 (Router verbindet sich AUSGEHEND)
                         ┌─────────────────────────────────┼───────────────────────────┐
                    MikroTik Site A (sdwan-mgmt 10.100.0.2)   MikroTik Site B (10.100.0.3)  …
                         └──────── sdwan-mesh (10.200.x.0/24, Site-to-Site) ───────────┘
```

| Komponente       | Aufgabe |
|------------------|---------|
| `backend/` (api) | REST + WebSocket, Auth, RBAC, Mandanten, Pairing, Konfig-Push |
| `backend/` (worker) | periodische Jobs: Polling, Metriken, Backups, Alerts, Firmware-Queue, Reports |
| `hub/`           | WireGuard-Hub, synchronisiert Peers aus der API, meldet Handshakes |
| `frontend/`      | React/TypeScript/Tailwind Dashboard |
| PostgreSQL       | Stammdaten, Policies, Backups (Text + Diff), Audit, Alerts |
| Redis            | Event-Bus (Worker → API → WebSocket) |
| InfluxDB/Grafana | Zeitreihen (nicht in Postgres) |

## Phase 1 – Fundament

### Mandanten-Isolation (DB-Ebene)
* Jede mandantenbezogene Tabelle erbt `TenantScoped` (`tenant_id NOT NULL`, FK, Index).
* Ein SQLAlchemy-`do_orm_execute`-Hook hängt an **jede** ORM-SELECT/UPDATE/DELETE-Query
  automatisch `WHERE tenant_id = :aktiver_tenant` (`with_loader_criteria`). Ein vergessener
  Filter im Endpoint führt damit nicht zu einem Datenleck. Das gilt auch für `session.get()`.
* Ein `before_flush`-Hook blockiert Schreibzugriffe auf Objekte eines anderen Tenants
  (`TenantIsolationError` → HTTP 403) und setzt `tenant_id` bei neuen Objekten automatisch.
* `GlobalOrTenantScoped` (z. B. Firewall-Policies) erlaubt zusätzlich `tenant_id IS NULL`
  (globale MSP-Objekte) – lesbar für Tenants, aber nicht änderbar.
* Mandantenübergreifende Abfragen (Worker, Pairing per Token, IP-Vergabe) müssen explizit
  `system_session()` oder `execution_options(skip_tenant_filter=True)` verwenden.
* **Entscheidung:** Kein PostgreSQL-Row-Level-Security zusätzlich, weil Worker und API
  denselben DB-User nutzen; die ORM-Hooks decken jede Query ab und sind mit SQLite testbar.

### Benutzer & RBAC
* Rollen `admin` > `technician` > `readonly`. MSP-Mitarbeiter sind `is_superuser` ohne
  `tenant_id` und wählen den aktiven Mandanten per Header `X-Tenant-ID` (UI: Dropdown).
  Ohne gewählten Mandanten sehen sie alle Mandanten (read) – schreibende Aktionen, die einen
  Mandanten brauchen (Gerät anlegen), verlangen die Auswahl.
* JWT (HS256), Laufzeit 12 h. **Entscheidung:** Kein Refresh-Token – Self-Hosting-Szenario,
  Re-Login nach 12 h akzeptabel; kann später ergänzt werden.
* Erster MSP-Admin wird beim Start aus `BOOTSTRAP_ADMIN_*` angelegt, falls keine Benutzer existieren.

### Pairing & Schlüssel
* **Entscheidung (Sicherheit):** Der WireGuard-*Private-Key* des Management-Interfaces wird
  **auf dem Router** erzeugt (RouterOS generiert ihn beim Anlegen des Interfaces). Nur der
  Public-Key wird an die Cloud gemeldet. Jeder Router hat damit ein eigenes Keypair; die
  Control-Plane speichert keine Router-Private-Keys. Was die Control-Plane generiert und pusht:
  Tunnel-IP, Peer-Konfiguration, Allowed-IPs, pro Mesh-Verbindung einen Preshared-Key (Phase 2)
  und das zufällige API-Passwort.
* Pairing-Token: 192 bit Zufall, nur SHA-256-Hash in der DB, einmalig verwendbar, Standard-TTL 72 h.
* Ein Befehl im Router-Terminal: `/tool fetch url=".../onboard/<token>.rsc"; /import …`.
  Das Script registriert den Public-Key per `POST /api/v1/pair`, die Antwort ist wiederum ein
  RSC-Script (Tunnel-IP, Hub-Peer mit `persistent-keepalive=25s`, API-User, Firewall-Regel).
* Der API-User `sdwan` ist nur von der Hub-Adresse (`10.100.0.1/32`) erlaubt, der API-Dienst
  wird auf diese Adresse beschränkt. Das Passwort (24 Zeichen) liegt Fernet-verschlüsselt in der DB.
* **Entscheidung:** RouterOS-API unverschlüsselt (Port 8728) – die Verbindung läuft
  ausschließlich im WireGuard-Tunnel. Kein Zertifikatsmanagement für API-SSL nötig.

### WireGuard-Hub & Routing
* Ein gemeinsamer Hub für alle Mandanten, Management-Netz `10.100.0.0/16` (≈65k Geräte),
  Hub = `.0.1`, Tunnel-IPs werden global fortlaufend vergeben.
* Hub-Agent (Python, nur Stdlib) erzeugt seinen Schlüssel selbst (Volume `/data`), registriert den
  Public-Key bei der API und synchronisiert alle 10 s die Peers per `wg syncconf` (nur gepairte,
  nicht gesperrte Geräte; Allowed-IPs = exakt die `/32` des Geräts). Fallback auf `wireguard-go`,
  falls das Kernel-Modul fehlt (z. B. unprivilegierter LXC).
* API/Worker bekommen per Entrypoint die Route `10.100.0.0/16 via <hub>` (daher `NET_ADMIN`).
  Der Hub maskiert Docker-Traffic auf seine Tunnel-IP, sodass Router nur `10.100.0.1` kennen müssen.
* Hub-Firewall: kein Router→Router-Verkehr über den Hub, keine neuen Verbindungen vom Router
  in das Docker-Netz (nur ESTABLISHED/RELATED).
* `connect_device()` verweigert jede Zieladresse außerhalb von `WG_NETWORK` – API-Calls über
  das öffentliche Internet sind technisch ausgeschlossen.

### Online-Status
* Worker pollt alle 60 s jedes gepairte Gerät (`/system/resource`). Erfolg → `online`;
  Fehlschlag länger als `OFFLINE_AFTER_SECONDS` (180 s) → `offline`.
* Zusätzlich meldet der Hub die letzten WireGuard-Handshakes (Diagnose: Tunnel steht, API nicht).
* Statuswechsel werden über Redis Pub/Sub an alle WebSocket-Clients des Mandanten verteilt.

### Simulator
* `ROUTEROS_BACKEND=simulator` ersetzt librouteros durch einen In-Memory-RouterOS
  (`app/routeros/simulator.py`). Damit ist `docker compose up` ohne Hardware voll bedienbar
  (Button „Pairing simulieren“) und die Test-Suite deckt die Push-Logik ab.
* Für echte Router: `ROUTEROS_BACKEND=api`.

### Migrationen
* Alembic (`backend/alembic`), je Phase eine Revision. Der API-Container führt beim Start
  `alembic upgrade head` aus. Tests nutzen `create_all` (SQLite oder `TEST_DATABASE_URL`).

### Idempotente Konfiguration auf RouterOS
* Alle von der Plattform verwalteten Einträge tragen einen Kommentar mit Präfix `sdwan:`.
  `DeviceAPI.sync_managed()` gleicht verwaltete Einträge mit dem Sollzustand ab (add/set/remove)
  und lässt manuell gepflegte Einträge unangetastet. Das ist die Grundlage aller Pushes.

## Phase 2 – VPN-Mesh

* **Zweites WireGuard-Interface** `sdwan-mesh` (UDP 13232) je Gerät, getrennt vom Management-
  Tunnel. Private-Key wird wieder auf dem Router erzeugt, die Control-Plane liest den Public-Key
  per API aus. Pro Verbindung erzeugt die Control-Plane einen **Preshared-Key** (Fernet-verschlüsselt
  in `vpn_peers.psk_enc`) – zusätzliche Post-Quantum-Absicherung und kein geteiltes Secret.
* **Transfernetz:** pro Tenant ein `/24` aus `MESH_NETWORK` (10.200.0.0/16 → 256 Tenants),
  Mesh-IPs werden stabil pro Gerät vergeben (Hub bekommt die erste Adresse).
* **Teilnehmer:** ein gepairtes Gerät pro Standort (alphabetisch erstes). **Entscheidung:** HA-Paare
  pro Standort (VRRP) sind nicht Teil dieses Scopes; weitere Geräte erzeugen eine Warnung.
* **Hub-and-Spoke (Standard):** Spokes verbinden sich aktiv zum Hub (Endpoint + Keepalive 25 s),
  der Hub braucht deshalb eine erreichbare Adresse. Spoke-Allowed-IPs = Transfernetz + alle LANs
  der anderen Standorte → Spoke-zu-Spoke läuft über den Hub. Hub-Allowed-IPs = Mesh-IP + LANs des Spokes.
* **Full-Mesh:** jede Seite mit bekanntem Endpoint des Gegenübers initiiert. Sind beide Seiten hinter
  NAT/CGNAT ohne Endpoint, wird die Verbindung konfiguriert, aber als `no_endpoint` markiert.
  **Entscheidung:** kein Relay über den Cloud-Hub (würde Kunden-Nutzdaten durch die MSP-Cloud leiten).
* **Endpoint-Ermittlung:** `devices.mesh_endpoint` (manuell) oder automatisch die öffentliche
  Quell-IP, die der Management-Hub beim Handshake sieht (nur wenn global routbar).
* **Routen:** WireGuard auf RouterOS legt keine Routen an → für jedes entfernte LAN wird
  `dst=<LAN> gateway=sdwan-mesh` gesetzt. Firewall: Input-Accept für UDP 13232, Forward-Accept
  in/out `sdwan-mesh` (vor den Default-Drop-Regeln). Feinere Einschränkungen über Phase-5-Policies.
* **Push-Ablauf:** (1) Interface + Adresse auf allen Teilnehmern sicherstellen, Public-Keys einsammeln,
  (2) Peers/Routen/Firewall idempotent über `sync_managed` pushen. Ausgeschiedene Geräte bekommen
  die Mesh-Konfiguration entfernt. Ergebnis pro Gerät wird im Tenant gespeichert und auditiert.
* **Automatik:** Worker-Job alle 5 min berechnet einen Fingerprint (Topologie, Hub, Geräte, LANs,
  Endpoints) und wendet das Mesh nur bei Änderungen an (abschaltbar pro Tenant).
* **Status:** Poll-Hook liest `last-handshake`/rx/tx der Mesh-Peers; ein Tunnel gilt als `up`, wenn
  der letzte Handshake ≤ 180 s zurückliegt (WireGuard rekeyt alle 120 s). Wechsel werden live gepusht.

## Phase 3 – WAN Failover & Load-Balancing

* **Health-Checks laufen auf dem Router** (`/tool netwatch`, ICMP oder HTTP-GET), nicht in der Cloud.
  Grund: Fällt der primäre WAN aus, ist meist auch die Cloud-Verbindung kurz weg – Failover darf
  davon nicht abhängen. Die Cloud konfiguriert, beobachtet (Poll-Hook) und alarmiert.
* **Check-Routen:** Pro WAN eine Host-Route `<check_target>/32 via <gateway>`; dadurch laufen
  die Probes immer über genau diesen WAN. Deshalb muss jedes Check-Ziel pro Gerät eindeutig sein
  (validiert). Vorbelegt: 1.1.1.1, 9.9.9.9, 8.8.4.4, 208.67.222.222.
* **Umschalten:** Netwatch-`down-script` deaktiviert alle Default-Routen des WANs
  (`comment~"^sdwan:wan:default:<slot>"`, main-Table und PCC-Tabellen). Das `up-script` wartet die
  **Recovery-Verzögerung** ab und aktiviert nur, wenn der Link dann noch `up` ist (Hysterese).
* **Modi:**
  * `failover` – Default-Routen mit `distance = priority`.
  * `loadbalance_ecmp` – gleiche Distanz, RouterOS 7 verteilt per ECMP; ausgefallene Links werden deaktiviert.
  * `loadbalance_pcc` – Routing-Tabellen `sdwan-wan<slot>` (eigener WAN + übrige als Fallback),
    Mangle mit `per-connection-classifier`, gewichtet über mehrere PCC-Slots. Eingehende
    Verbindungen werden markiert und antworten über denselben WAN. Private Ziele
    (RFC1918/CGNAT, Adressliste `sdwan-private`) werden nie markiert, damit LAN-/Mesh-Verkehr in
    der main-Table bleibt. Optional werden bei Ausfall die Verbindungen des WANs gelöscht
    (`flush_connections`), damit sie neu verteilt werden.
* **Gateways:** IP, Interface-Name (PPPoE/LTE) oder `dhcp`. Bei `dhcp` wird das Gateway beim Push aus
  `/ip dhcp-client` gelesen; ändert es sich, erkennt der Poll-Hook das und pusht automatisch neu.
* Eine NAT-Masquerade-Regel für die Interface-Liste `sdwan-wan` wird angelegt (vor manuellen Regeln).
* **Status:** `up` / `down` / `degraded` (Latenz über Schwelle) / `disabled`; `active` = die
  Default-Route dieses WANs ist aktiv. Statuswechsel gehen live an das Dashboard (Basis für Alerts).
* Slot (1–4) ist stabil pro Link und bestimmt Tabellen-/Kommentarnamen; Priorität ist davon unabhängig.

## Phase 4 – Monitoring & Metriken

* **Erfassung im bestehenden Poll-Zyklus** (Standard 60 s): Poll-Hook liest Interface-Zähler,
  Raten werden aus der Differenz zum letzten Poll berechnet (Zählerstand in `devices.facts._counters`,
  Zähler-Reset nach Reboot wird verworfen). CPU/RAM/Uptime aus `/system/resource`, WAN-Latenz/-Verlust
  aus Netwatch (Phase 3), Mesh-Handshake/Traffic (Phase 2).
* **Latenz Cloud↔Router** = Round-Trip des API-Calls `/system/resource/print` durch den Tunnel –
  kostet keinen zusätzlichen Ping und misst genau den Management-Pfad.
* **InfluxDB 2** (Bucket `metrics`, 90 Tage Retention), Measurements `system`, `interface`, `wan`, `mesh`
  mit Tags `tenant_id`, `tenant`, `site_id`, `site`, `device_id`, `device`. Keine Metriken in Postgres.
* **Live-Kacheln:** Nach jedem Poll wird `device.metrics` über Redis → WebSocket verteilt.
  **Live-Modus:** Öffnet ein Benutzer den Metriken-Tab, setzt das Frontend alle 60 s
  `POST /devices/{id}/metrics/live` (Redis-Key mit 90 s TTL); ein Worker-Job pollt diese Geräte alle 5 s.
  So entstehen Echtzeit-Kacheln ohne die ganze Flotte hochfrequent abzufragen.
* **History-API** `GET /devices/{id}/metrics?measurement=&range=` – Tenant-Prüfung über das Device,
  Flux-Query nur mit Whitelist-Measurement und UUID (keine Injection). Frontend zeichnet mit eigener
  SVG-Chart-Komponente (keine Chart-Library).
* **Grafana:** Datasource und drei Dashboards (Mandant / Standort / Gerät, Template-Variablen auf
  `tenant_id`, `site_id`, `device_id`) werden provisioniert (`deploy/grafana`, Generator-Script).
  **Entscheidung:** Grafana selbst ist nicht mandantenfähig isoliert → nur für MSP-Admins verlinkt
  (unter `/grafana`, eigener Login). Tenant-Benutzer sehen Metriken ausschließlich über die
  tenant-geprüfte API im Plattform-Frontend.

## Phase 5 – Firewall & Security Policies

* **Policy-Modell:** `firewall_policies` mit `content = {address_lists, filter, nat}`; `tenant_id NULL`
  = globale MSP-Policy (für alle Mandanten les- und zuweisbar, nur vom MSP änderbar – durchgesetzt vom
  `GlobalOrTenantScoped`-Flush-Guard und explizit in der API).
* **Validierung statt Freitext:** Nur bekannte RouterOS-Felder (Whitelist), erlaubte Chains/Actions je
  Tabelle, Adressen werden geparst, Werte auf sichere Zeichen beschränkt. Beliebige RouterOS-Befehle
  können über Policies nicht eingeschleust werden.
* **Versionierung:** Jede inhaltliche Änderung erzeugt eine unveränderliche `policy_versions`-Zeile.
  Rollback = neue Version mit dem Inhalt einer alten Version (Historie bleibt linear nachvollziehbar),
  optional mit sofortigem Push.
* **Zuweisung:** `policy_assignments` (Gerät ↔ Policy, `position` bestimmt die Reihenfolge bei mehreren
  Policies). Zuweisen per Gerät, Standort oder Tag (wird beim Zuweisen expandiert).
  **Entscheidung:** keine dynamischen Tag-Gruppen – explizite Zuweisung ist für Audits nachvollziehbarer.
* **Push = kompletter Sollzustand pro Gerät:** Alle zugewiesenen Policies werden gerendert
  (Kommentar `sdwan:fw:<policy>:<f|n|a><idx>`) und per `sync_managed` angewendet: Address-Lists
  idempotent, Filter/NAT geordnet und **vor** den manuellen/Default-Regeln (`place-before`).
  Manuelle Regeln bleiben unangetastet.
* **Deployments:** Push auf mehrere Geräte parallel (max. 10 gleichzeitig) als Hintergrund-Task, Status
  in `policy_deployments` (`success | partial | failed | rolled_back`), Ergebnis pro Gerät, Live-Event.
* **Rollback bei Push-Fehlern:** Vor jedem Push wird der verwaltete Firewall-Stand des Geräts gesichert
  (Snapshot). Scheitert der Push, wird dieser Stand sofort wiederhergestellt. Im Modus **atomar** werden
  bei einem Fehler auch alle bereits erfolgreichen Geräte des Deployments zurückgesetzt.
* Entzug einer Zuweisung pusht das Gerät neu (Regeln der Policy verschwinden).

## Phase 6 – Zero-Touch Provisioning

* **Provisioning-Templates** (pro Mandant): Identity-Muster (`{tenant}-{site}-{name}`), Zeitzone, NTP,
  DNS, WAN-Interface für den ersten Boot, LAN (Bridge-Ports, LAN-IP – Standard: erste Adresse des
  ersten Standort-LANs –, DHCP-Server), WAN-Vorlage (wie Phase 3) und Liste von Firewall-Policies.
  Alle Werte werden validiert, bevor sie in ein RouterOS-Script gelangen.
* **Staging:** Geräte werden mit Seriennummer angelegt (einzeln oder als Liste). Der Pairing-Token ist
  **an die Seriennummer gebunden** (Pairing mit anderer Hardware wird abgelehnt) und langlebig
  (Standard 180 Tage, statt 72 h). Tokens werden nur einmal angezeigt (nur Hash in der DB); ein neues
  Bootstrap-Script invalidiert das alte.
* **Wie der Router „beim ersten Boot“ zieht:** Das Bootstrap-Script wird im Lager einmalig importiert
  oder per Netinstall (`netinstall -s sdwan-ztp.rsc`) als Default-Konfiguration eingespielt. Es legt
  einen DHCP-Client auf dem WAN-Port und einen Scheduler (`start-time=startup`, `interval=1m`) an, der
  das Onboarding-Script abruft, bis der Hub-Peer existiert, und sich dann selbst entfernt.
  **Entscheidung:** Kein Mechanismus ohne jede Vorab-Berührung (MikroTik bietet kein herstellerseitiges
  Cloud-Claiming für Dritte); der Kunde muss aber nichts tun außer Strom und Internet anschließen.
  Alternativ funktioniert der Token auch mit dem normalen Ein-Befehl-Onboarding.
* **Zwei Stufen:** (1) Die Pairing-Antwort enthält die Basiskonfiguration (Identity, Zeit, DNS, LAN,
  DHCP) – sie wirkt also sofort, auch bevor die Cloud das Gerät per API erreicht. (2) Beim ersten
  erfolgreichen Poll (Post-Poll-Hook) legt die Control-Plane die WAN-Links aus der Vorlage an und
  pusht sie, weist die Template-Policies zu und deployt sie, und stößt das Mesh an.
* Zustände: `staged → paired → provisioning → provisioned | failed`, mit Verlauf (`devices.ztp_log`).

## Phase 7 – Content Filtering (NextDNS)

* **Profile** (`content_filter_profiles`, pro Mandant): Parental-Control-Kategorien, blockierte Dienste,
  Security-Optionen, Privacy-Blocklisten, Deny-/Allowlist, SafeSearch/YouTube-Restricted/Block-Bypass,
  `force_dns`. Jedes Profil entspricht genau einem NextDNS-Profil (`<tenant-slug>-<name>`).
  Alle Werte werden gegen Kataloge/Domain-Regex validiert.
* **Synchronisation:** Objekte per `PATCH`, Arrays (Kategorien, Dienste, Blocklisten, Listen) per `PUT`
  (vollständiges Ersetzen = idempotent). Fehler werden am Profil gespeichert (`sync_status=error`) und
  können erneut synchronisiert werden; der Rest der Plattform bleibt funktionsfähig.
* **API-Keys:** globaler MSP-Key (`NEXTDNS_API_KEY`) oder mandanteneigener Key (Fernet-verschlüsselt in
  `tenant.settings`) – für Kunden mit eigenem NextDNS-Vertrag.
* **Zuweisung:** Standort-Profil > Mandanten-Standard > kein Filter.
* **Router-Konfiguration (DoH):** `use-doh-server=https://dns.nextdns.io/<profil>/<gerätename>`
  (Gerätename erscheint in den NextDNS-Logs), `verify-doh-cert=yes`, `servers=""`, statische
  Bootstrap-Einträge für `dns.nextdns.io` (45.90.28.0 / 45.90.30.0), Cache-Flush.
  **Entscheidung:** DoH statt DoT, da RouterOS keinen DoT-Client hat. CA-Vertrauen über
  `builtin-trust-anchors` (RouterOS ≥ 7.19), sonst einmaliger Import des curl-CA-Bundles.
* **DNS erzwingen:** optional `dstnat`-Redirect (UDP/TCP 53) aus den LAN-Netzen des Standorts auf den
  Router, damit Clients den Filter nicht mit eigenem DNS umgehen. „Block Bypass“ bei NextDNS blockiert
  zusätzlich bekannte DoH-/VPN-Umgehungen.
* **Rückbau:** Beim Entfernen der Zuweisung werden die vorherigen DNS-Server (bei der ersten Anwendung
  gesichert) wiederhergestellt und alle `sdwan:dns:`-Objekte entfernt.

## Phase 8 – Remote Access

* **Eigener Dienst `remote-proxy`** (gleiches Image, `python -m app.remote_proxy`): asyncio-TCP-Proxy mit
  Port-Pool (`REMOTE_PROXY_PORT_RANGE`, Standard 40000–40019). Er gleicht alle 2 s die aktiven Sessions
  aus der DB ab, öffnet/schließt Listener und verbindet **ausschließlich** zur Tunnel-IP des Geräts
  (gleicher Tunnel-Guard wie die RouterOS-API). **Entscheidung:** Plain-TCP-Weiterleitung statt
  Web-Terminal – funktioniert unverändert mit Winbox, SSH-Clients und WebFig.
* **Zeitlich begrenzt:** 5 min bis `REMOTE_SESSION_MAX_MINUTES` (Standard 240). Ablauf wird doppelt
  durchgesetzt: Proxy schließt Listener + laufende Verbindungen, Worker-Job markiert `expired`.
* **Quell-IP-Bindung:** Standard ist die IP des anfordernden Technikers (per `X-Forwarded-For` hinter
  dem Reverse-Proxy), optional ein CIDR. Fremde Quellen werden abgewiesen und auditiert (`remote.denied`).
* **Temporärer RouterOS-Benutzer pro Session** (`sdwan-rs-<id>`, Zufallspasswort, nur einmal angezeigt,
  Login nur von der Hub-Adresse) – dadurch personalisierte Logs auf dem Gerät, keine geteilten Admin-
  Passwörter, automatische Entfernung bei Ablauf/Schließen. Ist der Dienst (ssh/winbox/www) deaktiviert
  oder auf Adressen beschränkt, wird er aktiviert bzw. um die Hub-Adresse ergänzt.
* **Audit:** `remote.open`, `remote.connect` (Quell-IP), `remote.disconnect` (Dauer, Bytes),
  `remote.denied`, `remote.close`, `remote.expired`. Nur Techniker/Admins dürfen Sessions öffnen;
  schließen dürfen der Ersteller oder Admins.

## Phase 9 – Backups & Firmware

* **Export per SSH** (`/export terse` via asyncssh, API-Benutzer, nur über den Tunnel): Die RouterOS-API
  liefert `/export` nicht zuverlässig; `terse` erzeugt eine Zeile pro Objekt → aussagekräftige Diffs.
* **Keine Secrets in Backups:** RouterOS 7 blendet Passwörter/Keys im Export aus. **Entscheidung:** Die
  Text-Backups dienen Nachvollziehbarkeit, Diff und Wiederaufbau; binäre `.backup`-Dateien mit Secrets
  werden bewusst nicht in der Cloud gespeichert.
* **Speicherung:** Volltext + SHA-256 (ohne Zeitstempel-Kopfzeile) + Diff zum Vorgänger als JSONB
  (`{previous_id, added, removed, lines}`). Tägliches Backup (Standard 02:00 UTC) wird nur bei Änderung
  gespeichert; manuelle und Pre-Update-Backups immer (und gepinnt), seit Phase 13 auch nach Policy-Push. Retention: letzte 90 automatische
  Backups pro Gerät. Beliebige Stände lassen sich per API gegeneinander diffen.
* **Firmware-Jobs:** Geräte werden in Batches (`batch_size`) eingeteilt; ein Worker-Tick (15 s)
  bearbeitet nur den aktuellen Batch: Pre-Update-Backup → Kanal setzen → `check-for-updates` →
  `install` (bereits aktuelle Geräte = `skipped`) → nach dem Reboot Verifikation der neuen Version
  (Timeout 15 min) → optional RouterBOARD-Firmware-Upgrade + Reboot.
  Zwischen Batches wird `batch_interval_s` gewartet; erreichen die Fehler `max_failures`, wird der Job
  **pausiert** (Fortsetzen akzeptiert die bekannten Fehler). Abbrechen storniert alle wartenden Geräte.
* Firmware-Jobs können (wie Policy-Deployments) als MSP mandantenübergreifend laufen (`tenant_id NULL`),
  die Job-Items sind jedoch mandantengebunden.

## Phase 10 – Alerts & SLA-Reports

* **Regeltypen:** `device_offline`, `wan_down`, `latency` (WAN-Netwatch-RTT oder Latenz zur Cloud),
  `mesh_down`, `cpu_high`. Pro Regel: Schwere, Verzögerung (`duration_s`), Geltung (alle / Standorte /
  Geräte), Empfänger (leer = Kontakt-E-Mail des Mandanten), Entwarnung ja/nein.
* **Zustandsmaschine** je (Regel, Gerät, Subjekt): `pending` → nach `duration_s` → `firing` (Mail +
  Live-Event/Toast) → `resolved` (optional Entwarnungs-Mail). Nie gefeuerte `pending`-Einträge werden
  verworfen, damit kurze Flaps keine Alarme erzeugen. Auswertung minütlich im Worker (nach dem Poll),
  manuell per API auslösbar. Alarme können quittiert werden (Audit).
* **E-Mail:** SMTP mit STARTTLS; ohne `SMTP_HOST` werden Mails nur protokolliert (Entwicklung/Demo).
* **Statusverlauf:** Jeder Wechsel von Gerät (online/offline) und WAN-Link (up/down/degraded) wird als
  `status_events`-Zeile gespeichert. **Entscheidung:** SLA aus Zustandswechseln (exakt, speicherarm)
  statt aus Metrik-Stichproben in InfluxDB.
* **Verfügbarkeit** = online / (online + offline) im Zeitraum; Zeit mit unbekanntem Zustand (vor dem
  ersten Kontakt) zählt nicht. Zusätzlich Anzahl Ausfälle, längster Ausfall, MTTR, WAN-Verfügbarkeit.
* **Berichte:** ad hoc als JSON/PDF (reportlab) für beliebige Zeiträume (max. 1 Jahr); gespeichert und
  optional versendet. **Automatisch** am 1. jedes Monats (06:00 UTC) für den Vormonat an Kontakt +
  konfigurierbare Empfänger (abschaltbar pro Mandant).

## Phase 11 – VRRP & Backup-Transparenz

**Szenario:** Filialen hängen per Glasfaser am zentralen Core. Im gemeinsamen Kassen-VLAN
(z. B. `192.168.110.0/24`) ist die FortiGate VRRP-Master (VIP `192.168.110.1`, Priorität 255, Preempt).
Je Standort läuft ein MikroTik (z. B. L009UiGS-RM) als VRRP-Backup an `ether2` und hat ein 5G-Modem an
`ether8`. WAN1 = `ether2` mit der *echten* FortiGate-IP als Gateway (nicht die VIP), WAN2 = `ether8` per DHCP.
Fällt die FortiGate bzw. die Glasfaser aus, übernimmt der MikroTik die VIP und leitet die Kassen über 5G.

* **Bugfix Verbindungs-Flush im Failover:** Bisher wurden Verbindungen nur im PCC-Modus geleert
  (per `connection-mark`). Im Failover bleibt das Interface physisch „up“, NAT-Einträge hängen aber an
  der alten Quelladresse. Das Netwatch-Down-Skript entfernt jetzt alle Verbindungen, deren Antwort an eine
  IP des WAN-Interfaces geht (`reply-dst-address`). Ausgenommen sind UDP-Verbindungen zu den
  WireGuard-Ports von Hub und Mesh, damit `sdwan-mgmt` und die Mesh-Tunnel nicht abreißen.
* **Datenmodell:** `vrrp_instances` (mandantenbezogen, mehrere pro Gerät): Name (= Name des
  VRRP-Interfaces), Interface, VRID, Priorität, Intervall, Preemption, Version, VIP, optionale lokale
  Adresse, optional gekoppelter WAN-Slot, aktiv. Laufzeit: `state` (master/backup/disabled/unknown),
  `last_change_at`. Migration `0011`.
* **Validierung:** VRID 1–255, Priorität 1–254 (255 hat nur der Adress-Besitzer, also die FortiGate),
  VIP ohne Präfix wird zu `/32`, andere Präfixe werden abgelehnt. Mit lokaler Adresse muss die VIP in
  deren Netz liegen und darf nicht gleich sein. Name und Interface nur `[A-Za-z0-9._-]`, damit nichts in
  RouterOS-Skripte eingeschleust wird. (Interface, VRID) ist pro Gerät eindeutig.
* **Push (idempotent, `sync_managed`):** `/interface/vrrp` mit Kommentar `sdwan:vrrp:<id8>`, lokale
  Adresse `sdwan:vrrp:<id8>:local` auf dem physischen Interface, VIP `sdwan:vrrp:<id8>:vip` als `/32`
  auf dem VRRP-Interface. Reihenfolge: lokale Adressen → VRRP-Interfaces → VIPs, damit keine Adresse auf
  ein noch fehlendes Interface zeigt. Manuell angelegte VRRP-Instanzen und Adressen ohne `sdwan:`-Kommentar
  bleiben unangetastet.
* **RouterOS-7-Syntax:** Die Felder (`interface`, `vrid`, `priority`, `interval`, `preemption-mode`,
  `version`, `on-master`, `on-backup`) stammen aus dem Schema des Terraform-Providers `routeros`, weil
  help.mikrotik.com aus der Entwicklungsumgebung nicht erreichbar war. **Nicht geprüft:** ob die
  API bei `print` die Flags M/B als Felder `master`/`backup` liefert. Der Poller liest diese Felder und
  fällt sonst auf `running` zurück (VRRP-Interface läuft = Master). Bitte am echten Gerät mit
  `/interface/vrrp print detail` gegenprüfen. Version 3 ist Standard in RouterOS 7; die FortiGate muss
  dieselbe VRRP-Version sprechen.
* **Gekoppeltes WAN:** `on-master` schaltet sofort die Default-Routen des Slots ab
  (`sdwan:wan:default:<slot>`) und leert dessen Verbindungen (gleicher Flush wie oben). So wartet der
  Router nicht erst auf die Netwatch. `on-backup` schaltet **nicht** blind wieder ein: Nach der
  Recovery-Verzögerung werden die Routen nur aktiviert, wenn die Netwatch des Slots „up“ meldet. Die
  Hysterese bleibt also bei der Netwatch; ist sie noch „down“, übernimmt später ihr eigenes Up-Skript.
* **Status:** Ein Poll-Hook liest `/interface/vrrp`. Wechsel landen in `status_events` (`vrrp:<id>`)
  und als Live-Event `vrrp.state`. Aktivwechsel eines WAN-Links werden ebenfalls protokolliert
  (`wanactive:<id>`, `wan_links.active_since`, Migration `0012`).
* **Simulator:** `/interface/vrrp` mit Master/Backup und Ausführung von `on-master`/`on-backup` über
  einen kleinen Interpreter für die verwendeten Befehle. Im Tab „VRRP“ gibt es „Master werden“ und
  „Backup werden“ zum Vorführen.
* **ZTP:** Das Template kann eine `vrrp`-Liste enthalten. Die lokale Adresse ist je Gerät verschieden
  und wird deshalb beim Vorbereiten pro Gerät angegeben (dritte Spalte) und schon beim Staging gegen die
  Template-VIP geprüft. Ausgerollt wird nach WAN und vor den Policies.
* **Alarme:** `vrrp_master` (Standard-Regel „VRRP: Standort auf Backup (Master)“, Warnung, 30 s)
  und `wan_backup_active`: Im **Failover**-Modus trägt ein WAN mit schlechterer Priorität als der
  beste aktive Link die Default-Route. Bei Lastverteilung ist das der Normalbetrieb, dort gibt es daher
  keinen Alarm. Beide werden wie alle anderen Alarme automatisch behoben.
* **Datenvolumen:** Optionales Monatslimit (GB, dezimal wie bei Mobilfunktarifen) pro WAN-Link.
  **Entscheidung:** Das Volumen wird in der DB aufsummiert (Delta der Interface-Zähler rx+tx jedes
  *erfolgreichen* Polls), nicht per Flux-Abfrage. Grund: exakt, auch ohne InfluxDB, und für Alarme
  billig abfragbar. Kleinerer Zählerstand als zuvor = Reboot (aktueller Stand zählt). Monatswechsel
  (UTC) setzt auf 0. Alarm `wan_volume` mit den Schwellen 80 % und 100 % (Parameter `thresholds`), je
  Schwelle ein eigener Alarm. Migration `0013`.
* **SLA:** Je Gerät „Zeit auf Backup-WAN“ (Vereinigung der `active`-Phasen aller Backup-Links, nur
  Failover) und „Zeit als VRRP-Master“ (Vereinigung der `master`-Phasen), jeweils mit Anzahl der
  Umschaltungen. Beides steht in der JSON-Antwort, in der PDF-Tabelle „Backup-Betrieb“, im gespeicherten
  Monatsbericht und im Text der Monats-Mail.
* **Webhooks (je Alarmregel, zusätzlich zur E-Mail):** Format `generic` (flaches JSON mit
  `event`, `title`, `text`, …) oder `teams` (Nachricht mit Adaptive Card, wie sie Teams-Workflows
  „Send webhook alerts to a channel“ annehmen). Erlaubt ist nur `https`, ohne Zugangsdaten in der URL.
  Interne Ziele werden abgelehnt: bei der Eingabe per Name/IP und vor jedem Versand per DNS-Auflösung
  (SSRF-Schutz). Die URL wird verschlüsselt gespeichert, weil Workflow-URLs eine Signatur enthalten, und
  in API und Audit-Log nur maskiert angezeigt. Migration `0014`.

## Phase 12 – Branding & Mail-Layout

* **Positionierung:** Das Produkt heißt „MikroTik-Fleet-Management“: Konfigurations- und
  Flottenmanagement für MikroTik-Router. SD-WAN, VPN-Mesh und WAN-Failover bleiben als Funktionen
  (Menüpunkte, Doku-Abschnitte) bestehen, sind aber nicht mehr der Produktname.
* **Branding per Einstellung:** `PRODUCT_NAME`, `PRODUCT_SHORT` (Betreff), `MAIL_ACCENT_COLOR`,
  `MAIL_LOGO_URL`, `MAIL_FOOTER_TEXT`, `MAIL_SUBJECT_EMOJI`. Das Frontend holt Name und Kurzname zur
  Laufzeit aus dem öffentlichen `GET /api/v1/meta` (auch der Seitentitel). Im Build steht kein
  Produktname, ein Rebranding braucht also keinen neuen Frontend-Build. PDF-Berichte tragen den
  Produktnamen im Kopf, in den Metadaten und in der Fußzeile.
* **Bewusst NICHT umbenannt:** Der Kommentar-Präfix `sdwan:` auf den Routern, die Interfaces
  `sdwan-mgmt`/`sdwan-mesh`, der API-Benutzer `sdwan`, DB-Namen und -Tabellen, Docker-Dienste,
  Installationspfad `/opt/sdwan`, Grafana-UIDs und die Log-/Hinweistexte in den Router-Skripten
  (Netwatch, VRRP, Onboarding, ZTP). `sync_managed` erkennt verwaltete Einträge am Präfix. Eine
  Umbenennung würde bestehende Router-Konfigurationen verwaisen lassen bzw. auf jedem Router ein Update
  der Skripte auslösen, ohne funktionalen Nutzen. Grafana-Dashboards und der Ordner bekommen nur neue
  *Titel* („MikroTik-Flotte · …“), die UIDs bleiben.
* **Mails:** `send_mail(..., html=...)` erzeugt multipart/alternative. Der Klartext steht immer
  zuerst und dient als Fallback, danach kommt HTML, Anhänge machen daraus multipart/mixed. Die
  Templates liegen unter `backend/app/templates/mail/` (Jinja2, Autoescape für HTML):
  `base.html` (Kopf, Balken, Inhalt, Fußzeile), `alert.html`/`alert.txt`, `test.html`, `sla.html`.
  Outlook-Regeln: nur Tabellenlayout, nur Inline-CSS (kein `<style>`), 600 px Breite, Systemschriften,
  kein JavaScript, Button als Tabellenzelle mit `bgcolor`, Logo nur mit `MAIL_LOGO_URL`.
  **Dark Mode:** Deklariert wird nur das helle Schema (`color-scheme: light`). Clients zeigen es
  unverändert oder invertieren komplett, beides bleibt lesbar. Farbbalken und Button nutzen weiße
  Schrift auf kräftiger Farbe und funktionieren in beiden Fällen.
* **Alarm-Mail:** Statusbalken (kritisch rot, Warnung orange, Info blau, behoben grün) mit
  „AUSGELÖST“/„BEHOBEN“ und Alarmtyp. Darunter eine Überschrift „Standort X – Gerät: Kurzmeldung“ und
  eine Info-Tabelle (Mandant, Standort, Gerät mit Modell und RouterOS-Version, Regel, Schweregrad,
  Beginn, Ende, Dauer). Es folgt ein Kontextblock je Alarmtyp aus der DB: WAN-Tabelle, VRRP-Instanz,
  Messwert mit Schwelle, letzter Kontakt oder Mesh-Gegenstelle. Dann statische „Empfohlene nächste
  Schritte“ je Typ (`mail_render.NEXT_STEPS`), bei „behoben“ stattdessen eine Zusammenfassung. Am Ende
  „Gerät öffnen“ und „Alarm quittieren“ sowie eine Fußzeile mit dem Grund des Empfangs (Regelname).
* **Betreff:** `[KURZ] 🔴 KRITISCH | Standort – Gerät: Kurzmeldung` (🟠 WARNUNG, 🔵 INFO),
  behoben `[KURZ] ✅ BEHOBEN | … (Dauer 24 min)`. Die Emojis lassen sich mit `MAIL_SUBJECT_EMOJI=false`
  abschalten. Die Kurzmeldung wird je Typ aus den Objekten erzeugt, nicht aus der langen Alarmmeldung;
  diese steht weiter im Klartext und in der Alarmliste.
* **Zeitzone je Mandant:** `tenants.timezone` (IANA, Standard `Europe/Vienna`, Migration `0015`).
  Alle Zeiten in Mails und SLA-PDFs werden umgerechnet, Format „25.09.2026, 11:39 Uhr“. In der DB bleibt
  alles UTC. `tzdata` ist als Python-Paket in den Requirements, damit die Zeitzonen auch im
  schlanken Container verfügbar sind.
* **Test-Mail und SLA-Mail** nutzen dasselbe Grundlayout. Die SLA-Mail zeigt die Verfügbarkeit je
  Gerät, einschließlich Zeit auf Backup-WAN bzw. als VRRP-Master; das PDF bleibt Anhang.
  Webhooks übernehmen Betreff, Überschrift und die lokalen Zeiten.
* **Vorschau:** `GET /api/v1/alerts/mail-preview?type=<typ>&state=firing|resolved[&format=text]`
  (nur MSP-Admins) rendert die Mail mit Beispieldaten, ohne DB-Zugriff. Der Betreff steht URL-kodiert
  im Header `X-Mail-Subject`.

## Phase 13 – Labortest-Werkzeuge

Vorbereitung auf den ersten Test mit echter Hardware (L009UiGS-RM, RouterOS 7). Die Plattform wurde bis
dahin nur gegen den Simulator entwickelt; die folgenden Werkzeuge sollen Abweichungen früh sichtbar machen.
Die Checkliste für den Test steht in `docs/LABORTEST.md`.

### Hardware-Selbsttest

* **Eine Quelle für Pfade und Felder:** `backend/app/routeros/schema.py` (`PATH_SPECS`) listet jeden
  RouterOS-Pfad, den die Plattform liest, mit Pflichtfeldern, optionalen Feldern, Nutzern und Hinweisen.
  Der Selbsttest und der Simulator-Test (`tests/test_selftest.py`) lesen dieselbe Liste. Wer Code ergänzt,
  der ein neues Feld liest, trägt es dort ein.
* **Nur lesend:** ausschließlich `print`, `/ping` (1 Paket zum Hub) und der Export über SSH (wie im
  Echtbetrieb). Kein `check-for-updates`, kein `set`/`add`/`remove`. `/ip/firewall/connection` wird mit
  `count-only` gezählt und nur bei ≤ 5000 Einträgen mit `.proplist` gelesen.
* **Ampel je Pfad:** rot = nicht erreichbar oder Pflichtfeld fehlt; orange = leere Pflicht-Tabelle oder
  fehlendes optionales Feld mit Warnhinweis; grün = in Ordnung (leere, nicht zwingende Tabellen sind grün mit
  dem Hinweis „Feldprüfung übersprungen“). Pflichtfelder müssen in jeder Zeile stehen, weil RouterOS leere
  Felder (z. B. `comment`) weglässt; diese sind deshalb optional. Gesamtstatus = schlechtester Einzelwert.
* **Zusatzprüfungen:** RouterOS ≥ 7, Architektur bekannt, Policies der Gruppe des API-Benutzers
  (Pflicht: `api, read, write, policy, reboot, test, ssh`; `sensitive` fehlt → orange, weil der Export
  dann keine Schlüssel/Passwörter enthält), `/ip service` `api` und `ssh` aktiv und für die Hub-Adresse
  erlaubt (sonst rot, der Export läuft über SSH), Uhrzeitabweichung (> 60 s orange, > 300 s rot).
  `latest-version` ist erst nach einer Update-Prüfung befüllt und daher nur ein Hinweis.
* **Speicherung:** letzter Lauf je Gerät in `device_selftests` (eigene Tabelle, damit der Poller ihn nie
  überschreibt). Die Oberfläche zeigt die Karte „Selbsttest“ in der Übersicht mit JSON-Export.

### Neustart und Alarm-Unterdrückung

* `POST /devices/{id}/reboot` (Techniker): `/system/reboot`, Audit `device.reboot`. Ein Verbindungsabbruch
  direkt danach gilt als Erfolg. Die Plattform setzt `device.facts.reboot = {at, by, until}` mit
  `until = at + 5 min`.
* `alerts.reboot_suppressed()`: Die Bedingung `device_offline` wird bis `until` übersprungen. Andere
  Alarmtypen bleiben aktiv. Kommt das Gerät bis dahin nicht zurück, greift danach die normale Alarmierung
  (Test `test_device_not_returning_alarms_after_five_minutes`).
* Der Poller entfernt die Markierung, sobald das Gerät mit einer Uptime antwortet, die kleiner ist als die
  seit dem Neustart vergangene Zeit, oder wenn `until` erreicht ist. Bis dahin zeigt die Oberfläche
  „Neustart läuft“.
* Die Bestätigung verlangt die Eingabe des Gerätenamens. Firmware-Updates nutzen diese Unterdrückung
  (noch) nicht.

### VRRP-Gegenstelle

* Pro Instanz optional `peer_address` (IP des Hauptsystems, z. B. FortiGate) und `peer_description`.
  Die Gegenstelle muss im Netz von `local_address` liegen und sich von VIP und lokaler Adresse
  unterscheiden. Sie wird nicht auf den Router geschrieben. Die Priorität der Gegenstelle wird bewusst
  nicht geführt.
* Bei jeder Abfrage: `/ping address=<peer> src-address=<lokale IP> count=3 interval=200ms timeout=500ms`
  (auf dem Router ≤ ~1,5 s). Zusätzlich begrenzt `PEER_PING_LIMIT_S = 2` die Wartezeit auf der
  Plattformseite. Timeout oder Fehler bedeutet „nicht erreichbar“, der restliche Poll läuft weiter. Nach
  einem Abbruch wird die API-Verbindung verworfen (`LibRouterOSConnection._broken`), weil der Thread sonst
  weiter vom Socket liest. Der VRRP-Hook läuft deshalb als letzter.
* Die Ping-Ziele stehen in `device.facts.vrrp_peers`. Sie werden beim Speichern und in jedem Post-Poll
  aktualisiert, sodass auch per ZTP angelegte Instanzen erfasst werden. „Peer prüfen“ führt
  `POST /devices/{id}/vrrp/{inst}/ping` aus. Ändert sich die Erreichbarkeit, gibt es das Event `vrrp.peer`.

### Backup-Metadaten

* `config_backups.created_by` (Migration 0018, Altbestand `unbekannt`). Werte: E-Mail des Benutzers
  (manuell, Firewall-Übernahme), Ersteller des Firmware-Jobs (`pre-update`), Starter des Deployments
  (`post-policy`) und `system` (geplant).
* Neuer Auslöser `post-policy`: Nach einem erfolgreichen Policy-Push entsteht je Gerät ein Backup.
  Das ist best effort: Exportfehler werden nur protokolliert und ändern das Deployment-Ergebnis nicht.
  Diese Backups unterliegen der Aufbewahrungsgrenze; gepinnt sind nur `manual` und `pre-update`.
* Die Liste zeigt die SHA-256-Prüfsumme gekürzt auf 12 Zeichen, den vollen Wert im Tooltip und kopierbar.

### IP-Adressen und Sensoren

* Poll-Hook `device_info`: `/ip/address` und `/ip/dhcp-client`. Kennzeichnung: DHCP (dynamisch und
  DHCP-Client auf dem Interface), dynamisch, deaktiviert, ungültig, von der Plattform verwaltet.
  `GET /devices/{id}/addresses` liest live, sonst den Stand der letzten Abfrage.
* `/system/health`: RouterOS-7-Format (`name/value/type`) und flaches Format werden in eine Liste
  `[{name, value, unit}]` überführt (nur Zahlenwerte in C/V/A/W). Die Kacheln Temperatur (CPU bevorzugt)
  und Spannung erscheinen nur, wenn Werte vorhanden sind. Es gibt keine Alarmregel darauf.

## Frontend-Designsystem

Visuelle Vorlage ist der Prototyp in `docs/design/` (`FleetApp.dc.html`). Übernommen wurden Layout,
Komponenten, Farben, Typografie und Interaktionen, **nicht** der Code und nicht die Beispieldaten: Jede
Anzeige kommt aus der API. Was das Backend nicht liefert, fehlt in der Oberfläche; es gibt keine Platzhalter.

* **Tokens:** `frontend/src/index.css` übernimmt die CSS-Variablen 1:1 aus den Blöcken `[data-fm]` (hell)
  und `[data-fm-theme="dark"]` (`--bg`, `--panel`, `--panel2`, `--sunken`, `--hover`, `--border`,
  `--border-strong`, `--text*`, `--blue*`/`--green*`/`--orange*`/`--red*`/`--gray*`, `--c2`, `--code`,
  `--shadow`, `--overlay`). Über `@theme inline` sind sie als Tailwind-Klassen nutzbar: `bg-panel`,
  `text-fg2`, `border-line`, `bg-orange-bg`, `text-red-text` usw. Ältere Klassen (`slate-*`, `emerald-*`,
  `amber-*`, `brand-*`) zeigen übergangsweise auf dieselben Tokens, damit nichts im Dunkelmodus hell bleibt.
  Die Standard-Rahmenfarbe ist `--border`, weil Tailwind 4 sonst `currentColor` nimmt.
* **Theme:** `data-theme="light|dark"` am `<html>`. Auswahl Hell/Dunkel/System im Kopf und auf der
  Login-Seite, gespeichert in `localStorage` (`fm.theme`). „System“ folgt `prefers-color-scheme` live
  (`lib/theme.ts`). Ein Inline-Script in `index.html` setzt das Theme vor dem ersten Rendern, damit es
  nicht aufblitzt. Tailwind-`dark:` hängt am Attribut (`@custom-variant`).
* **Schrift:** Geist und Geist Mono werden lokal über `@fontsource/geist-sans` und `@fontsource/geist-mono`
  ausgeliefert; es gibt keine Anfragen an Google Fonts oder andere CDNs. Global gilt
  `font-variant-numeric: tabular-nums`. Monospace nutzen IPs, Interfaces, Seriennummern, Befehle, Hashes
  und Zeitstempel in Tabellen.
* **Icons:** `components/Icon.tsx` enthält die SVG-Pfade aus dem Prototyp (Lucide-Stil), ohne zusätzliche
  Abhängigkeit.
* **Rahmen (`components/Layout.tsx`):** Seitenleiste 232 px, einklappbar auf 60 px (Zustand in
  `localStorage`), Bereich „Verwaltung“. Zähler-Badges: aktive Alarme (rot) und ausstehende
  Firmware-Updates. Unter 1024 px wird die Seitenleiste zum Overlay-Menü (Esc schließt). Kopfzeile mit
  Mandanten-Wechsel (nur MSP-Admins), globaler Suche (Strg/⌘+K; Gerät, Tunnel-/Mesh-IP, Seriennummer,
  Standort, Tag), Alarm-Glocke, Theme-Umschalter und Benutzermenü (Live-Status, Abmelden). Der
  Produktname kommt aus `/api/v1/meta`.
* **Komponenten (`components/ui.tsx`):** `Card`, `PageHeader`, `Button` (primary/secondary/ghost/danger),
  Formularfelder mit Hinweis per `aria-describedby`, `Toggle`, `Pill`/`StatusBadge`/`SeverityBadge`
  (immer Icon + Text, nie nur Farbe), `KpiTile`, `Segment`, `Tabs` mit Zähler, `Table` (Kopf `panel2`,
  in Karten randlos), `RowCheck` + `SelectionBar` für Mehrfachauswahl, `Modal`/`Dialog` (max. 88vh,
  Inhalt scrollt, Footer bleibt sichtbar, Esc, Fokusfalle, Fokus-Rückgabe), `CodeBlock` mit
  Kopieren-Button (entfernt Leerzeilen zwischen Befehlen), `EmptyState`, `Loading`, `Notice`.
  Fachliche Bausteine (aktiver WAN, VRRP-Rolle, CPU-Balken) liegen in `components/fleet.tsx`.
* **Diagramme:** Die vorhandene `components/Chart.tsx` wurde erweitert (Token-Farben, 5 Zeitmarken,
  gestrichelte Markierungen mit schattiertem Bereich für Backup-Betrieb, verzerrungsfreie Beschriftung
  über `ResizeObserver`). Es gibt keine Chart-Bibliothek.
* **Einheitliche Alarm-Definition (`lib/fleet.ts`):** aktiv = ausgelöst, nicht behoben, nicht quittiert.
  Glocke, Seitenleiste, Dashboard und der Filter „Aktiv“ nutzen dieselbe Funktion `alarmState()`.
* **Neue Lese-Endpunkte für das Frontend:**
  * `GET /dashboard/fleet-state`: aktiver WAN, VRRP-Rolle und Backup-Betrieb je Gerät.
  * `GET /devices/{id}/events`: state_log für Rollenverlauf, Ereignisse und Failover-Markierungen.
  * `GET /devices/{id}/wan/routes`: verwaltete `sdwan:wan`-Routen live vom Router.
  * Phase 13: `GET/POST /devices/{id}/selftest`, `POST /devices/{id}/reboot`,
    `GET /devices/{id}/addresses`, `POST /devices/{id}/vrrp/{inst}/ping`.

  Bestehende Endpunkte blieben unverändert.
* **Kontrast:** Badge-Text auf Badge-Grund ≥ 4,5:1 in beiden Themes, Sekundärtext ≥ 4,6:1. Primär-Buttons
  nutzen das eigene Token `--btn-primary` (#2563EB in beiden Themes, weißer Text 5,2:1); `--blue` selbst
  bleibt der Designwert und wird weiter für Linien, Diagramme und Fokusrahmen genutzt.
