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
  gespeichert; manuelle und Pre-Update-Backups immer (und gepinnt). Retention: letzte 90 automatische
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
