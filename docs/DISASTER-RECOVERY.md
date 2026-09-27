# Disaster Recovery – MikroTik-Fleet-Management

Diese Anleitung beschreibt, wie die Plattform gesichert wird, wie sie auf einem **frischen Server**
wiederhergestellt wird und wie die Wiederherstellung regelmäßig geprüft wird. Ziel: Nach einem Totalausfall
verbinden sich alle Router **ohne Eingriff vor Ort** wieder mit der Plattform.

## 1. Was gesichert wird

| Teil | Warum |
|------|-------|
| PostgreSQL-Dump (`pg_dump -Fc`) | Mandanten, Geräte, Policies, Backups der Router, Hotspot-Portale inkl. Logos (liegen in der DB) |
| `.env` | `SECRET_KEY` / `ENCRYPTION_KEY`: Ohne sie sind gespeicherte Router-Passwörter und Webhook-URLs unlesbar |
| WireGuard-Hub (`hub.key`, `wg0.conf`) | Gleicher Hub-Schlüssel ⇒ die Router akzeptieren den neuen Server als denselben Hub |
| InfluxDB (optional, `PLATFORM_BACKUP_INFLUX=true`) | Metrik-Verlauf; für den Betrieb nicht nötig |

Alles landet in **einem** Archiv `sdwan-platform-<Zeitstempel>.tar.gz.age`. Es ist mit
[age](https://age-encryption.org) für einen Public Key verschlüsselt. Das Manifest enthält den Migrationsstand,
den Hub-Endpoint und Prüfsummen je Teil.

## 2. Einrichtung (einmalig)

1. **Schlüsselpaar erzeugen**, auf einem Admin-Rechner, **nicht** auf dem Server:
   ```bash
   age-keygen -o sdwan-dr-key.txt      # enthält AGE-SECRET-KEY-… und als Kommentar den Public Key
   ```
2. **Private Key sicher verwahren** – er ist der Schlüssel zu allen Sicherungen:
   - Ausgedruckt im Tresor und zusätzlich im Passwort-Manager des MSP (nicht auf dem Plattform-Server).
   - Zugriff für mindestens zwei Personen (Urlaub, Krankheit).
   - Ohne Private Key sind die Sicherungen wertlos. Ein Verlust erfordert ein neues Schlüsselpaar und eine neue
     Sicherung.
3. **Public Key** (`age1…`) in `.env` eintragen: `PLATFORM_BACKUP_AGE_RECIPIENT=age1…`.
4. Optional **externes Ziel**:
   - `deploy/rclone/rclone.conf` nach dem Beispiel `rclone.conf.example` anlegen (S3-kompatibel oder SFTP).
   - `PLATFORM_BACKUP_RCLONE_REMOTE=offsite:sdwan-backups` setzen.
   - Empfehlung: ein Ziel **außerhalb** des Rechenzentrums des Servers.
5. `docker compose up -d` (der Worker bindet `.env` und das Hub-Volume nur lesend ein). Danach auf der Seite
   **Plattform-Sicherung** „Jetzt sichern“ ausführen und prüfen, dass der Status „erfolgreich“ ist.

Zeitplan: täglich `PLATFORM_BACKUP_HOUR_UTC`:10 UTC. Aufbewahrung: `PLATFORM_BACKUP_KEEP_DAYS` Tage (lokal und
extern). Schlägt eine Sicherung fehl, gibt es den Plattform-Alarm „Plattform-Sicherung fehlgeschlagen“: Mail an
alle MSP-Admins und optional `PLATFORM_WEBHOOK_URL`.

Manuell auf dem Server: `sudo bash deploy/backup.sh` bzw. `sudo bash deploy/backup.sh list`.

## 3. Wiederherstellung auf einem frischen Server

Voraussetzung: der age-Private-Key, die letzte Sicherung (lokal gerettet oder vom externen Ziel) und der
Zugriff auf das DNS.

1. **DNS umstellen:** Der Name aus `WG_HUB_ENDPOINT` (Hub) und aus `PUBLIC_URL` zeigt auf den neuen Server.
   Gleicher Name + gleicher Hub-Schlüssel ⇒ die Router verbinden sich selbst wieder. Sie lösen den Namen beim
   nächsten WireGuard-Handshake neu auf; das kann einige Minuten dauern.
2. Server mit Docker vorbereiten, das Repository nach `/opt/sdwan` auschecken und die **Firewall-Ports** öffnen
   (wie `deploy/install.sh`: 80/443/tcp, WireGuard-Port/udp).
3. Sicherung und Schlüssel auf den Server kopieren, z. B. nach `/root/restore/`.
4. Wiederherstellen:
   ```bash
   cd /opt/sdwan
   sudo ARCHIVE=/root/restore/sdwan-platform-….tar.gz.age IDENTITY=/root/restore/sdwan-dr-key.txt bash deploy/restore.sh
   ```
   Ablauf:
   1. Entschlüsseln und Prüfsummen kontrollieren.
   2. `.env` zurückspielen.
   3. Hub-Schlüssel ins Volume `hub-data`.
   4. PostgreSQL starten, `pg_restore`.
   5. Stack starten, Health-Check.
5. **Schlüssel sofort vom Server entfernen:** `shred -u /root/restore/sdwan-dr-key.txt`.
6. HTTPS/Reverse-Proxy wie bei der Erstinstallation einrichten (`deploy/install.sh` mit bestehender `.env` ändert
   keine Secrets).
7. Kontrolle:
   - Anmeldung funktioniert.
   - Geräteliste: Die Geräte werden nach und nach „online“.
   - Ein Gerät öffnen: Metriken und Firewall laden.
   - Plattform-Sicherung: „Jetzt sichern“.
8. InfluxDB (falls gesichert): `docker compose cp <out>/influx influxdb:/tmp/restore` und
   `docker compose exec influxdb influx restore /tmp/restore` (ANNAHME (Labor): Befehl bei Bedarf anpassen).

## 4. Regelmäßige Testwiederherstellung (vierteljährlich)

1. Test-VM ohne öffentliche DNS-Umstellung bereitstellen.
2. Letzte Sicherung vom **externen** Ziel holen (prüft zugleich das Ziel).
3. `deploy/restore.sh` wie oben ausführen, mit `FORCE=yes`, falls nötig.
4. Prüfen:
   - Anmeldung.
   - Anzahl Mandanten und Geräte stimmt.
   - Ein Router-Backup lässt sich anzeigen, d. h. `ENCRYPTION_KEY` passt.
   - Die Seite Plattform-Sicherung zeigt den Verlauf.
5. Die Router verbinden sich **nicht** (DNS zeigt weiter auf den Produktivserver) – das ist beim Test gewollt.
   **Nicht** den Test-Server mit dem Produktiv-Hub-Namen ins Internet stellen.
6. Ergebnis (Datum, Dauer, Abweichungen) im Betriebshandbuch des MSP festhalten, danach die Test-VM löschen.

## 5. 2FA-Notfall (nur mit Server-Zugriff)

Kommt Phase 22 hinzu, gibt es für ausgesperrte Administratoren folgende Befehle:
```bash
docker compose exec api python -m app.cli reset-2fa <email>   # Zwei-Faktor zurücksetzen
docker compose exec api python -m app.cli unlock <email>      # Sperre nach Fehlversuchen aufheben
```
Beide schreiben einen Audit-Eintrag und melden sich über den Plattform-Webhook.
