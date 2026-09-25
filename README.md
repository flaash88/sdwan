# MikroTik-Fleet-Management

Selbst gehostetes, mandantenfähiges **Konfigurations- und Flottenmanagement für MikroTik-Router**
(RouterOS 7). Gebaut für MSPs, die viele Standorte zentral betreiben: Konfiguration ausrollen,
Router per Zero-Touch in Betrieb nehmen, sichern, aktualisieren und aus der Ferne erreichen.
Betrieb per Docker Compose (Hetzner-vServer, Proxmox-VM/LXC).

Die Plattform ist **kein allgemeines RMM** (keine Clients, Server, Tickets) und **kein reines
SD-WAN-Produkt**. WAN-Failover, VRRP und Site-to-Site-VPN-Mesh sind Funktionen unter mehreren:

* **Konfiguration & Rollout:** Firewall-Policies mit Versionierung und Rollback, Zero-Touch-Provisioning
  per Template, Content-Filter (NextDNS).
* **Betrieb der Flotte:** Tägliche Backups mit Diff, Firmware-Updates in Batches, Fernzugriff
  (SSH/Winbox/WebFig) mit Audit.
* **Konnektivität:** WAN-Failover/Load-Balancing, VRRP als Backup hinter einem zentralen Master,
  VPN-Mesh zwischen Standorten.
* **Überwachung:** Metriken, Alarme per E-Mail (HTML) und Webhook, SLA-Berichte.

Produktname, Kurzname und Mail-Branding lassen sich per `.env` anpassen (`PRODUCT_NAME`,
`PRODUCT_SHORT`, `MAIL_*`).

* **Kein Public-IP nötig:** Jeder Router baut ausgehend einen WireGuard-Tunnel zum Hub auf (CGNAT-tauglich).
* **Ein Befehl zum Onboarding** im RouterOS-Terminal.
* Alle RouterOS-API-Zugriffe laufen ausschließlich durch diesen Tunnel.

Installation auf einem Server: [INSTALL.md](INSTALL.md) · Aufbau und Design-Entscheidungen: [ARCHITECTURE.md](ARCHITECTURE.md).

## Funktionsumfang

| Phase | Funktion | Status |
|------:|----------|--------|
| 1 | Auth (JWT), RBAC (Admin/Techniker/Read-Only), Mandanten, Standorte, Device-Pairing, WireGuard-Hub, Dashboard | ✅ |
| 2 | VPN-Mesh: Hub-and-Spoke / Full-Mesh, PSK pro Link, automatischer Push, Tunnel-Status + Topologie-Ansicht | ✅ |
| 3 | WAN Failover & Load-Balancing: bis 4 Links, Netwatch-Health-Checks (Ping/HTTP), PCC/ECMP, Recovery-Hysterese | ✅ |
| 4 | Monitoring: Polling → InfluxDB, Live-Kacheln per WebSocket (5-s-Live-Modus), Verlaufs-Charts, Grafana-Dashboards Mandant/Standort/Gerät | ✅ |
| 5 | Firewall-Policies: global/pro Mandant, Address-Lists/Filter/NAT, Multi-Device-Push, Versionierung, Snapshot-Rollback (auch atomar) | ✅ |
| 6 | Zero-Touch Provisioning: Templates, seriengebundene Langzeit-Tokens, Bootstrap-Script (Staging/Netinstall), Basiskonfig + WAN + Policies automatisch | ✅ |
| 7 | Content-Filter: NextDNS-Profile (Kategorien, Dienste, Security, Blocklisten), pro Mandant/Standort, DoH auf RouterOS, DNS-Erzwingung | ✅ |
| 8 | Remote Access: SSH/Winbox/WebFig-Proxy über den Tunnel, zeitlich begrenzt, IP-gebunden, Temp-User pro Session, lückenloses Audit | ✅ |
| 9 | Backups & Firmware: tägliche Exporte mit Diff-Ansicht, Dedupe, Retention; Fleet-Updates mit Batches, Pause bei Fehlern, Pre-Update-Backup | ✅ |
| 10 | Alerts & SLA: Regeln (offline, WAN down, Latenz, Mesh, CPU) mit E-Mail + Live-Toasts, Verfügbarkeit aus Statuswechseln, PDF-Berichte, Monatsversand | ✅ |
| 11 | VRRP & Backup-Transparenz: VRRP-Backup hinter zentralem Master (z. B. FortiGate) mit gekoppeltem WAN, Verbindungs-Flush im Failover, Alarme VRRP-Master / Backup-WAN aktiv / Datenvolumen (80 %/100 %), Monatslimit je WAN, SLA-Zeiten auf Backup, Webhooks (JSON/Teams) | ✅ |
| 12 | Branding & Mail-Layout: Produktname konfigurierbar, HTML-Mails (Outlook-tauglich, Klartext-Fallback) mit Statusbalken, Kontext je Alarmtyp, nächsten Schritten, lokaler Zeitzone je Mandant, einheitliche Betreffzeilen, Mail-Vorschau | ✅ |

## Schnellstart (Demo ohne Hardware)

```bash
cp .env.example .env          # Secrets anpassen!
docker compose up -d --build
```

* Dashboard: http://localhost:8080 – Login mit `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD`
* API-Doku (OpenAPI): http://localhost:8000/docs (nur lokal gebunden)
* Grafana: http://localhost:8080/grafana (MSP-Admins, eigener Grafana-Login)

Dienste im Compose-Setup: `api`, `worker`, `remote-proxy`, `wireguard-hub`, `frontend` (nginx),
`postgres`, `redis`, `influxdb`, `grafana`.

Mit `ROUTEROS_BACKEND=simulator` (Standard in `.env.example`) simuliert die Plattform RouterOS-Geräte:
Mandant anlegen → Standort anlegen → Gerät anlegen → „Pairing simulieren“.

## Produktivbetrieb mit echten Routern

1. `.env`: `ROUTEROS_BACKEND=api`, `PUBLIC_URL=https://sdwan.example.com`, `WG_HUB_ENDPOINT=vpn.example.com`.
2. HTTPS-Reverse-Proxy (Caddy, Traefik, Nginx Proxy Manager) vor Port `8080` setzen.
3. UDP-Port `51820` (WireGuard) am Server/Firewall freigeben.
4. Im Dashboard ein Gerät anlegen und den angezeigten Befehl im RouterOS-Terminal ausführen:

   ```
   /tool fetch url="https://sdwan.example.com/api/v1/onboard/<token>.rsc" dst-path=sdwan-onboard.rsc; :delay 2s; /import file-name=sdwan-onboard.rsc
   ```

**Proxmox-LXC:** Für den Hub entweder das WireGuard-Kernelmodul auf dem Host laden (`modprobe wireguard`)
oder den LXC mit `/dev/net/tun` betreiben – der Hub fällt automatisch auf `wireguard-go` zurück.

**Ports:** `8080/tcp` (Web/API über nginx), `51820/udp` (WireGuard-Hub), `40000-40019/tcp`
(Remote-Access-Proxy, nur wenn Fernzugriff genutzt wird). Port 8000 ist nur an `127.0.0.1` gebunden.

## Sicherheit (Kurzfassung)

* Strikte Mandanten-Isolation im ORM (automatischer `tenant_id`-Filter + Schreibschutz), RBAC pro Endpoint.
* Jeder Router erzeugt sein WireGuard-Keypair selbst; Pairing-Token sind einmalig, gehasht, befristet
  (Zero-Touch: zusätzlich an die Seriennummer gebunden).
* RouterOS-API/SSH-Zugriffe sind technisch auf das Management-Netz beschränkt; der API-Benutzer auf dem
  Router akzeptiert nur Logins von der Hub-Adresse.
* Secrets in der DB (API-Passwörter, PSKs, NextDNS-Keys) sind Fernet-verschlüsselt.
* Audit-Log für alle schreibenden Aktionen inkl. Policy-Pushes und jeder Remote-Access-Verbindung.
* `/api/v1/internal/*` (Hub-Agent) ist über nginx nicht erreichbar.

## Entwicklung

```bash
# Backend
cd backend
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                                   # 43 Tests, SQLite + RouterOS-Simulator
TEST_DATABASE_URL=postgresql+asyncpg://sdwan:sdwan@localhost/sdwan_test pytest   # gegen PostgreSQL
DB_AUTO_CREATE=true ROUTEROS_BACKEND=simulator USE_REDIS=false \
  DATABASE_URL=sqlite+aiosqlite:///dev.db uvicorn app.main:app --reload
python -m app.worker                     # Worker (zweites Terminal)

# Frontend
cd frontend && npm install && npm run dev   # http://localhost:5173 (Proxy auf :8000)
```

Neue Migration: `cd backend && alembic revision --autogenerate -m "..."`.

## Projektstruktur

```
backend/   FastAPI-App (app/), Worker (app/worker), Remote-Proxy (app/remote_proxy.py), Alembic, Tests
hub/       WireGuard-Hub-Agent (Peer-Sync, Handshake-Statistik)
frontend/  React + TypeScript + Tailwind
deploy/    Grafana-Provisioning (Datasource, Dashboards)
```
