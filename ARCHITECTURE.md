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
