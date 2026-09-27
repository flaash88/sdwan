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
* Peer-Sync gegen den **Ist-Zustand** des Interfaces (`wg show wg0 dump`: Public-Keys + Allowed-IPs), nicht gegen
  `/data/wg0.conf` – das Volume überlebt Neustarts, `wg0` wird beim Start aber leer angelegt. `syncconf` läuft, wenn
  Ist ≠ Soll, beim Start immer einmal, und erneut, wenn nach einem Sync die Peer-Anzahl im Interface nicht der
  API-Liste entspricht (Warnung im Log).
* Healthcheck `python /app/agent.py --health`: unhealthy, wenn der letzte erfolgreiche Sync > 3 min alt ist, die
  Peer-Anzahl im Interface ≠ API-Liste oder die Route ins WG-Netz nicht über `dev wg0` läuft. Der Hub startet erst,
  wenn die API healthy ist.
* **Route im Hub-Namespace:** syslog und flows laufen mit `network_mode: service:wireguard-hub`. Sie bekommen deshalb
  kein `NET_ADMIN` und leere `ROUTE_VIA_HUB`/`ROUTE_WG_NETWORK`; `WG_NETWORK` behält für die App den echten Wert
  aus `.env`. Der Backend-Entrypoint setzt die Route `ROUTE_WG_NETWORK via ROUTE_VIA_HUB` nur, wenn im eigenen Namespace kein `wg0`
  existiert. Der Hub-Agent prüft bei jedem Sync, dass die Route über `dev wg0` läuft, und korrigiert sie sonst
  (`ip route replace <netz> dev wg0 src <hub-ip>`, Warnung im Log). `update.sh` pingt am Ende alle Tunnel-IPs vom
  Hub aus an und gibt die Anzahl erreichbarer Geräte aus.
* Plattform-Alarm `hub_no_peers`: `/internal/hub/stats` meldet 0 Peers, obwohl gekoppelte Geräte existieren
  (Mail an MSP-Admins, Plattform-Webhook); behoben, sobald wieder Peers gemeldet werden.
* `deploy/update.sh`: wurde der Hub neu erstellt, werden `syslog` und `flows` (Netz-Namespace des Hubs) neu
  gestartet; danach wartet das Script auf „healthy“ und gibt die Peer-Anzahl aus.
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
* **Zugangsdaten in der Sitzungsansicht:** Host/DNS, Port, Benutzer, Passwort je mit Kopieren-Button
  (`CopyButton`, Bestätigung „Kopiert“); das Passwort ist maskiert (Auge zum Anzeigen), kopiert wird immer der
  Klartext. „Alles kopieren“ liefert `Host: <dns>:<port>` / `Benutzer:` / `Passwort:`; SSH zusätzlich
  `ssh -p <port> <user>@<dns>`, WinBox `winbox.exe <dns>:<port> <user> <pass>` (ANNAHME Labor: WinBox 3/4
  nehmen Adresse, Benutzer, Passwort als Argumente). Die Zwischenablage wird nicht automatisch geleert (Browser
  erlauben das nicht zuverlässig); Hinweis „Zugangsdaten gelten nur für diese Sitzung“. Jedes Kopieren des
  Passworts (einzeln, im Block oder im WinBox-Aufruf) → Audit `remote.credentials_copied` (ohne Passwort).

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
  (gegen `API_CORE_POLICIES`/`API_RECOMMENDED_POLICIES`, siehe „Rechte und Gruppen“), `/ip service`
  `api` und `ssh` aktiv und für die Hub-Adresse erlaubt (sonst rot, der Export läuft über SSH),
  Uhrzeitabweichung (> 60 s orange, > 300 s rot).
  `latest-version` ist erst nach einer Update-Prüfung befüllt und daher nur ein Hinweis.
* **Speicherung:** letzter Lauf je Gerät in `device_selftests` (eigene Tabelle, damit der Poller ihn nie
  überschreibt). Die Oberfläche zeigt die Karte „Selbsttest“ in der Übersicht mit JSON-Export.

### Rechte und Gruppen

* **Eine Definition:** `routeros/schema.py` enthält `API_GROUP = "sdwan-api"` mit `API_POLICIES`
  (`read, write, api, policy, reboot, test, ssh, sensitive, winbox, web`), aufgeteilt in Kern-Policies
  (fehlt eine → Selbsttest rot) und empfohlene (`sensitive`, `winbox`, `web` → orange mit Begründung).
  Onboarding-Skript, Selbsttest und „Rechte einschränken“ lesen nur diese Werte.
* **Onboarding/ZTP:** Die Pairing-Antwort legt `sdwan-api` an bzw. setzt bei vorhandener Gruppe nur die
  Policies und legt den API-Benutzer in dieser Gruppe an. `full` wird nicht mehr verwendet. ZTP holt
  dasselbe Skript und ist damit abgedeckt.
* **Altgeräte** (API-Benutzer in `full`) werden nicht automatisch umgestellt. Der Selbsttest zeigt einen
  orangen Hinweis, und der Button „Rechte einschränken“ (`POST /devices/{id}/restrict-api-user`,
  Techniker, Audit `device.restrict_api_user`) stellt mit **Totmannschaltung** um
  (`services/api_rights.py`):
  1. Vorherige Gruppe lesen.
  2. `/system clock` lesen und Scheduler `sdwan-revert-api-group` mit **festem Start** anlegen:
     `start-date`/`start-time` = Router-Zeit + 3 min, dazu `interval=1m` als Sicherheitsnetz.
     `on-event` stellt erst die vorherige Gruppe wieder her und entfernt danach den Scheduler. Schlägt das
     Zurückstellen fehl, läuft er nach einer Minute erneut. Ist die Uhr nicht lesbar, wird nichts umgestellt.
  3. Gruppe anlegen/aktualisieren und zurücklesen. Weichen die Policies ab, wird nicht umgestellt und
     der Scheduler entfernt.
  4. Benutzer umstellen.
  5. **Neue** Verbindung aufbauen und den Selbsttest ausführen.
  6. Nur wenn beides klappt (Selbsttest nicht rot), den Scheduler löschen. Sonst zeigt die Oberfläche
     „Rechte werden in ca. 3 Minuten automatisch zurückgestellt“. Der Terminal-Befehl steht nur als
     letzte Rückfallebene darunter.
* **Fernzugriff:** Temporäre Benutzer liegen in der Gruppe `REMOTE_GROUP = "sdwan-remote"` mit
  `REMOTE_POLICIES` (`ssh, read, write, test, winbox, web, reboot, sensitive`, bewusst ohne `policy`,
  `api` und `local` – letzteres ist nur der Konsolen-Login). Die Gruppe wird bei jeder Sitzung angelegt
  bzw. aktualisiert und zurückgelesen. Scheitert
  das, bricht die Sitzung mit einer klaren Meldung ab; es gibt kein Ausweichen auf `full`.
  `REMOTE_POLICIES ⊆ API_POLICIES` sichert ein Test (`tests/test_policies.py`) ab.
* **Annahme, im Labor zu verifizieren:** RouterOS lehnt das Anlegen einer Gruppe mit Policies ab, die der
  anlegende Benutzer selbst nicht hat. Der Simulator bildet das nach (`_check_group_rights`, abschaltbar
  über `SIMULATOR_ENFORCE_GROUP_RIGHTS=false`).
* **Zeitrechnung für den Scheduler** (`api_rights.revert_start`, `routeros/util.py`): Die Rechnung läuft
  mit `datetime` in der lokalen Zeit des Routers, in der er auch `start-date`/`start-time` auswertet.
  Tages-, Monats- und Jahreswechsel sowie Schaltjahre sind damit korrekt. Das Datum geht im Format zurück,
  in dem der Router es liefert (`jan/02/2026` bis 7.9, `2026-01-02` ab 7.10). Selbsttest und
  Totmannschaltung nutzen denselben Parser (`parse_router_datetime`).

### Fernzugriff: Dienstzustand wiederherstellen

* Vor dem Einschalten eines Dienstes (`www` für WebFig, `winbox`, `ssh`) speichert die Sitzung dessen
  Zustand in `remote_sessions.service_restore` (Migration 0019): `{service, disabled, address, changed}`.
  `changed` ist nur gesetzt, wenn die Plattform den Dienst eingeschaltet oder die Hub-Adresse ergänzt hat.
* **Parallele Sitzungen:** Eine neue Sitzung übernimmt den gespeicherten Ursprungszustand einer schon
  laufenden Sitzung desselben Dienstes, nicht den bereits geänderten. Beim Beenden oder Ablauf wird nur
  zurückgestellt, wenn keine andere aktive Sitzung den Dienst noch nutzt. Zurückgestellt wird genau
  `disabled` und `address` von vorher.
* **Robustheit:** Scheitert eine Sitzung nach dem Einschalten oder ist der Router beim Beenden nicht
  erreichbar, bleibt `pending` gesetzt. Der Worker-Job `expire_sessions` (alle 30 s) versucht es erneut.
  Er liest alles aus der Datenbank und räumt deshalb auch nach einem Neustart der Plattform auf.

### Neustart und Alarm-Unterdrückung

* `POST /devices/{id}/reboot` (Techniker): `/system/reboot`, Audit `device.reboot`. Ein Verbindungsabbruch
  direkt danach gilt als Erfolg. Die Plattform setzt `device.facts.reboot = {at, by, reason, until}` mit
  `until = at + Dauer je Anlass`.
* `alerts.reboot_suppressed()`: Die Bedingung `device_offline` wird bis `until` übersprungen. Andere
  Alarmtypen bleiben aktiv. Kommt das Gerät bis dahin nicht zurück, greift danach die normale Alarmierung
  (Test `test_device_not_returning_alarms_after_five_minutes`).
* Der Poller entfernt die Markierung, sobald das Gerät mit einer Uptime antwortet, die kleiner ist als die
  seit dem Neustart vergangene Zeit, oder wenn `until` erreicht ist. Bis dahin zeigt die Oberfläche
  „Neustart läuft“.
* Die Bestätigung verlangt die Eingabe des Gerätenamens.
* **Dauer je Anlass** (`alerts.REBOOT_SUPPRESS`): manuell 5 min, Firmware 10 min (RouterOS-Update und
  RouterBOARD-Firmware bedeuten zwei Neustarts). `alerts.mark_reboot(dev, reason, by)` ist der einzige
  Einstieg. Der Firmware-Job ruft ihn vor `/system/package/update/install` und erneut vor dem
  RouterBOARD-Neustart auf. `facts.reboot.reason` steuert die Anzeige
  („Neustart läuft (Firmware-Update)“).

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

## Phase 14 – Firewall-Editor (vereinfacht)

Plan und Entscheidungen: `docs/PLAN-PHASE-14-20.md`.

* **Kompilieren statt neuer Push-Logik:** Eine Policy ist `mode=expert` (bisheriges Rohformat, Standard für
  alle bestehenden Policies und die API) oder `mode=simple`. Bei `simple` hält `spec` den Editor-Stand.
  `services/fw_compile.py` erzeugt daraus `content` im bestehenden Format `{address_lists, filter, nat}`,
  danach läuft `validate_content`. Push, Versionierung (`PolicyVersion.spec`), Rollback und
  `policy.render()` sind unverändert.
* **Objekte, Dienste, Zonen, Bausteine** (`fw_objects`, `fw_services`, `fw_zones`, `fw_blocks`) sind global
  (MSP) oder mandantenweit. Vordefinierte Einträge kommen aus `app/seeds/firewall.json` (`builtin`,
  `seed_key`, idempotent beim Start). Sie sind schreibgeschützt; „Kopieren“ erzeugt einen eigenen Eintrag.
  Bausteine pflegen nur Admins, globale Einträge nur der MSP. Globale Policies dürfen nur globale Einträge
  referenzieren.
* **Namen auf dem Router:**
  - Objekte werden zu `sdwan-obj-<kürzel>`; mehrere Objekte in einer Regel zu `sdwan-r-<id>-src|dst`,
    denn RouterOS erlaubt nur eine Address-List je Seite.
  - Zonen werden zu `sdwan-zone-<kürzel>`. Die Zone mit `source=wan` nutzt die bestehende Liste `sdwan-wan`
    der WAN-Konfiguration.
  - Kommentare: `r:<regel-id>` für Editor-Regeln, `base:<name>` für Grundregeln.
* **Zonen je Gerät:** `device_zone_members` (Interface → Zone), gesetzt über `PUT /devices/{id}/zones`
  (Firewall-Tab) oder die ZTP-Vorlage (`zones: {kürzel: [interfaces]}`).
  - `services/zones.py` verwaltet `/interface/list` und `/interface/list/member` mit `sdwan:zone:`.
  - Vor dem Push einer einfachen Policy legt `run_deployment` die referenzierten Listen an, denn RouterOS
    lehnt Regeln mit unbekannter Interface-List ab. Das ist rein additiv, nur für `simple`.
* **Grundregeln** (grau, nicht editierbar), in dieser Reihenfolge:
  1. **Plattform-Zugänge, immer:** Hub über `sdwan-mgmt`, Mesh-Port, VRRP, ICMP. Ein Default-Drop darf
     Plattform, Mesh und VRRP nie aussperren, und die Reihenfolge verwalteter Regeln verschiedener Tags ist
     nicht garantiert.
  2. established/related/untracked accept, invalid drop (input und forward).
  3. Verwaltungsports (`21,22,23,80,443,8728,8729,8291`) nur aus Zonen mit `management=true` bzw. über
     den Tunnel.
  4. Am Ende Default-Drop input/forward. Abschaltbar, mit Warnung.

  Nur IPv4 (`/ip firewall`); IPv6 bleibt unverändert.
* **Einfügeposition:** Verwaltete Regeln kommen per `place_first` vor die erste nicht verwaltete Regel, stehen
  also **oben** im Regelwerk. Der Editor zeigt das an.
* **Vorprüfung** (`services/fw_deploy_check.py`, Entscheidung 17):
  - Bei Default-Drop liest die Plattform die nicht verwalteten, aktiven Filterregeln (input/forward) jedes
    Zielgeräts und listet sie in Deploy-Dialog und `deploy-check`.
  - Diese Geräte werden **übersprungen** (Deployment-Ergebnis `skipped`, Status `partial`). Ausgerollt wird
    nur mit `confirm_devices`.
  - ZTP weist solche Policies gar nicht zu, weil dort keine Bestätigung möglich ist.
* **Lint** (`services/fw_lint.py`):
  - verdeckte und doppelte Regeln;
  - any→any accept;
  - leere Objekte/Dienste;
  - Zonen ohne Interface bzw. ohne WAN auf Zielgeräten;
  - Portweiterleitung ohne Forward-Regel;
  - kein Router-Zugriff bei Default-Drop;
  - Default-Drop nicht als letzte Policy;
  - Management-Zone ohne Interface (Fehler: „Lokaler Zugriff … nur noch über den Tunnel“).

  Fehler erfordern `confirm_lint` beim Deploy.
* **Vorschau:** `POST /policies/{id}/preview` liefert RouterOS-Befehle, Diff zur zuletzt ausgerollten Version
  (`make_diff`) und Lint. Liste und Editor zeigen „Änderungen nicht ausgerollt“ samt Geräten
  (`undeployed`).
* **Trefferzähler:**
  - Der Poll-Hook `fw_hits` liest alle 5 Minuten `packets`/`bytes` der `sdwan:fw:`-Regeln.
  - Die Zuordnung zur Editor-Regel läuft über den Kommentar. `fw_rule_hits` speichert je Gerät
    `last_hit_at`; ein sinkender Zähler gilt als Reset.
  - `POST /devices/{id}/firewall/reset-counters` (Techniker) setzt nur verwaltete Regeln zurück.
* **Objekte ändern:** Einfache Policies, die das Objekt (auch über Gruppen) nutzen, werden neu kompiliert und
  bekommen eine neue Version. Es gibt **kein** automatisches Deploy.
* **Umwandeln:**
  - einfach → Experte: jederzeit, `content` bleibt gleich.
  - Experte → einfach: nur mit Bestätigung. Die bisherigen Regeln werden unverändert als Rohregeln
    übernommen; Grundregeln und Default-Drop sind zunächst aus, der kompilierte Inhalt muss identisch
    bleiben.
* **Annahmen (Labor):** Address-List-Bereiche `a-b`, `protocol=vrrp`, `reset-counters` mit `.id`,
  Interface-Listen-Felder. Siehe LABORTEST und Selbsttest (`interface_list`, `interface_list_member`,
  `packets` in `fw_filter`).

### Werks-Firewall (defconf) – Nachtrag Phase 14

* Filterregeln mit Kommentar `defconf…` (MikroTik-Werkskonfiguration) meldet die Vorprüfung **gesondert**
  (`deploy-check` → `defconf` je Gerät). Der Deploy-Dialog zeigt sie als eigene Gruppe „Werks-Firewall (defconf) –
  wird durch die Grundregeln der Plattform abgedeckt“.
* Option je Deploy `disable_defconf` (Standard **an** bei einfachen Policies mit Default-Drop):
  - Nach dem erfolgreichen Push setzt die Plattform die aktiven defconf-Regeln der Chains input/forward auf
    `disabled=yes`. Sie werden nie gelöscht und gelten nicht als Hinderungsgrund.
  - Ist die Option aus, zählen sie wie manuelle Regeln (Gerät wird ohne Bestätigung übersprungen).
  - Andere nicht verwaltete Regeln verhalten sich unverändert: Standard ist Überspringen.
  - ZTP deaktiviert defconf-Regeln standardmäßig, wenn eine zugewiesene Policy Default-Drop hat.
* **Merken und Rückgängig:** `fw_defconf_disabled` speichert je Gerät die von der Plattform deaktivierten Regeln
  (`.id` + Kommentar).
  - Wird dem Gerät keine Policy mit Default-Drop mehr zugewiesen, aktiviert der nächste Push genau diese Regeln
    wieder. Das passiert beim Entfernen der Zuweisung, das ohnehin neu pusht.
  - Alternativ per Button im Firewall-Tab (`POST /devices/{id}/firewall/defconf/restore`, Audit).
  - Zuordnung beim Wiederaktivieren: zuerst über `.id` (Kommentar und Fingerabdruck müssen passen). Passt die
    Regel dort nicht (fehlt, anderer Fingerabdruck), wird per **Fingerabdruck** unter den deaktivierten
    defconf-Regeln gesucht. Der Fingerabdruck ist ein sha256 über chain, action und alle Match-Felder,
    normalisiert und ohne `.id`, Zähler, Kommentar und `disabled`. Aktiviert wird nur bei genau einem Treffer,
    z. B. nach einem Import mit neuen `.id`s. Bei mehreren Treffern wird nichts getan, und der Firewall-Tab zeigt
    „nicht eindeutig zuordenbar“ (`status=ambiguous`).
  - Regeln, die inzwischen gelöscht oder verändert wurden, und Regeln, die der Kunde selbst deaktiviert hatte,
    bleiben unberührt.
  - Bei einem atomaren Rollback werden soeben deaktivierte Regeln sofort wieder aktiviert.
* **Zonen-Vorschlag:** Die defconf-Interface-Lists `WAN`/`LAN` mit ihren Mitgliedern werden als Vorschlag für die
  Zonen WAN/LAN angeboten (`GET /devices/{id}/zones/suggestions`, Button „Vorschlag übernehmen“ im Firewall-Tab).
  Die Zone WAN folgt weiter der WAN-Konfiguration der Plattform; ihr Vorschlag dient nur zur Kontrolle.

## Phase 15 – Threat-Feeds

* `threat_feeds` (global oder mandantenweit, Seeds in `app/seeds/feeds.json`: Spamhaus DROP IPv4/IPv6 im
  JSON-Zeilen-Format) und `threat_feed_assignments` (je Gerät). Anlegen und Ändern dürfen nur Admins,
  zuweisen und laden auch Techniker. Vordefinierte Feeds lassen sich nur in Intervall, Obergrenze und
  Aktivierung ändern.
* **Laden** (`services/feeds.py`, Worker alle 5 min): nur Feeds mit Zuweisungen, fällig nach `interval_min`.
  - HTTP mit 30 s Timeout und Größenlimit (`FEED_MAX_DOWNLOAD_MB`).
  - Formate: `lines` (IP/CIDR je Zeile, Kommentarzeichen wählbar) und `jsonl` (Feld wählbar).
  - Nur **öffentlich routbare** Netze (`ip_network.is_global`, keine Multicast, keine Präfixe breiter als /8
    bzw. /16). Grund: Ein Feed mit privaten Netzen könnte Standorte aussperren.
  - Über der Obergrenze oder bei einem Fehler bleibt die **letzte gültige Liste** aktiv; nur `last_error`
    wird gesetzt.
* **Verteilung:** Address-List `sdwan-feed-<kürzel>` mit Kommentar `sdwan:feed:<kürzel>`; IPv6-Einträge in
  `/ipv6/firewall/address-list`.
  - Nur Differenzen: fehlende Einträge anlegen, überzählige entfernen.
  - Manuelle Einträge (auch in gleichnamigen Listen ohne den Kommentar) bleiben unberührt.
  - Vorher **RAM-Check**: `free-memory` ≥ Einträge × `FEED_BYTES_PER_ENTRY` + `FEED_MIN_FREE_MB`. Reicht
    der Speicher nicht, wird das Gerät übersprungen (Status `skipped_memory`, sichtbar).
  - Entzug einer Zuweisung löscht die Liste auf dem Router.
* **Firewall-Editor:** Jeder Feed hat ein Objekt vom Typ `feed`, das auf `sdwan-feed-<kürzel>` verweist.
  Solche Objekte sind nur einzeln je Regelseite und nicht in Gruppen nutzbar. Baustein „Threat-Feeds
  eingehend verwerfen“.
* **Alarmtyp `feed_stale`:** je Gerät mit zugewiesenem Feed, wenn die letzte erfolgreiche Aktualisierung
  älter als 3 × Intervall ist.
* **Annahmen (Labor):** Spamhaus-URLs und das JSON-Format; `/ipv6/firewall/address-list` mit gleichen Feldern;
  RAM-Schätzwert je Eintrag.

## Phase 16 – Compliance und Config-Suche

* **Regelsets** (`compliance_rule_sets`, global oder mandantenweit; Seed „MSP-Baseline“ in
  `app/seeds/compliance.json`, schreibgeschützt, kopierbar). Pflege durch Admins, Zuweisung
  (`compliance_assignments`, Ziele Geräte/Standorte/Tags) durch Techniker. Die Ziele werden bei jeder
  Auswertung neu aufgelöst.
* **Regeltypen** (`services/compliance.py`):
  - Text gegen das letzte Backup: `contains`, `not_contains`, `regex` (match/no_match).
  - Strukturiert, **live** gelesen: `service_disabled`, `no_user`, `ntp_enabled`,
    `service_restricted_to_tunnel`, `channel_in`, `min_version`.
  - Entscheidung: Strukturierte Prüfungen lesen live statt den Export zu parsen, weil `/export` Standardwerte
    weglässt. Ist der Router nicht erreichbar, lautet das Ergebnis `unknown`, nicht „verletzt“.
* **Auswertung:** nach jedem neuen Backup (Hook am Ende von `take_backup`, nur bei vorhandenen Zuweisungen,
  best effort) und manuell (`POST /compliance/evaluate`). Ergebnisse (`compliance_results`) bleiben
  180 Tage für den Trend.
* **Bericht:** Matrix Geräte × Regeln (`/compliance/report`, `.csv`, `.pdf` über reportlab),
  Trend je Tag (`/compliance/trend`). Der Alarmtyp `compliance_failed` gehört nicht zu den Standardregeln
  (standardmäßig aus) und wird bei Bedarf angelegt.
* **Config-Suche** (`GET /config-search`):
  - Durchsucht das jeweils letzte Backup je Gerät des Mandanten; MSP mit `all_tenants` mandantenübergreifend.
  - Geheimnisse (`password`, `private-key`, `preshared-key`, `passphrase`, `secret`, …) werden **vor** der Suche
    maskiert und sind nie Treffer.
  - Regex: max. 200 Zeichen, verschachtelte Quantoren abgelehnt, Zeitlimit 3 s (Schutz vor ReDoS).
  - Jede Suche steht im Audit-Log.
* **Annahmen (Labor):** `/system/ntp/client` mit Feld `enabled` (Selbsttest `ntp_client`).

## Phase 17 – Script-Bibliothek und Massen-Ausführung

* **Scripts** (`scripts`, global oder mandantenweit, versioniert in `script_versions`; Beispiele als Seed in
  `app/seeds/scripts.json`). Kategorien:
  - `read` (nur lesend): anlegen und ausführen ab Techniker.
  - `change` (ändernd): nur Admin/MSP-Admin.
* **Variablen:** nur die feste Liste `device.name/identity/tunnel_ip/serial/model`, `site.name`, `tenant.name/slug`
  als `{{ … }}`; eigener Ersetzer, kein Template-Motor. Unbekannte Variablen und Werte mit `"`, `\`, `$`,
  `[]`, `{}`, `;` oder Zeilenumbruch sind Fehler, damit Gerätenamen das Script nicht verändern können.
* **Warnungen** (`services/scripts.warnings`) bei Bezug auf verwaltete Objekte (`sdwan:`/`sdwan-`), bei
  Neustart/Reset und bei Änderungen an Benutzern/Diensten.
* **Ausführung** (`script_runs`/`script_run_items`, Worker alle 15 s, Muster wie Firmware-Rollout):
  1. Vorschau mit gerenderten Befehlen je Gerät.
  2. Bei `change` Pflicht-Bestätigung durch den exakten Script-Namen und Backup je Gerät (Auslöser
     `pre-script`, gepinnt). Schlägt das Backup fehl, wird auf diesem Gerät **nicht** ausgeführt.
  3. Abarbeitung gestaffelt in Gruppen. Ab `max_failures` Fehlern wird angehalten; es gibt Fortsetzen und
     Abbrechen.
* **Ausführung per SSH** (wie der Backup-Export) mit Zeitlimit 120 s. Die Ausgabe wird gespeichert (max. 64 KB)
  und ist durchsuchbar (`/script-runs/search`). Als Fehler gilt ein Exit-Status ≠ 0 oder eine
  RouterOS-Fehlermeldung in der Ausgabe (`syntax error`, `failure:` …).
* **Audit:** `script.run` mit vollem Script-Text, dazu Anlegen, Ändern und Status.
* **Annahme (Labor):** Mehrzeilige Scripts laufen als SSH-Befehl wie eingetippt; die Fehlererkennung über die
  Ausgabe greift.

## Phase 18 – Wartungsfenster, Speedtest, Syslog

* **Namen auf Routern** (`app/routeros/naming.py`): Logging-Aktionen nur `[A-Za-z0-9]` (auf Hardware bestätigt, daher
  `sdwansyslog`; eine alte Aktion `sdwan-syslog` mit Ziel Hub-IP wird beim nächsten Abgleich samt Regeln entfernt).
  Alle übrigen erzeugten Namen laufen über `routeros_safe_name()` (ANNAHME `[A-Za-z0-9._-]`, gültige Namen bleiben
  unverändert, keine Kürzung). Der Simulator lehnt ungültige Namen je Menü wie RouterOS ab. Fehler von RouterOS
  (`/pfad/add: …`, `failure: …`) zeigt die Oberfläche mit „Router hat Konfiguration abgelehnt – bitte melden“ und einem
  Link zum Selbsttest.

* **Wartungsfenster** (`maintenance_windows`, `services/maintenance.py`, Seite „Wartungsfenster“):
  - Gilt für den ganzen Mandanten, einen Standort oder ein Gerät; einmalig (Beginn + Dauer) oder wöchentlich
    (Wochentage, Uhrzeit, Dauer) in der Zeitzone des Mandanten, auch über Mitternacht.
  - `suppress_alerts`: Anliegende Bedingungen lösen während des Fensters keinen Alarm aus. Der Alarm bleibt
    „ausstehend“ mit `suppressed_reason = "maintenance:<Name>"` und wird in der Alarmliste als „Unterdrückt –
    Wartungsfenster …“ angezeigt. Besteht die Bedingung nach dem Fenster noch, wird normal alarmiert.
    Bereits ausgelöste Alarme bleiben unverändert.
  - Firmware-Rollouts mit `only_in_window`: Ein Gerät startet nur, wenn für es ein Fenster mit
    `firmware_allowed` aktiv ist; sonst bleibt es „wartet auf Wartungsfenster“. Ohne Option wie bisher.
* **Speedtest je WAN** (`speedtest_results`, `services/speedtest.py`, Karte im WAN-Tab):
  - `/tool bandwidth-test` gegen einen btest-Server (`SPEEDTEST_SERVER`, `SPEEDTEST_USER/PASSWORD`,
    `SPEEDTEST_DURATION_S`). Der Hub ist Linux und **kein** btest-Server; ohne Konfiguration ist die Funktion
    deaktiviert (mit Erklärung).
  - Je WAN eine vorübergehende /32-Route zum Server über das Gateway dieses WAN (Kommentar
    `sdwan:speedtest:<slot>`, bei DHCP das Gateway aus `/ip dhcp-client`). Sie wird nach dem Test immer
    entfernt; Reste früherer Läufe werden vor jedem Test aufgeräumt.
  - Vor dem Start: geschätzter Datenverbrauch (Dauer × letzte gemessene Rate, sonst 100 Mbit/s je Richtung).
    Bei WAN-Links mit Monatslimit ist eine ausdrückliche Bestätigung nötig (`confirm_volume`, sonst 409).
  - Wöchentliche Planung je WAN (`wan_links.speedtest_weekly`, Standard aus), täglicher Worker-Job (03:30) startet Tests, deren letzte Messung ≥ 7 Tage alt ist.
  - Annahme (Labor): Antwortfelder `rx-total-average`/`tx-total-average`, Richtungen `receive`/`transmit`.
* **Zentrales Syslog** (`device_syslog`, `syslog_messages`, `services/syslog.py`, Tab „Log“):
  - Opt-in je Gerät (Standard aus). Beim Aktivieren legt die Plattform `/system logging action`
    `sdwansyslog` (target=remote, remote=Hub-Tunnel-IP, Port `SYSLOG_PORT`, src-address=Tunnel-IP) und je
    gewähltem Topic eine Regel `/system logging` mit `action=sdwansyslog` an. Erkannt werden die verwalteten
    Einträge über den Aktionsnamen (Annahme: Aktionen/Regeln haben kein Kommentarfeld). Andere Aktionen und
    Regeln bleiben unangetastet.
  - Empfänger `app/syslog_receiver.py` als eigener Container `syslog` im Netz-Namespace des Hubs
    (`network_mode: service:wireguard-hub`), UDP. Zuordnung über die Quell-IP = Tunnel-IP eines Geräts mit
    aktivem Syslog; alles andere wird verworfen.
  - Aufbewahrung je Mandant (`tenant.settings.syslog_retention_days`, Standard 30 Tage, `PUT /syslog/retention`),
    täglicher Lösch-Job.
  - Tab „Log“: Filter nach Text, Topic, Schweregrad und Zeit. `?tab=log&around=<Zeit>` zeigt ±15 Minuten; Absprung
    „Log um diesen Zeitpunkt“ aus dem VRRP-Verlauf und aus den Metriken (Backup-Markierung).

## Phase 19 – WLAN-Verwaltung

* **Erkennung** (`services/wlan.detect`, Poll-Hook alle 10 min → `facts.wlan`): zuerst `/interface/wifi/print`
  (Paket `wifi`, RouterOS 7), sonst `/interface/wireless/print` (altes Paket `wireless`). Beide fehlen → kein WLAN,
  `facts.wlan` leer, **kein WLAN-Tab**. Dazu Radios mit Bändern (`/interface/wifi/radio`), CAPsMAN-/CAP-Rolle und
  Anzahl Clients. Nur lesende Befehle.
* **Konfiguriert wird nur `wifi`.** Vor jedem Abgleich wird der Treiber neu erkannt; bei `wireless` oder ohne Paket
  wird **kein** Schreibbefehl gesendet (Status „wireless-Treiber: nur Anzeige“ bzw. „kein WLAN“; Test).
* **Profile** (`wlan_profiles`, mandantenweit, da sie Schlüssel enthalten): SSID, WPA2/WPA2+3/WPA3-PSK oder
  WPA2/WPA3-Enterprise mit RADIUS (Server, Port, Secret verschlüsselt), Band, Kanalbreite, Ländercode, VLAN, Bridge,
  Client-Isolation, versteckt, täglicher Zeitplan, Gäste-WLAN mit optionaler PSK-Rotation.
  - **Ländercode:** `tenants.country_code` (Standard `AT`, Seite Mandanten), je Profil überschreibbar. Auf dem Router
    als Ländername (`country=Austria`, Normtabelle `COUNTRIES`).
  - Das Kürzel ist Teil der Router-Objektnamen und nach dem Anlegen fest.
* **Zuweisung** (`wlan_assignments`): Geräte/Standorte/Tags (gemeinsame `resolve_targets`), Modus `local` oder
  `capsman`.
* **Ausrollen** nur auf Knopfdruck (wie Policies): Änderungen erhöhen die Version; die Liste zeigt „Änderungen nicht
  ausgerollt“ mit den betroffenen Geräten. Ein Abgleich stellt je Gerät **alle** zugewiesenen Profile her und
  entfernt nicht mehr zugewiesene (Status je Gerät/Profil in `wlan_device_states`; die Zeile bleibt bis zum
  erfolgreichen Entfernen stehen).
  - Router-Objekte `sdwan-wifi-<kürzel>` in `/interface/wifi/security`, `/datapath`, `/channel` (nur bei fester
    Kanalbreite) und `/configuration`.
  - **Lokal:** je passendem Radio ein **virtueller AP** (`/interface/wifi add master-interface=<Radio>`, Kommentar
    `sdwan:wifi:<kürzel>:<radio>`). Physische Radios und vorhandene WLANs werden nie verändert. Ist ein Radio
    deaktiviert, zeigt der Status einen Hinweis.
  - **CAPsMAN:** Konfiguration auf dem Controller plus `/interface/wifi/provisioning` je Band (Kommentar
    `sdwan:wifi:prov:<band>`, erstes Profil `master-configuration`, weitere `slave-configurations`), **hinter**
    vorhandenen Regeln angefügt – bestehende CAPs werden weiter wie bisher provisioniert. CAPs selbst werden nicht
    automatisch umgestellt (Gerät verliert dabei seine lokale WLAN-Konfiguration); das bleibt ein manueller Schritt.
  - **Zeitplan:** `/system/scheduler` `sdwan-wifi-<kürzel>-on|off` (täglich, `interval=1d`), aktiviert/deaktiviert
    die virtuellen APs des Profils. Wochentage werden nicht unterstützt.
  - **RADIUS:** `/radius` mit `service=wireless`, Kommentar `sdwan:wifi:<kürzel>`.
* **Status** (Tab „WLAN“, live): Radios, Kanal (`/interface/wifi/monitor … once`), Clients aus der
  Registration-Table (MAC, Signal, Raten, Verbindungsdauer), CAPs am Controller. **Keine Nachbarnetze**: ein
  Scan würde verbundene Clients trennen.
* **Gäste-PSK:** „PSK rotieren“ erzeugt ein gut lesbares PSK und rollt es aus; optional automatisch alle N Tage
  (Worker täglich 04:45). Der Aushang (`/print/wlan/<id>`, A4 ohne Navigation) zeigt SSID, PSK und einen QR-Code
  (`WIFI:T:WPA;S:…;P:…;;`, im Backend mit `segno` als SVG erzeugt). Zugangsdaten abrufen: ab Techniker,
  protokolliert (`wlan.credentials.view`); Audit-Einträge enthalten nie PSK oder Secret.
* **Rechte:** Profile anlegen/ändern/löschen Admin; zuweisen, ausrollen, rotieren, Zugangsdaten Techniker+;
  lesen alle.
* **Annahmen (Labor):** Pfade und Feldnamen des `wifi`-Pakets (siehe `services/wlan.py` und `PATH_SPECS`
  `wifi*`), Ländernamen, Werte für `width`/`supported-bands`, `monitor once` ohne Scan. Pfade mit `package` im
  Selbsttest gelten als „Paket nicht vorhanden“ statt als Fehler.

## Phase 20 – Hotspot / Gäste-Portal

* **Unabhängig vom AP-Hersteller:** Der Hotspot läuft auf dem MikroTik (`/ip hotspot`) auf einem Interface oder VLAN.
  Access-Points jeder Marke dahinter liefern nur den Funk; Anmeldung, Freigabe und Limits macht der Router.
  Voraussetzung: das Interface hat eine IP-Adresse und DHCP im Gästenetz (`address-pool=none`).
* **Portale** (`hotspot_portals`, global oder mandantenweit): Logo (PNG/JPEG/WebP als Data-URL), Farben, Texte DE/EN,
  Nutzungsbedingungen mit Pflicht-Checkbox, Anmeldeart und Formularfelder. Vorlagen **Hotel** (Voucher),
  **Gastronomie** (Klick), **Veranstaltung** (Formular: Name), **Büro-Gäste** (Formular: Name, Firma, Ansprechpartner)
  sind Seed-Daten (`app/seeds/hotspot.json`), schreibgeschützt, zum Anpassen kopieren. Kein PMS-Anschluss.
  - Login-Seiten (`login.html`, `status.html`, `alogin.html`, `logout.html`) erzeugt `services/hotspot.render_pages`
    aus dem Portal; Portal-Texte werden HTML-escaped und `$` entschärft (keine RouterOS-Variablen einschleusbar).
    Sprache per `<html lang>` und CSS, umschaltbar DE/EN. Eigene Login-Seiten können hochgeladen werden (Text, je
    Datei max. 100 KB, mind. `login.html`) und ersetzen die erzeugten.
  - Vorschau im Designer: dieselben Seiten mit Beispielwerten (`/hotspot/preview`), in einer Sandbox ohne Netzzugriff.
* **Hotspots** (`hotspot_instances`) je Gerät + Interface; Router-Objekte `sdwan-hs-<kürzel>`:
  `/ip/hotspot/profile` (`hotspot-address` = IP des Interfaces oder fest, `html-directory`, `login-by`),
  `/ip/hotspot`, `/ip/hotspot/user/profile` (`…-trial`, `…-v-<voucherprofil>`: `rate-limit`, `shared-users`,
  `idle-timeout`), `/ip/hotspot/walled-garden` (Hosts), Upload der Login-Seiten per SFTP nach `sdwan-hs-<kürzel>/`.
  Ausrollen auf Knopfdruck; Hinweis „Änderungen nicht ausgerollt“, wenn Hotspot oder Portal seither geändert wurden.
  Entfernen löscht alle `sdwan-hs-<kürzel>`-Objekte; Standardprofile und fremde Einträge bleiben.
* **Anmeldearten:**
  - **Voucher:** Benutzer = Code, leeres Passwort (`http-pap`); Voucher als `/ip/hotspot/user` mit `limit-uptime`
    und optional `limit-bytes-total`.
  - **Klick:** Trial-Login (`login-by=…,trial`, `trial-uptime-limit` = Sitzungsdauer, Benutzer `T-<MAC>`).
  - **Formular:** Die Seite sendet die Felder per `fetch` an `POST /api/v1/portal/<id>/register` (öffentlich), danach
    Trial-Login. Die Plattform-Adresse wird dafür automatisch in `/ip/hotspot/walled-garden/ip` (`dst-host`)
    aufgenommen.
* **Öffentlicher Endpunkt `/portal/<id>/register`** (Entscheidung 19): Rate-Limit je Quell-IP und Portal
  (10 je 10 min, Redis, sonst im Prozess), Body max. 4 KB, nur die im Portal definierten Felder mit Typ-/Längenprüfung
  (unbekannte werden verworfen), Pflicht-Zustimmung zu den Nutzungsbedingungen, CORS nur für diesen Endpunkt.
  **Walled Garden und HTTPS:** Bei HTTPS kann der Walled Garden nur nach Host freigeben, nicht nach Pfad – Gäste
  erreichen damit auch die Plattform-Oberfläche (weiterhin anmeldegeschützt). Deshalb gibt es ohne Anmeldung nur
  diesen einen Endpunkt.
* **Voucher:** Profile (Online-Zeit, Datenlimit, Bandbreite, Geräte je Voucher), Stapel (1–500, Codes aus 8 Zeichen ohne
  verwechselbare Zeichen), A4-Druckansicht (`/print/vouchers/<stapel>`, 10 Karten je Seite, QR-Code mit
  `http://<dns-name|adresse>/login?username=<code>&password=`), CSV, Status (neu/aktiv/verbraucht/gesperrt) alle 5 min
  aus `/ip/hotspot/user` (`uptime`, `bytes-in/out`), Sperren = `disabled=yes`.
* **Live-Gäste** aus `/ip/hotspot/active` (nicht gespeichert): Trennen (Eintrag entfernen), Sperren (Voucher deaktivieren
  bzw. MAC per `/ip/hotspot/ip-binding type=blocked`), Entsperren.
* **DSGVO:** Gespeichert werden nur die Formularfelder (`guest_registrations`), keine Browser-/Gerätedaten, keine
  MAC/IP. Aufbewahrung je Mandant (`tenant.settings.guest_retention_days`, Standard 30 Tage), täglicher Lösch-Job.
  Registrierungen sehen nur Admins (protokolliert). Hinweis im Designer: Nutzungsbedingungen und Datenschutz
  verantwortet der Betreiber.
* **Firewall-Editor:** Betreibt ein Zielgerät einen Hotspot und verwirft keine Regel Verkehr aus der Zone „Gäste“
  (Kürzel `guest`) in eine andere Zone, schlägt Lint den Baustein „Gäste vom LAN isolieren“ vor (Hinweis).
* **Annahmen (Labor):** Pfade/Feldnamen (`PATH_SPECS` `hotspot*`), Trial-Benutzer `T-<MAC>`, `http-pap` mit leerem
  Passwort, Login per GET-Parametern (QR), Upload-Ziel des `html-directory`, `walled-garden/ip dst-host`,
  Variablen der Login-Seiten.

## Offboarding (Gerät aus der Verwaltung nehmen)

* Gerät entfernen öffnet einen Dialog mit zwei Wegen (`POST /devices/{id}/offboard`):
  - Admin/MSP-Admin, Bestätigung durch den exakten Gerätenamen, Audit `device.offboard` mit allen Schritten.
  - Vorschau `GET /devices/{id}/offboarding/preview` zeigt Anzahlen je Kategorie und Warnungen, z. B.
    „keine eigene Default-Route“ oder „kein eigenes Masquerade“.
  - **Router bereinigen und entfernen** (`clean`, Standard, nur online) – feste Reihenfolge
    (`services/offboarding.py`), jeder Schritt protokolliert:
    1. Backup (Auslöser `offboarding`).
    2. Von der Plattform deaktivierte defconf-Regeln wieder aktivieren – zuerst, damit der Router nie ohne
       Firewall dasteht. Nicht eindeutig zuordenbare Regeln führen zum Abbruch.
    3. Verwaltete Objekte (Kommentar `sdwan:` bzw. Name `sdwan-`) entfernen: Firewall (verwerfende Regeln zuerst),
       NAT/Mangle ohne WAN, Address-Lists inkl. Feeds, Hotspot, WLAN (nur virtuelle APs; Radios bleiben), RADIUS,
       Syslog, Scheduler, Scripts, VRRP, Mesh, DNS-Einträge und Zonen-Listen.
    4. Von Fernzugriffen geänderte Dienste (www/winbox/ssh) auf den gemerkten Ursprungszustand.
    5. Temporäre Fernzugriffs-Benutzer und die Gruppe `sdwan-remote` entfernen; offene Sitzungen werden
       geschlossen.
    6. Zuletzt ein einmaliger Scheduler `sdwan-offboard` auf dem Router: Start = Router-Uhr + 30 s,
       `interval=1m` als Wiederholung, jeder Befehl in `:do {} on-error={}`.
       - Er entfernt alles, wovon der Management-Tunnel abhängt: WAN-Netwatch, -Mangle, -NAT, -Routen,
         Routing-Tabellen, Liste `sdwan-wan` und ZTP-LAN.
       - Danach API-Benutzer, Gruppe `sdwan-api` und den Management-Tunnel (Adresse, Peer, Interface).
       - Zum Schluss entfernt er sich selbst.
       - Grund: Diese Objekte über die API zu entfernen, würde den Zugang vor dem Ende kappen.
    - Schlägt ein Schritt vor 6 fehl, wird abgebrochen: Das Gerät bleibt in der Plattform, und der Dialog zeigt
      das Protokoll.
  - **Nur aus der Plattform entfernen** (`platform_only`): Der Router bleibt unverändert. Eine deutliche Warnung
    weist darauf hin, dass verwaltete Konfiguration, API-Benutzer, Tunnel und deaktivierte defconf-Regeln bestehen
    bleiben. Archiviert wird das letzte vorhandene Backup.
* **Archiv** (`offboarding_archives`, Seite „Offboarding-Archiv“): Backup und Protokoll bleiben nach dem Löschen
  90 Tage beim Mandanten als Download. Ein täglicher Job löscht sie danach.
* Nicht automatisch zurückgestellt:
  - DNS-Server-Einstellungen des Content-Filters (Hinweis in der Vorschau).
  - Die Adressbeschränkung des `api`-Dienstes aus dem Onboarding (Ursprungszustand unbekannt).
  - Importierte Zertifikate.
* Das bisherige `DELETE /devices/{id}` bleibt unverändert für noch nicht gepairte Geräte; die Oberfläche nutzt für
  gepairte Geräte den Offboarding-Dialog.
* **Annahme (Labor):** Script-Syntax `remove [find where comment~"^sdwan:"]`. Der Scheduler läuft weiter, nachdem
  API-Benutzer und Tunnel entfernt wurden.

## Phase 21 – Plattform-Sicherung und Disaster Recovery

* **`app/platform_backup.py`** (CLI `python -m app.platform_backup run|list|restore`) läuft im **Worker**. Nur er
  bindet `.env` und das Hub-Volume lesend sowie das Zielverzeichnis ein.
  - Täglicher Job `PLATFORM_BACKUP_HOUR_UTC`:10.
  - „Jetzt sichern“ in der Oberfläche legt eine Anforderung an (`status=queued`), die der Worker binnen einer Minute
    ausführt.
  - `deploy/backup.sh` ruft die CLI im Worker auf.
* **Archiv** `sdwan-platform-<ts>.tar.gz.age`: `pg_dump -Fc`, `.env`, Hub `hub.key`/`wg0.conf`, optional
  `influx backup`, `manifest.json` (Migrationsstand, Hub-Endpoint, sha256 je Teil).
  - Es gibt keine Datei-Uploads; Hotspot-Logos und -Seiten liegen in der DB.
  - Verschlüsselung mit `pyrage` (age) für `PLATFORM_BACKUP_AGE_RECIPIENT`. Ohne Public Key gibt es **keine**
    Sicherung, sondern Status „nicht konfiguriert“, weil `.env` Schlüssel enthält.
* **Ziele:** lokal (`PLATFORM_BACKUP_DIR`, Aufbewahrung `PLATFORM_BACKUP_KEEP_DAYS`), optional per rclone auf
  S3-kompatiblen Speicher oder SFTP (`PLATFORM_BACKUP_RCLONE_REMOTE`, Konfiguration in `deploy/rclone`).
* **Status:** `platform_backups` (ohne Mandant), Seite „Plattform-Sicherung“ (nur MSP-Admin, `SuperCtx`).
* **Alarm `platform_backup_failed`:** eigene Tabelle `platform_alerts` (ohne Mandant, `services/platform_events.py`).
  - Je Typ höchstens ein aktiver Alarm; Mail an alle MSP-Admins, optional `PLATFORM_WEBHOOK_URL`.
  - Nach der nächsten erfolgreichen Sicherung behoben.
  - Der mandantenbezogene Alarm-Mechanismus bleibt unverändert.
* **Wiederherstellung** `deploy/restore.sh` auf einem frischen Server:
  1. Entschlüsseln und Prüfsummen kontrollieren.
  2. `.env` zurück.
  3. Hub-Schlüssel ins Volume.
  4. `pg_restore`.
  5. Stack starten.

  Gleicher Hub-Endpoint (DNS) und Hub-Schlüssel: Die Router verbinden sich ohne Eingriff wieder.
* **Image:** `postgresql-client-16` (PGDG) und `rclone`. Anleitung inkl. Schlüsselverwahrung und
  Testwiederherstellung: `docs/DISASTER-RECOVERY.md`.
* **Annahmen (Labor):** PGDG-Paket im Image, rclone-Ziele, `influx backup`/`restore` im Container.

## Phase 22 – Zwei-Faktor-Anmeldung (TOTP)

* **TOTP:** eigene Implementierung `app/totp.py` nach RFC 6238 (SHA1, 30 s, 6 Stellen; getestet mit den
  RFC-Testvektoren), ±1 Zeitschritt Toleranz. Der zuletzt verwendete Schritt wird gespeichert, damit ein Code
  nicht zweimal gilt.
  - Geheimnis verschlüsselt (`encrypt_secret`).
  - 10 Wiederherstellungscodes (`xxxx-xxxx`), gespeichert nur als sha256, je einmal gültig.
* **Anmeldung:**
  - `POST /auth/login` liefert bei aktiver 2FA `mfa_token` (JWT `typ=mfa`, 5 min). Danach `POST /auth/login/2fa`
    mit TOTP oder Wiederherstellungscode.
  - Ist 2FA Pflicht, aber nicht eingerichtet: `setup_token` (`typ=mfa_setup`, 15 min). Mit ihm laufen
    `/auth/2fa/setup` (QR per `segno`) und `/auth/2fa/enable`; erst dann gibt es das Access-Token.
  - Schritt-Tokens werden von `deps` nie als Access-Token akzeptiert.
* **Pflicht:**
  - Je Mandant `tenant.settings.require_2fa`, über `PUT /tenants/current/security` (Admin) bzw. die Benutzerseite.
  - Für MSP-Admins immer, über `MFA_ENFORCE_SUPERUSER` (Default true; in den Tests aus).
  - Die Pflicht greift bei der **nächsten** Anmeldung; bestehende Sitzungen bleiben gültig.
  - Bei Pflicht lässt sich 2FA nicht deaktivieren. Sonst ist Deaktivieren nur mit gültigem Code möglich.
* **Fehlversuche:**
  - Passwort und 2FA zählen je Konto. Ab `LOGIN_MAX_FAILURES` (5) folgt eine Sperre für `LOGIN_LOCK_MINUTES` (15).
  - Zusätzlich ein IP-Limit auf Fehlversuche (`LOGIN_IP_LIMIT`/`LOGIN_IP_WINDOW_S`, Redis oder im Prozess,
    `app/ratelimit.py`).
  - Audit: `auth.login_failed`, `auth.2fa_failed`, `auth.locked`, `auth.login_blocked`, `auth.recovery_code_used`,
    `auth.2fa_enabled/disabled/reset`, `auth.unlock`.
* **Verwaltung:**
  - MSP-Admin setzt 2FA zurück (`POST /users/{id}/2fa/reset`, Audit und Plattform-Webhook).
  - Admin/MSP heben Sperren auf (`POST /users/{id}/unlock`).
  - Notfall mit Server-Zugriff: `python -m app.cli reset-2fa|unlock <email>` (Audit, Webhook; siehe
    DISASTER-RECOVERY.md).
* **Oberfläche:** zweistufige Anmeldung mit erzwungener Einrichtung, Profil-Menü „Zwei-Faktor“ (einrichten,
  Codes neu erzeugen, deaktivieren), Benutzerliste mit 2FA-Status, „2FA zurücksetzen“ und „Sperre aufheben“.
* Kein SSO / kein Identity-Provider.

## Phase 23 – Sicherheitsmeldungen und Mindestversionen

* **Modell** `security_advisories` (global): Kennung/CVE, Titel, Beschreibung, Funktion, Schweregrad, Link,
  aktiviert.
  - Funktionen: general, hotspot, wlan, vrrp, wireguard, dns, rest-api, api, winbox, www, ssh, other.
  - Schweregrade: low bis critical.
  - Versionsbereich: `affected_from` (inklusive), optional `affected_to` (inklusive) bzw. `fixed_in` (exklusiv).
  - Pflege nur durch MSP-Admins, kein Scraping.
  - Der Seed enthält nur ein **deaktiviertes Beispiel**; echte Versionsbereiche werden nicht geraten.
* **Versionsvergleich** (`services/advisories.parse_version`): `7.15.3`, `7.16rc2`, `7.16beta1`
  (beta < rc < final).
* **Aktive Funktionen je Gerät:**
  - general/api/wireguard immer (Management-Tunnel und API).
  - hotspot, wlan, vrrp aus den verwalteten Objekten der Plattform.
  - winbox/ssh/www/rest-api aus `facts.services`: Der Info-Poll liest `/ip/service` alle 10 min.
  - Unbekannt: Status „möglicherweise betroffen“, nur Anzeige, kein Alarm.
* **Anzeige:** Geräteliste (Pill in der RouterOS-Spalte), Gerätedetail (Karte mit „behoben ab“), Firmware-Seite
  (Spalte „Sicherheit“), Dashboard-Kachel, Seite „Sicherheitsmeldungen“ mit Anzahl betroffener Geräte.
* **Alarmtyp `security_advisory`:** gerätebezogen, nur „betroffen“, ab Schweregrad `min_severity` (Default high).
  Wie `compliance_failed` nicht in den Standardregeln, sodass bestehende Mandanten unverändert bleiben.
* **Blockade:** Hotspot anlegen/ausrollen (409) und WLAN-Ausrollen je Gerät (Status „Sicherheitsmeldung“) werden
  verweigert, wenn eine aktive Meldung mit Schweregrad high/critical für die Funktion (oder `general`) die
  Geräteversion betrifft.
  - Hinweis „erst Firmware aktualisieren“ mit „behoben ab“.
  - Entfernen bleibt immer erlaubt.
  - Ohne bekannte Version wird nicht blockiert.
* **Compliance:** Regeltyp `no_security_advisory` (Plattform-Daten statt Router-Abfrage), in der MSP-Baseline.

## Phase 24 – Vor-Ort-Zugang (Break-Glass) und API-Tokens

* **Ziel:** Techniker kommen auch ohne Plattform und ohne Tunnel an jeden Router – mit einem eigenen Passwort je
  Gerät und nur von lokal (LAN/Management oder Service-Port), nie aus dem WAN.
* **Modell `local_access`** (je Gerät, `TenantScoped`): Status `pending | active | not_created | error | disabled`
  mit Grund, Benutzername, Passwort (Fernet), gesetzt/angezeigt/Rotation fällig, verwendete Netze und Interfaces,
  manuelle Netze, Service-Port-Konfiguration sowie gemerkte Vorzustände (MAC-WinBox, `/ip service`, Bridge-Port).
* **Router-Objekte** (Kommentar `sdwan:local`, Service-Port `sdwan:local:sp`):
  - Gruppe `sdwan-local` mit `LOCAL_POLICIES_FULL` (routeros/schema.py: `local, ssh, ftp, reboot, read, write,
    policy, test, winbox, password, web, sniff, sensitive, romon` – ohne telnet/api/rest-api). Angelegt im
    Pairing-Script (Onboarding und ZTP, lokal als Admin). Fehlt die Gruppe, legt der API-Benutzer sie nachträglich mit
    der Schnittmenge aus dieser Liste und den Rechten der API-Gruppe an; `missing_policies` hält fest, was fehlt
    (orangener Hinweis mit Terminal-Einzeiler `schema.local_group_command()`, Compliance-Warnung). Eine vorhandene
    Gruppe wird nicht verändert.
  - Benutzer `<Name je Mandant, Default localadmin>` mit Zufallspasswort (24 Zeichen ohne 0/O/l/1/I).
  - Interface-Liste `sdwan-local-access` mit den lokalen Interfaces.
  - `/tool/mac-server/mac-winbox allowed-interface-list=sdwan-local-access`.
* **Lokale Netze:** Interfaces der Zonen Management/LAN (Plattform), sonst der defconf-Liste `LAN`. Ausgeschlossen
  sind WAN-Interfaces (WAN-Konfiguration, Listen `sdwan-wan`/`WAN`, DHCP-Clients). Netze aus `/ip/address` dieser
  Interfaces, plus manuell angegebene Netze (keine 0.0.0.0/0, keine Überschneidung mit WAN-Netzen) und das
  Service-Port-Netz. Gibt es keine: Status `not_created` mit Grund, es wird nichts angelegt.
* **Ausnahme privates WAN-Netz** (Doppel-NAT, VRRP-Backup mit WAN = lokales Netz): Ein manuelles Netz, das sich mit
  einem WAN-Netz überschneidet, ist nur erlaubt, wenn es vollständig in RFC1918 liegt (10/8, 172.16/12, 192.168/16)
  und je Netz ausdrücklich bestätigt wird (`allow_wan_networks`, sonst 409 mit `confirm_wan`). Öffentliche Netze und
  0.0.0.0/0 bleiben verboten. Gespeichert in `wan_exceptions` (Netz, Interface, WAN-Netz, bestätigt von/am), Audit
  `local_access.wan_exception`. Firewall: eine Regel je Ausnahme `chain=input action=accept protocol=tcp
  dst-port=22,8291 in-interface=<WAN> src-address=<Netz>` (Kommentar `sdwan:local:wan:…`), ganz oben in der
  Filter-Tabelle; das WAN-Interface kommt nie in `sdwan-local-access` (keine MAC-WinBox, keine Zonen-Freigabe).
  Gerätedetail: Hinweis „Vor-Ort-Zugang aus WAN-Netz X erlaubt“; Compliance `local_admin_present`: Warnung.
* **Adressbeschränkung** (Mandanten-Einstellung, Default an): `address=` = lokale Netze. Aus: `address=` leer,
  dafür `/ip service` winbox/ssh auf lokale Netze + Hub-IP (Vorzustand gemerkt, beim Deaktivieren zurück).
* **Firewall:** Grundregeln `base:local-access` (tcp 22,8291) und `base:local-access-dhcp` (udp 67) aus
  `in-interface-list=sdwan-local-access`, vor allen Policy-Regeln. Die Liste wird mit den Zonen immer angelegt und
  ist ohne aktiven Zugang leer – bestehende Geräte verhalten sich unverändert.
* **Service-Port (optional, Default aus):** Ethernet-Port aus der Bridge (Vorzustand gemerkt), Adresse `.1` des
  Netzes (Default 192.168.254.0/29, änderbar), Pool, DHCP-Server `sdwan-local-sp`, DHCP-Netz; Aufnahme in die Liste.
* **Lebenszyklus:** Datensatz `pending` beim Pairing (Onboarding/ZTP, abschaltbar je Mandant), angelegt vom
  Post-Poll-Hook; für bestehende Geräte per Button (Gerätedetail) oder Massenaktion (Geräteliste, Seite
  „Vor-Ort-Zugang“).
* **Passwort anzeigen:** nur Admin/MSP-Admin, nie per API-Token, Begründung Pflicht, Audit `local_access.reveal`,
  Plattform-Webhook und Mandanten-Webhook. Optional „nach Anzeige rotieren“ (4 h).
* **Rotation:** manuell, nach Anzeige oder alle n Tage (Worker stündlich). Ändert nur diesen Benutzer; bei einem
  Router-Fehler bleibt das alte Passwort gespeichert und gültig.
* **Export:** KeePass-kompatible CSV (Group, Title, Username, Password, URL, Notes), nur verschlüsselt: age
  (Public Key) oder AES-ZIP (`pyzipper`). Automatischer Export per Webhook nach Anlegen/Rotation nur age-verschlüsselt.
* **Offboarding:** Option „Vor-Ort-Zugang behalten“ (Default). Behalten: Kommentare ohne `sdwan:`, Service-Port in
  die defconf-Liste `LAN`. Entfernen: MAC-WinBox, Dienst-Adressen und Bridge-Port zurück, Objekte entfernt das
  Offboarding-Script.
* **Compliance:** Regeltyp `local_admin_present` (MSP-Baseline) schlägt bei fehlendem, ausstehendem oder nicht
  anlegbarem Zugang fehl; bei eingeschränkter Gruppe Status `warn` („Warnung“, zählt als bestanden, nicht als
  Verstoß). `service_restricted_to_tunnel` toleriert die Netze eines aktiven Zugangs.
* **API-Tokens (`api_tokens`):**
  - Bearer `sdw_…`, gespeichert nur als sha256; Klartext einmal bei der Erstellung.
  - Scope `read` (nur lesend, schreibende Methoden 403) oder `role` (Rechte der Rolle); Ablauf 1–365 Tage.
  - Letzte Nutzung mit IP; widerrufbar durch den Besitzer oder Admin.
  - Schreibende Aufrufe: Audit `api_token.use`.
  - Gesperrt (`Ctx.forbid_token`): Vor-Ort-Passwörter anzeigen/exportieren, Vor-Ort-Einstellungen, 2FA
    (einrichten, abschalten, Codes, Reset, Mandanten-Pflicht), Token erstellen/widerrufen.
  - OpenAPI: Security-Scheme `Bearer` mit Beschreibung (`/docs`).

## Phase 25 – Nachbarn, Top-Verbraucher, Inventar, ZTP-Import

* **Nachbarn:** Poll-Hook `neighbors.neighbor_poll_hook` liest `/ip/neighbor` alle 10 min; der Post-Poll-Hook
  ersetzt die Einträge je Gerät in `device_neighbors` (höchstens 500). `facts.neighbor_count` steuert die Sichtbarkeit
  des Tabs „Nachbarn“. Zuordnung zu Plattform-Geräten über Identity oder eine Adresse des Geräts.
  Standort-Topologie: `GET /sites/{id}/topology`, SVG im Dialog „Topologie“ der Standortliste.
* **Top-Verbraucher (opt-in je Gerät, `device_flows`):**
  - Router: `/ip/traffic-flow` (enabled, interfaces = WAN-Interfaces der WAN-Konfiguration; Vorzustand gemerkt) und
    `/ip/traffic-flow/target` (Hub-IP, `FLOW_PORT` 2055, `version=ipfix`, Kommentar `sdwan:flow`).
  - Collector `app/flow_collector.py` (Container `flows`, `network_mode: service:wireguard-hub`): eigener
    IPFIX-Parser (RFC 7011, Templates je Quelle/Domain/ID, IEs 1/2/8/12/10/14 und IPv6 27/28), Zuordnung per
    Tunnel-IP, nur Geräte mit aktivem Export.
  - Speicherung als 5-Minuten-Aggregate `flow_aggregates` (WAN, lokaler Host, Gegenstelle, Bytes, Pakete) – keine
    Ports, keine Einzel-Flows; je Gerät/Intervall höchstens 200 Paare, Rest „andere“.
  - Auswertung `GET /devices/{id}/flows/top?period=1h|24h|7d&wan=`: Top-Hosts und Top-Ziele.
  - Aufbewahrung `tenant.settings.flow_retention_days` (Default 7), Lösch-Job täglich.
* **Inventar:** `device_inventory` (Kaufdatum, Garantie bis, Lieferant, Notizen), Seriennummer/Modell aus dem Gerät.
  EOL-Liste `eol_models` (global, Seed nur als deaktiviertes Beispiel, Pflege durch MSP), exakter Modellvergleich.
  Hinweise: EOL, Garantie abgelaufen bzw. endet in ≤ 60 Tagen. `GET /inventory`, `GET /inventory.csv` (Semikolon,
  UTF-8 mit BOM), `PUT /devices/{id}/inventory`.
* **ZTP-Massenimport:** `POST /ztp/import/preview` (nur prüfen) und `POST /ztp/import/commit` (`confirm=true`,
  erneute Prüfung, nur gültige Zeilen). Spalten `name; serial` (Pflicht), `model; site; template; tags;
  vrrp_local_address`. Prüfung: Format, Duplikate in der Datei, bereits registrierte Seriennummern, doppelte
  Gerätenamen, unbekannte Standorte/Vorlagen, VRRP-Adresse. Angelegt über `ztp_import.stage_device` (dieselbe Logik
  wie „Geräte vorbereiten“).

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
  * Phase 13: `GET/POST /devices/{id}/selftest`, `POST /devices/{id}/reboot`, `POST /devices/{id}/restrict-api-user`,
    `GET /devices/{id}/addresses`, `POST /devices/{id}/vrrp/{inst}/ping`. Migrationen 0016–0019.

  Bestehende Endpunkte blieben unverändert.
* **Kontrast:** Badge-Text auf Badge-Grund ≥ 4,5:1 in beiden Themes, Sekundärtext ≥ 4,6:1. Primär-Buttons
  nutzen das eigene Token `--btn-primary` (#2563EB in beiden Themes, weißer Text 5,2:1); `--blue` selbst
  bleibt der Designwert und wird weiter für Linien, Diagramme und Fokusrahmen genutzt.
