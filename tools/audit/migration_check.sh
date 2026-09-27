#!/usr/bin/env bash
# Audit: Alembic-Migrationen gegen eine frische PostgreSQL-Datenbank prüfen.
#  1) leer -> head   2) leer -> 0019 + Testdaten -> head (Zeilen erhalten?)   3) head -> base (Downgrade)
# Aufruf: tools/audit/migration_check.sh   (braucht lokales PostgreSQL, Benutzer sdwan mit CREATEDB oder sudo -u postgres)
set -uo pipefail
cd "$(dirname "$0")/../../backend" || exit 1
# PY = Verzeichnis mit dem alembic der Test-Umgebung (Default: aus PATH)
PY=${PY:-$(dirname "$(command -v alembic)")}
DB=sdwan_audit_mig
PGUSER_DB=${PGUSER_DB:-sdwan}
PGPASS=${PGPASS:-sdwan}
psqlp() { PGPASSWORD=$PGPASS psql -h localhost -U "$PGUSER_DB" -d "$DB" -v ON_ERROR_STOP=1 -q "$@"; }
recreate() { su postgres -c "dropdb --if-exists $DB" && su postgres -c "createdb -O $PGUSER_DB $DB"; }
export DATABASE_URL="postgresql+asyncpg://$PGUSER_DB:$PGPASS@localhost:5432/$DB"
echo "== 1) leer -> head"; recreate
s=$(date +%s); $PY/alembic upgrade head >/tmp/mig1.log 2>&1 && echo "OK ($(( $(date +%s)-s ))s)" || { echo FEHLER; tail -20 /tmp/mig1.log; }
echo "== 2) 0019 + Daten -> head"; recreate
$PY/alembic upgrade 0019 >/tmp/mig2a.log 2>&1 || { echo "FEHLER upgrade 0019"; tail /tmp/mig2a.log; }
psqlp <<'SQL'
insert into tenants (id, created_at, name, slug, is_active, settings, mesh_topology) values ('11111111-1111-1111-1111-111111111111', now(), 'Audit', 'audit', true, '{"x":1}', 'hub');
insert into users (id, created_at, email, password_hash, role, is_superuser, is_active, tenant_id, full_name)
  values ('22222222-2222-2222-2222-222222222222', now(), 'a@example.com', 'h', 'admin', false, true, '11111111-1111-1111-1111-111111111111', 'A');
insert into sites (id, created_at, tenant_id, name, lan_subnets, is_mesh_hub) values ('33333333-3333-3333-3333-333333333333', now(), '11111111-1111-1111-1111-111111111111', 'S', '[]', false);
insert into devices (id, created_at, tenant_id, site_id, name, serial, tunnel_ip, status, pairing_status, tags, facts)
  values ('44444444-4444-4444-4444-444444444444', now(), '11111111-1111-1111-1111-111111111111', '33333333-3333-3333-3333-333333333333', 'r1', 'SN1', '10.100.0.5', 'online', 'paired', '[]', '{"cpu_load":3}');
SQL
[ $? -eq 0 ] || echo "Testdaten konnten nicht eingefügt werden (Schema 0019 abweichend)"
before=$(psqlp -tAc "select (select count(*) from tenants)||'/'||(select count(*) from users)||'/'||(select count(*) from devices)||'/'||(select count(*) from sites)")
$PY/alembic upgrade head >/tmp/mig2b.log 2>&1 && echo "upgrade 0019->head OK" || { echo "FEHLER"; tail -20 /tmp/mig2b.log; }
after=$(psqlp -tAc "select (select count(*) from tenants)||'/'||(select count(*) from users)||'/'||(select count(*) from devices)||'/'||(select count(*) from sites)")
echo "Zeilen tenants/users/devices/sites vorher=$before nachher=$after"
psqlp -tAc "select name, serial, tunnel_ip, facts::text, (select settings::text from tenants limit 1) from devices"
echo "== 3) head -> base (Downgrade)"
$PY/alembic downgrade base >/tmp/mig3.log 2>&1 && echo "Downgrade bis base OK" || { echo "Downgrade FEHLER:"; grep -E "Running downgrade|Error|error" /tmp/mig3.log | tail -4; }
su postgres -c "dropdb --if-exists $DB"
