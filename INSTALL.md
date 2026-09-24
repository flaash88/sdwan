# Installation auf einem Server (z. B. „netzwarte“)

Voraussetzungen: Debian 12 / Ubuntu 22.04+ mit öffentlicher IPv4, root-Zugang.
Andere Dienste auf dem Server sind kein Problem – der Installer erkennt belegte Ports,
einen vorhandenen Reverse-Proxy (Caddy/nginx) und kollidierende Docker-Netze.

## 1. DNS

| Name | Typ | Wert |
|------|-----|------|
| `sdwan.networkx.cc` | A | öffentliche IP des Servers |

(Web-Oberfläche, Onboarding und WireGuard-Hub laufen unter demselben Namen.)

## 2. Installation

```bash
sudo git clone -b claude/mikrotik-sdwan-platform-qs2sti https://github.com/flaash88/sdwan.git /opt/sdwan
cd /opt/sdwan
sudo DOMAIN=sdwan.networkx.cc ADMIN_EMAIL=admin@networkx.cc bash deploy/install.sh
```

Der Installer:

* installiert Docker/Compose (falls nötig) und git/openssl/dig,
* erzeugt `.env` mit zufälligen Secrets (eine vorhandene `.env` bleibt erhalten),
* wählt freie Ports und ein freies internes Docker-Netz,
* richtet HTTPS ein: vorhandenes **Caddy** oder **nginx** (+ certbot) wird genutzt, sonst wird Caddy
  installiert. Belegt ein anderer Dienst 80/443 (z. B. Nginx Proxy Manager), gibt er das Proxy-Ziel aus,
* setzt UFW-Regeln (80/443/tcp, 51820/udp, 40000–40019/tcp),
* lädt das WireGuard-Kernelmodul, baut und startet den Stack,
* schreibt die Zugangsdaten nach `/opt/sdwan/ZUGANGSDATEN.txt` (nur root lesbar).

Optionale Variablen: `ROUTEROS_BACKEND=simulator` (Demo ohne Router), `PROXY=caddy|nginx|none`,
`REMOTE_ACCESS=no`, `REMOTE_ALLOW_FROM=<Büro-IP/CIDR>`, `WG_ENDPOINT=<anderer Hostname>`.

## 3. Hoster-Firewall

Falls der Hoster zusätzlich eine eigene Firewall hat: 80/tcp, 443/tcp, 51820/udp und (für Fernzugriff)
40000–40019/tcp freigeben.

## Update

```bash
sudo bash /opt/sdwan/deploy/update.sh
```

## Nützliche Befehle

```bash
cd /opt/sdwan
docker compose ps                    # Status
docker compose logs -f api worker    # Logs
docker compose logs wireguard-hub    # Hub (Public-Key, Peer-Sync)
docker compose down                  # stoppen (Daten bleiben in Volumes)
```
