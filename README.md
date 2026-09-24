# MikroTik SD-WAN Control Plane

Cloud-basierte, mandantenfähige SD-WAN-Management-Plattform für MikroTik-Router (RouterOS 7) –
gebaut für den Einsatz durch MSPs, selbst gehostet per Docker Compose (Hetzner-vServer, Proxmox-VM/LXC).

* **Kein Public-IP nötig:** Jeder Router baut ausgehend einen WireGuard-Tunnel zum Hub auf (CGNAT-tauglich).
* **Ein Befehl zum Onboarding** im RouterOS-Terminal.
* Alle RouterOS-API-Zugriffe laufen ausschließlich durch diesen Tunnel.

Details zu Aufbau und Design-Entscheidungen: [ARCHITECTURE.md](ARCHITECTURE.md).

## Funktionsumfang

| Phase | Funktion | Status |
|------:|----------|--------|
| 1 | Auth (JWT), RBAC (Admin/Techniker/Read-Only), Mandanten, Standorte, Device-Pairing, WireGuard-Hub, Dashboard | ✅ |
| 2 | VPN-Mesh: Hub-and-Spoke / Full-Mesh, PSK pro Link, automatischer Push, Tunnel-Status + Topologie-Ansicht | ✅ |
| 3 | WAN Failover & Load-Balancing: bis 4 Links, Netwatch-Health-Checks (Ping/HTTP), PCC/ECMP, Recovery-Hysterese | ✅ |
| 4 | Monitoring: Polling → InfluxDB, Live-Kacheln per WebSocket (5-s-Live-Modus), Verlaufs-Charts, Grafana-Dashboards Mandant/Standort/Gerät | ✅ |
| 5 | Firewall-Policies: global/pro Mandant, Address-Lists/Filter/NAT, Multi-Device-Push, Versionierung, Snapshot-Rollback (auch atomar) | ✅ |
| 6 | Zero-Touch Provisioning: Templates, seriengebundene Langzeit-Tokens, Bootstrap-Script (Staging/Netinstall), Basiskonfig + WAN + Policies automatisch | ✅ |

## Schnellstart (Demo ohne Hardware)

```bash
cp .env.example .env          # Secrets anpassen!
docker compose up -d --build
```

* Dashboard: http://localhost:8080 – Login mit `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD`
* API-Doku (OpenAPI): http://localhost:8000/docs
* Grafana: http://localhost:8080/grafana

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

## Entwicklung

```bash
# Backend
cd backend
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                                   # SQLite, Simulator
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
backend/   FastAPI-App (app/), Worker (app/worker), Alembic, Tests
hub/       WireGuard-Hub-Agent (Peer-Sync, Handshake-Statistik)
frontend/  React + TypeScript + Tailwind
deploy/    Grafana-Provisioning (Datasource, Dashboards)
```
