#!/usr/bin/env bash
# =============================================================================
# MikroTik-Fleet-Management – Installer für einen (bestehenden) Linux-Server
#
#   sudo DOMAIN=sdwan.networkx.cc ADMIN_EMAIL=admin@networkx.cc bash deploy/install.sh
#
# Was passiert:
#   * Docker + Compose-Plugin installieren (falls nicht vorhanden)
#   * Code nach $INSTALL_DIR holen (oder aktuelles Repo verwenden)
#   * .env mit zufälligen Secrets erzeugen (bestehende .env bleibt erhalten)
#   * freie Ports / freies Docker-Subnetz wählen (Konflikte mit anderen Diensten vermeiden)
#   * HTTPS: Caddy (Standard), oder vorhandenes Caddy/nginx einbinden, sonst Anleitung
#   * UFW-Regeln setzen (80/443/tcp, WireGuard/udp, optional Remote-Access-Ports)
#   * WireGuard-Kernelmodul laden, Stack starten, Health-Check
#
# Variablen (alle optional):
#   DOMAIN            sdwan.networkx.cc   öffentlicher Hostname (DNS A-Record -> Server)
#   WG_ENDPOINT       = DOMAIN            Hostname für WireGuard (gleiche IP reicht)
#   ADMIN_EMAIL       admin@<domain>      erster MSP-Admin + Let's-Encrypt-Kontakt
#   INSTALL_DIR       /opt/sdwan
#   REPO_URL          https://github.com/flaash88/sdwan.git
#   BRANCH            claude/mikrotik-sdwan-platform-qs2sti
#   ROUTEROS_BACKEND  api | simulator     (simulator = Demo ohne Router)
#   PROXY             auto | caddy | nginx | none
#   REMOTE_ACCESS     yes | no            Ports 40000-40019/tcp für Winbox/SSH-Proxy veröffentlichen
#   REMOTE_ALLOW_FROM any | <CIDR>        UFW-Quelle für die Remote-Access-Ports (zusätzlich prüft
#                                         der Proxy pro Session die IP des Technikers)
# =============================================================================
set -euo pipefail

DOMAIN="${DOMAIN:-sdwan.networkx.cc}"
WG_ENDPOINT="${WG_ENDPOINT:-$DOMAIN}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@${DOMAIN#*.}}"
INSTALL_DIR="${INSTALL_DIR:-/opt/sdwan}"
REPO_URL="${REPO_URL:-https://github.com/flaash88/sdwan.git}"
BRANCH="${BRANCH:-claude/mikrotik-sdwan-platform-qs2sti}"
ROUTEROS_BACKEND="${ROUTEROS_BACKEND:-api}"
PROXY="${PROXY:-auto}"
REMOTE_ACCESS="${REMOTE_ACCESS:-yes}"
REMOTE_ALLOW_FROM="${REMOTE_ALLOW_FROM:-any}"

c_ok() { printf '\033[32m✔ %s\033[0m\n' "$*"; }
c_info() { printf '\033[36m➜ %s\033[0m\n' "$*"; }
c_warn() { printf '\033[33m⚠ %s\033[0m\n' "$*"; }
die() { printf '\033[31m✘ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Bitte als root ausführen (sudo)."
command -v apt-get >/dev/null || die "Nur Debian/Ubuntu werden von diesem Installer unterstützt."

port_used_tcp() { ss -Hltn "sport = :$1" 2>/dev/null | grep -q .; }
port_used_udp() { ss -Hlun "sport = :$1" 2>/dev/null | grep -q .; }
free_tcp_port() { local p=$1; while port_used_tcp "$p"; do p=$((p + 1)); done; echo "$p"; }
rand() { openssl rand -hex "${1:-24}"; }
setenv() { # setenv KEY VALUE -> in .env setzen/ersetzen
  local k=$1 v=$2
  if grep -q "^${k}=" .env; then sed -i "s|^${k}=.*|${k}=${v}|" .env; else echo "${k}=${v}" >>.env; fi
}
getenv() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true; }

# ----------------------------------------------------------------------------- 1. Pakete
c_info "Pakete prüfen …"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git openssl iproute2 dnsutils >/dev/null
if ! command -v docker >/dev/null; then
  c_info "Docker installieren …"
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
systemctl enable --now docker >/dev/null 2>&1 || true
docker compose version >/dev/null 2>&1 || apt-get install -y -qq docker-compose-plugin >/dev/null
CV=$(docker compose version --short | sed 's/^v//')
if [ "$(printf '%s\n2.24.0\n' "$CV" | sort -V | head -1)" != "2.24.0" ]; then
  c_info "Docker Compose $CV zu alt – aktualisiere Plugin …"
  apt-get install -y -qq --only-upgrade docker-compose-plugin >/dev/null || die "Docker Compose >= 2.24 benötigt (installiert: $CV)"
fi
c_ok "Docker $(docker version --format '{{.Server.Version}}'), Compose $(docker compose version --short)"

# ----------------------------------------------------------------------------- 2. Code
if [ -f "$(pwd)/docker-compose.yml" ] && [ -d "$(pwd)/backend" ] && [ "$(pwd)" != "$INSTALL_DIR" ] && [ ! -d "$INSTALL_DIR" ]; then
  c_info "Kopiere aktuelles Repo nach $INSTALL_DIR"
  mkdir -p "$INSTALL_DIR" && cp -a . "$INSTALL_DIR/"
elif [ -d "$INSTALL_DIR/.git" ]; then
  c_info "Aktualisiere $INSTALL_DIR ($BRANCH)"
  git -C "$INSTALL_DIR" fetch -q origin "$BRANCH" && git -C "$INSTALL_DIR" checkout -q "$BRANCH" && git -C "$INSTALL_DIR" pull -q --ff-only origin "$BRANCH"
elif [ ! -d "$INSTALL_DIR" ]; then
  c_info "Klone $REPO_URL ($BRANCH) nach $INSTALL_DIR"
  git clone -q -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR" || die "Klonen fehlgeschlagen – privates Repo? Dann zuerst selbst klonen und das Skript aus dem Repo starten."
fi
cd "$INSTALL_DIR"
c_ok "Code in $INSTALL_DIR"

# ----------------------------------------------------------------------------- 3. DNS-Check
SERVER_IP=$(curl -4 -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)
DNS_IP=$(dig +short A "$DOMAIN" | tail -1 || true)
if [ -n "$SERVER_IP" ] && [ "$DNS_IP" != "$SERVER_IP" ]; then
  c_warn "DNS: $DOMAIN zeigt auf '${DNS_IP:-nichts}', Server-IP ist $SERVER_IP."
  c_warn "Zertifikat wird erst ausgestellt, wenn der A-Record stimmt (Caddy versucht es automatisch erneut)."
else
  c_ok "DNS $DOMAIN -> ${DNS_IP:-?}"
fi

# ----------------------------------------------------------------------------- 4. Ports & Netz
NEW_ENV=0
if [ ! -f .env ]; then cp .env.example .env; chmod 600 .env; NEW_ENV=1; fi

FRONTEND_PORT=$(getenv FRONTEND_PORT); FRONTEND_PORT=${FRONTEND_PORT:-8080}
API_PORT=$(getenv API_PORT); API_PORT=${API_PORT:-8000}
WG_PORT=$(getenv WG_HUB_PORT); WG_PORT=${WG_PORT:-51820}
RUNNING=$(docker compose ps -q 2>/dev/null | wc -l)
if [ "$RUNNING" -eq 0 ]; then
  FRONTEND_PORT=$(free_tcp_port "$FRONTEND_PORT")
  API_PORT=$(free_tcp_port "$API_PORT")
  while port_used_udp "$WG_PORT"; do WG_PORT=$((WG_PORT + 1)); done
  if [ "$REMOTE_ACCESS" = "yes" ]; then
    for p in $(seq 40000 40019); do port_used_tcp "$p" && die "Port $p belegt – REMOTE_ACCESS=no setzen oder REMOTE_PROXY_PORT_RANGE in .env ändern."; done
  fi
fi
SDWAN_NET=$(getenv SDWAN_NET); SDWAN_NET=${SDWAN_NET:-172.30.0}
if [ "$RUNNING" -eq 0 ] && ! docker network ls --format '{{.Name}}' | grep -qx sdwan_sdwan; then
  # shellcheck disable=SC2046  # Word-Splitting der Netz-IDs ist gewollt
  used_nets=$( { ip -4 route | awk '{print $1}'; docker network inspect $(docker network ls -q) --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null || true; } | tr ' ' '\n' || true)
  for cand in 172.30.0 172.31.250 10.253.0 10.254.0 192.168.253; do
    if ! echo "$used_nets" | grep -q "^${cand}\."; then SDWAN_NET=$cand; break; fi
  done
fi

# ----------------------------------------------------------------------------- 5. Reverse-Proxy wählen
if [ "$PROXY" = "auto" ]; then
  if systemctl is-active --quiet caddy 2>/dev/null; then PROXY=caddy
  elif systemctl is-active --quiet nginx 2>/dev/null; then PROXY=nginx
  elif port_used_tcp 443 || port_used_tcp 80; then PROXY=none
  else PROXY=caddy
  fi
fi
FRONTEND_BIND=127.0.0.1
if [ "$PROXY" = "none" ]; then
  # z. B. Nginx Proxy Manager / Traefik in Docker: über die docker0-Bridge erreichbar, nicht öffentlich
  FRONTEND_BIND=$(ip -4 addr show docker0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 || true)
  FRONTEND_BIND=${FRONTEND_BIND:-127.0.0.1}
fi

# ----------------------------------------------------------------------------- 6. .env
if [ "$NEW_ENV" -eq 1 ]; then
  ADMIN_PW=$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)
  setenv SECRET_KEY "$(rand 32)"
  setenv HUB_TOKEN "$(rand 24)"
  setenv POSTGRES_PASSWORD "$(rand 24)"
  setenv INFLUX_ADMIN_PASSWORD "$(rand 20)"
  setenv INFLUX_TOKEN "$(rand 32)"
  setenv GRAFANA_ADMIN_PASSWORD "$(rand 16)"
  setenv BOOTSTRAP_ADMIN_EMAIL "$ADMIN_EMAIL"
  setenv BOOTSTRAP_ADMIN_PASSWORD "$ADMIN_PW"
  setenv ROUTEROS_BACKEND "$ROUTEROS_BACKEND"
fi
setenv ENVIRONMENT production
setenv PUBLIC_URL "https://$DOMAIN"
setenv CORS_ORIGINS "[\"https://$DOMAIN\"]"
setenv GRAFANA_PUBLIC_URL "https://$DOMAIN/grafana"
setenv WG_HUB_ENDPOINT "$WG_ENDPOINT"
setenv WG_HUB_PORT "$WG_PORT"
setenv REMOTE_PROXY_HOST "$DOMAIN"
setenv FRONTEND_BIND "$FRONTEND_BIND"
setenv FRONTEND_PORT "$FRONTEND_PORT"
setenv API_PORT "$API_PORT"
setenv SDWAN_NET "$SDWAN_NET"
# Remote-Access-Proxy: Listener existieren nur während aktiver Sessions und prüfen die Quell-IP selbst.
setenv REMOTE_PROXY_BIND "$([ "$REMOTE_ACCESS" = yes ] && echo 0.0.0.0 || echo 127.0.0.1)"
chmod 600 .env
c_ok ".env: Frontend ${FRONTEND_BIND}:${FRONTEND_PORT}, WireGuard ${WG_PORT}/udp, Docker-Netz ${SDWAN_NET}.0/24, Proxy=$PROXY"

# ----------------------------------------------------------------------------- 7. WireGuard-Modul
if modprobe wireguard 2>/dev/null; then
  echo wireguard >/etc/modules-load.d/sdwan-wireguard.conf
  c_ok "WireGuard-Kernelmodul geladen"
else
  c_warn "WireGuard-Kernelmodul nicht verfügbar – Hub nutzt wireguard-go (Userspace)"
fi

# ----------------------------------------------------------------------------- 8. Reverse-Proxy konfigurieren
SITE_BLOCK="$DOMAIN {
    encode gzip
    reverse_proxy 127.0.0.1:$FRONTEND_PORT
}"
case "$PROXY" in
  caddy)
    if ! command -v caddy >/dev/null; then
      c_info "Caddy installieren …"
      apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null
      curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
      curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt >/etc/apt/sources.list.d/caddy-stable.list
      apt-get update -qq && apt-get install -y -qq caddy >/dev/null
      printf '{\n    email %s\n}\n\n' "$ADMIN_EMAIL" >/etc/caddy/Caddyfile
    fi
    if grep -q "^$DOMAIN {" /etc/caddy/Caddyfile 2>/dev/null; then
      c_info "Caddyfile enthält $DOMAIN bereits – unverändert gelassen"
    else
      printf '\n%s\n' "$SITE_BLOCK" >>/etc/caddy/Caddyfile
    fi
    caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1 || die "Caddyfile ungültig – bitte /etc/caddy/Caddyfile prüfen"
    systemctl enable --now caddy >/dev/null 2>&1; systemctl reload caddy
    c_ok "Caddy: https://$DOMAIN -> 127.0.0.1:$FRONTEND_PORT (Zertifikat automatisch)"
    ;;
  nginx)
    apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
    cat >/etc/nginx/sites-available/sdwan.conf <<EOF
map \$http_upgrade \$sdwan_upgrade { default upgrade; '' close; }
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    client_max_body_size 20m;
    location / {
        proxy_pass http://127.0.0.1:$FRONTEND_PORT;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$sdwan_upgrade;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 3600s;
    }
}
EOF
    ln -sf /etc/nginx/sites-available/sdwan.conf /etc/nginx/sites-enabled/sdwan.conf
    nginx -t >/dev/null 2>&1 || die "nginx-Konfiguration ungültig"
    systemctl reload nginx
    certbot --nginx -n --agree-tos -m "$ADMIN_EMAIL" -d "$DOMAIN" --redirect >/dev/null 2>&1 \
      && c_ok "nginx + Let's Encrypt für $DOMAIN" \
      || c_warn "Zertifikat noch nicht ausgestellt (DNS?) – später: certbot --nginx -d $DOMAIN"
    ;;
  none)
    c_warn "Port 80/443 ist von einem anderen Dienst belegt (z. B. Nginx Proxy Manager/Traefik)."
    c_warn "Dort einen Proxy-Host anlegen: $DOMAIN -> http://$FRONTEND_BIND:$FRONTEND_PORT (Websockets an, SSL an)."
    ;;
esac

# ----------------------------------------------------------------------------- 9. UFW
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  c_info "UFW-Regeln setzen …"
  ufw allow OpenSSH >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null
  if [ "$PROXY" != "none" ]; then
    ufw allow 80/tcp comment 'sdwan http' >/dev/null
    ufw allow 443/tcp comment 'sdwan https' >/dev/null
  fi
  ufw allow "$WG_PORT"/udp comment 'sdwan wireguard hub' >/dev/null
  if [ "$REMOTE_ACCESS" = "yes" ]; then
    if [ "$REMOTE_ALLOW_FROM" = "any" ]; then
      ufw allow 40000:40019/tcp comment 'sdwan remote access' >/dev/null
    else
      ufw allow from "$REMOTE_ALLOW_FROM" to any port 40000:40019 proto tcp comment 'sdwan remote access' >/dev/null
    fi
  fi
  c_ok "UFW: $([ "$PROXY" != none ] && echo "80,443/tcp, ")$WG_PORT/udp$([ "$REMOTE_ACCESS" = yes ] && echo ", 40000-40019/tcp ($REMOTE_ALLOW_FROM)")"
  c_warn "Hinweis: Docker-veröffentlichte Ports umgehen UFW. Frontend/API sind daher nur an $FRONTEND_BIND gebunden."
fi

# ----------------------------------------------------------------------------- 10. Start
c_info "Images bauen und Stack starten (erster Build dauert einige Minuten) …"
docker compose pull -q postgres redis influxdb grafana >/dev/null 2>&1 || true
docker compose up -d --build --remove-orphans
c_info "Warte auf API …"
for i in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:$API_PORT/healthz" >/dev/null 2>&1 && break
  sleep 3
  [ "$i" -eq 60 ] && die "API startet nicht – Logs: cd $INSTALL_DIR && docker compose logs api"
done
c_ok "API läuft"
for i in $(seq 1 20); do
  docker compose logs wireguard-hub 2>/dev/null | grep -q "Hub Public-Key" && break
  sleep 3
done
docker compose logs wireguard-hub 2>/dev/null | grep -q "Hub Public-Key" && c_ok "WireGuard-Hub registriert" \
  || c_warn "WireGuard-Hub noch nicht bereit – prüfen: docker compose logs wireguard-hub"

# ----------------------------------------------------------------------------- 11. Zusammenfassung
CRED="$INSTALL_DIR/ZUGANGSDATEN.txt"
{
  echo "MikroTik-Fleet-Management – Zugangsdaten ($(date -Iseconds))"
  echo "Dashboard:   https://$DOMAIN"
  echo "Login:       $(getenv BOOTSTRAP_ADMIN_EMAIL)"
  echo "Passwort:    $(getenv BOOTSTRAP_ADMIN_PASSWORD)   (nach erstem Login ändern)"
  echo "Grafana:     https://$DOMAIN/grafana  (admin / $(getenv GRAFANA_ADMIN_PASSWORD))"
  echo "WireGuard:   $WG_ENDPOINT:$WG_PORT/udp"
  echo "Modus:       ROUTEROS_BACKEND=$(getenv ROUTEROS_BACKEND)"
} >"$CRED"
chmod 600 "$CRED"
echo
c_ok "Installation abgeschlossen."
cat "$CRED"
echo
echo "Zugangsdaten gespeichert in $CRED (nur root lesbar)."
echo "Update später:  sudo bash $INSTALL_DIR/deploy/update.sh"
