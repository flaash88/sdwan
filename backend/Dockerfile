FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 ca-certificates curl gnupg rclone \
 # Plattform-Sicherung (Phase 21): pg_dump/pg_restore in der Version des Servers (PostgreSQL 16, PGDG-Repository)
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
 && . /etc/os-release \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $VERSION_CODENAME-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update && apt-get install -y --no-install-recommends postgresql-client-16 \
 && rm -rf /var/lib/apt/lists/*
# AUDIT-030: eigener Benutzer; der Entrypoint setzt als root nur die Route ins Management-Netz (NET_ADMIN) und startet
# den Dienst dann per setpriv als "app" (einzige Fähigkeit: NET_BIND_SERVICE für Syslog UDP 514)
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN chmod +x /app/docker-entrypoint.sh
ENTRYPOINT ["/app/docker-entrypoint.sh"]
# Proxy-Header wertet die App selbst aus (app/proxy.py, TRUSTED_PROXIES) – nie "--forwarded-allow-ips *" (AUDIT-003)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
