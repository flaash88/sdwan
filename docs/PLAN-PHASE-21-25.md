# Plan Phasen 21–25 (Plattform-Sicherung, 2FA, Sicherheitsmeldungen, Vor-Ort-Zugang/API-Tokens, Nachbarn/Flows/Inventar/ZTP-Import)

## Context
Die Plattform (MikroTik-Fleet-Management, Repo flaash88/sdwan, Branch `claude/mikrotik-sdwan-platform-qs2sti`)
bekommt fünf Betriebs- und Sicherheitsphasen:
- Die Plattform selbst ist wiederherstellbar (Disaster Recovery).
- Anmeldungen sind besser geschützt (2FA, Sperre bei Fehlversuchen).
- Bekannte RouterOS-Schwachstellen werden sichtbar und blockieren riskante Funktionen.
- Techniker kommen auch ohne Plattform an jeden Router (Break-Glass), Automatisierung läuft über API-Tokens.
- Betriebsdaten werden erweitert: Nachbarn, Top-Verbraucher, Inventar, ZTP-Massenimport.

Grundsatz Allgemeinheit gilt: keine Kundendaten, Defaults nur markiert und änderbar, Listen als Seed-Daten.

## Arbeitsweise
- **Erster Schritt:** Dieser Plan wird als `docs/PLAN-PHASE-21-25.md` committet und gepusht. Danach werden die Phasen
  21→25 ohne Rückfragen umgesetzt.
- **Je Phase:**
  - Alembic-Migration (ab 0030, nur additiv).
  - Tests mit pytest, komplette Suite grün, `npm run build`, Screenshot-Check im Simulator.
  - Doku: ARCHITECTURE-Abschnitt, README-Zeile, LABORTEST-Schritte, `PATH_SPECS` und Simulator für neue Pfade,
    Abschnitt „Stand Phase X“.
  - Eigener Commit und Push.
- **Anhalten nur bei:** Tests nicht grün; Router-Änderung ohne Benutzeraktion; Migration mit Datenverlust.
- **Wiederverwendung:**
  - `services/webhook.py` (`validate_url`, `send`) und `security.encrypt_secret`/`generate_token`/`hash_token`.
  - Der Rate-Limit-Ansatz aus `services/hotspot.rate_limited` (Redis INCR, In-Memory-Fallback) wird nach
    `app/ratelimit.py` verallgemeinert.
  - `segno` für QR-Codes; `services/targets.resolve_targets`.
  - Syslog-Empfänger-Muster (`syslog_receiver.py`, Compose `network_mode: service:wireguard-hub`).
  - Compliance-Erweiterung über `LIVE_TYPES`/`evaluate_rule`, Alarmtypen über `alerts.TYPES`/`conditions`.
  - Seeds über `app/seeds/*.json` und `seeds.APPLIERS`.
  - Offboarding-Schritte aus `services/offboarding.py`; `api_rights.revert_start` für die Router-Uhr.

## Phase 21 – Plattform-Sicherung und Disaster Recovery
- **Ablauf:** `app/platform_backup.py` (CLI `python -m app.platform_backup run|list|verify`), ausgeführt im
  Worker-Container. `deploy/backup.sh` ruft `docker compose exec worker python -m app.platform_backup run` auf
  (manuell oder per Cron). Der Worker-Job läuft täglich um `PLATFORM_BACKUP_HOUR_UTC`.
- **Inhalt des Archivs** (tar.gz, dann mit age verschlüsselt):
  - `pg_dump -Fc` über das Netzwerk (Postgres-Client ins Backend-Image).
  - InfluxDB optional: `influx backup` bzw. HTTP-API, gesteuert über `PLATFORM_BACKUP_INFLUX`.
  - `.env` und Hub-Volume (`hub.key`, `wg0.conf`), jeweils read-only in den Worker gemountet.
  - Manifest mit Versionen, sha256 und Migrationsstand.
  - Assets: Es gibt keine Datei-Uploads; Hotspot-Logos und Seiten liegen in der DB und sind damit im Dump. Das
    wird in der Doku festgehalten.
- **Verschlüsselung:** `age` mit `PLATFORM_BACKUP_AGE_RECIPIENT` (nur Public Key). Ohne Recipient gibt es keine
  Sicherung, sondern Status „nicht konfiguriert“. Grund: `.env` enthält Schlüssel und darf nie unverschlüsselt
  abgelegt werden. Umsetzung mit der Python-Bibliothek `pyrage`, die ohne externes Binary auskommt.
- **Ziele:**
  - Lokal: `PLATFORM_BACKUP_DIR`, Aufbewahrung `PLATFORM_BACKUP_KEEP_DAYS`.
  - Optional rclone-Remote für S3-kompatible Speicher oder SFTP: `PLATFORM_BACKUP_RCLONE_REMOTE`. `rclone` kommt
    ins Image.
- **Status:** Tabelle `platform_backups` (nicht mandantenbezogen) mit Zeit, Größe, Zielen, sha256, Fehler.
  Seite „Plattform-Sicherung“ nur für MSP-Admins (`SuperCtx`), mit Button „Jetzt sichern“.
- **Alarm `platform_backup_failed`:** plattformweit in einer eigenen Tabelle `platform_alerts` (Entscheidung 3). Benachrichtigt werden
  die MSP-Admins per Mail und optional `PLATFORM_WEBHOOK_URL`.
- **`deploy/restore.sh`** auf einem frischen Server:
  1. `install.sh`-Basis.
  2. Archiv mit dem age-Private-Key entschlüsseln (Schlüssel wird nur für den Restore übergeben).
  3. `.env`, Hub-Volume und `pg_restore` einspielen, Influx optional.
  4. Stack starten.

  Gleicher `WG_HUB_ENDPOINT`/DNS und gleicher Hub-Schlüssel, also verbinden sich die Router ohne Eingriff wieder.
- **Doku:** `docs/DISASTER-RECOVERY.md` beschreibt Schlüsselverwahrung (offline, zwei Personen), Restore-Schritte,
  eine vierteljährliche Testwiederherstellung und Checklisten.
- **Tests:**
  - Archiv/Manifest/Verschlüsselung/Aufbewahrung als Unit-Tests.
  - Echter Test pg_dump → neue Datenbank `sdwan_restore_test` → pg_restore → Zeilen je Tabelle und Stichproben
    vergleichen. Läuft nur, wenn `pg_dump` und eine Postgres-Test-URL verfügbar sind (hier vorhanden), sonst skip.
  - Alarm-Test.

## Phase 22 – Zwei-Faktor-Anmeldung (TOTP)
- **TOTP:** eigene RFC-6238-Implementierung (`app/totp.py`, geprüft gegen die RFC-Testvektoren), Toleranz ±1
  Schritt, Wiederverwendungsschutz über den zuletzt genutzten Zeitschritt.
- **Benutzerfelder (Migration):** `totp_secret_enc`, `totp_enabled`, `totp_last_step`, `recovery_codes`
  (gehasht, 10 Stück), `failed_logins`, `locked_until`.
- **Login-Fluss:**
  - `POST /auth/login` antwortet bei aktiver 2FA `{mfa_required, mfa_token}` (JWT `typ=mfa`, 5 min), dann
    `POST /auth/login/2fa` mit Code oder Wiederherstellungscode.
  - Ist 2FA Pflicht, aber nicht eingerichtet: `{mfa_setup_required, setup_token}`, dann `/auth/2fa/setup` (QR,
    Secret) und `/auth/2fa/enable`. Das Access-Token gibt es erst danach.
- **Einstellungen:** Pflicht je Mandant in `tenant.settings.require_2fa` (Mandanten-Seite, Admin/MSP). Für
  MSP-Admins immer Pflicht, konfigurierbar über `MFA_ENFORCE_SUPERUSER` (Default true).
  - In `tests/conftest.py` wird der Wert auf false gesetzt, damit die bestehenden Tests unverändert bleiben;
    2FA-Tests schalten ihn ein.
- **Verwaltung:**
  - Deaktivieren nur mit gültigem Code.
  - Zurücksetzen durch den MSP-Admin: `POST /users/{id}/2fa/reset`, mit Audit und Webhook `PLATFORM_WEBHOOK_URL`.
- **Rate-Limit und Sperre:**
  - Je Konto und je IP (Passwort und TOTP): nach 5 Fehlversuchen 15 min Sperre (`locked_until`), IP-Limit 30/10 min.
  - Audit-Einträge `auth.login_failed`, `auth.locked`, `auth.2fa_failed`.
- **Frontend:** Login zweistufig, Einrichtungsseite mit QR und Wiederherstellungscodes (Download/Druck),
  Profil-Menü „Zwei-Faktor“, Benutzerliste mit 2FA-Status und Reset.
- Kein SSO.

## Phase 23 – Sicherheitsmeldungen und Mindestversionen
- **Modell `security_advisories`:** global, Seed + MSP-pflegbar. Felder: cve, title, affected_from, fixed_in,
  affected_to (optional), function (general, hotspot, wlan, vrrp, wireguard, dns, rest-api, api, winbox, www,
  ssh, other), severity, link, enabled, builtin/seed_key.
- **Seed:** nur die Struktur plus ein deutlich markierter, deaktivierter Beispiel-Eintrag. Echte Meldungen trägt
  der MSP ein (keine geratenen Versionsbereiche, kein Scraping).
- **Versionsvergleich:** `services/advisories.py` parst `7.15.3`, rc und beta.
- **Aktive Funktionen je Gerät:**
  - Verwaltete Features: Hotspot-Instanz, WLAN-State, VRRP, Mesh. wireguard gilt immer als aktiv (Mgmt-Tunnel).
  - Dienste aus `facts.services`: Der Info-Poll-Hook liest zusätzlich `/ip/service` alle 10 min.
  - Unbekannte Funktion → „möglicherweise betroffen“ (sichtbar, aber kein Alarm).
- **Anzeige:**
  - Geräteliste: Pill in der RouterOS-Zelle.
  - Gerätedetail: Karte.
  - Firmware-Seite: Spalte sowie „behoben in“.
  - Dashboard: Kachel.
  - Seite „Sicherheitsmeldungen“ (Pflege nur MSP).
- **Alarmtyp `security_advisory`** ist gerätebezogen und fügt sich ins bestehende Muster ein.
- **Blockieren:** Hotspot `create_instance`/`apply_instance` und WLAN `apply_profile`/`rotate`/Geräte-Abgleich
  liefern 409, wenn eine Meldung mit Schweregrad high/critical für die Funktion die Geräteversion betrifft.
  Hinweis „erst Firmware aktualisieren“ mit Link `/firmware`. Entfernen bleibt immer erlaubt.
- **Compliance:** Regeltyp `no_security_advisory` in `LIVE_TYPES`; die Daten kommen über `live["advisories"]`,
  die `evaluate_device` befüllt. Die Regel wird in die MSP-Baseline (Seed) aufgenommen.

## Phase 24 – Vor-Ort-Zugang (Break-Glass) und API-Tokens
- **Modell `local_access` je Gerät:** username, password_enc, enabled, status, error, rotated_at, viewed_at,
  mac_winbox_before, service_port-Konfiguration und Vorzustand.
- **Mandanten-Einstellungen:**
  - `local_admin_name` (Default `localadmin`).
  - `local_access_rotate_days` (optional), `rotate_after_view` (Default aus, dann Rotation 4 h nach Anzeige).
  - `local_access_age_recipient`, `local_access_webhook_url` (verschlüsselt).
- **Router-Objekte:**
  - Gruppe `sdwan-local` (volle Rechte, Liste als ANNAHME) und Benutzer `<name>`.
  - Kommentar `sdwan:local`; `address=` die lokalen Netze. Diese werden aus den Adressen der Interfaces in den
    Zonen Management/LAN ermittelt, sonst aus der Bridge. Lassen sie sich nicht ermitteln, wird nichts angelegt.
    Nie WAN.
- **Firewall:** Die Interface-Liste `sdwan-local-access` (Kommentar `sdwan:local`) enthält die Interfaces der
  Zonen Management und LAN, solange der Zugang aktiv ist.
  - Neue Grundregel `base:local-access` (tcp 22,8291 aus `in-interface-list=sdwan-local-access`) vor
    `base:mgmt-only`, immer kompiliert.
  - Bei leerer Liste wirkungslos, deshalb ändert sich an bestehenden Geräten nichts.
  - Aus WAN nie, weil WAN-Interfaces ausgeschlossen werden (Validierung plus Test).
- **MAC-Winbox:** `/tool/mac-server/mac-winbox allowed-interface-list=sdwan-local-access`. Der Vorzustand wird
  gemerkt und beim Deaktivieren/Offboarding zurückgestellt (ANNAHME Feldname).
- **Service-Port (optional, Default aus, je Gerät/ZTP-Vorlage):**
  - Port aus der Bridge nehmen (Vorzustand merken), Default-Netz `192.168.254.0/29` (änderbar).
  - IP, Pool, DHCP-Server, Netzwerk (`sdwan:local:sp`), Zone Management und Aufnahme in die Liste.
- **Anlegen:** beim Onboarding (Zeilen im Pair-Response-Script), per ZTP (`provision_device`, Vorlagen-Option
  `local_access: true`), per Button und per Massenaktion (Targets).
- **Passwort:** je Router zufällig, 24 Zeichen aus einem Alphabet ohne 0/O/l/1/I. Kein gemeinsames Passwort.
- **Anzeige:** nur Admin/MSP-Admin (nicht über Token), mit Begründung, Audit und Webhook.
- **Rotation:** manuell, nach Anzeige oder im Intervall.
  - Nur dieser Benutzer wird geändert. Bei einem Router-Fehler bleibt das alte Passwort gespeichert und gültig.
  - Nach jeder Rotation automatischer Export per Webhook (nur age-verschlüsselt, nie Klartext).
- **Export:** KeePass-CSV (Group, Title, Username, Password, URL, Notes) als age-Datei, oder als AES-ZIP
  (`pyzipper`) mit eingegebenem Passwort. UI-Hinweis „Vor-Ort-Passwörter müssen auch ohne Plattform verfügbar sein“.
- **Offboarding:** Option „Vor-Ort-Zugang behalten“ (Default an).
  - Behalten: Schritt 6 setzt den Kommentar von Benutzer und Gruppe auf „lokaler Zugang (ehemals verwaltet)“ statt
    sie zu entfernen (Script-Befehl vor dem generischen `remove`).
  - Liste und MAC-Server-Einstellung bleiben passend erhalten: Die Liste wird umbenannt bzw. übernommen, ANNAHME.
- **Compliance:** Regeltyp `local_admin_present` in die MSP-Baseline.
- **API-Tokens (`api_tokens`):** user_id, tenant_id, name, prefix, sha256-Hash, scope (`role`|`read`), expires_at,
  last_used_at, revoked_at.
  - Bearer `sdw_…` wird in `deps._user_from_token` vor dem JWT erkannt. `Ctx.via_token`; die Rolle ist
    min(Benutzerrolle, readonly bei `read`).
  - Tokens können keine Vor-Ort-Passwörter anzeigen oder exportieren, keine 2FA verwalten und keine Tokens
    erstellen (403).
  - Audit `api_token.use` bei schreibenden Methoden.
  - OpenAPI-Security-Scheme `ApiToken` und Doku-Abschnitt.
  - UI „API-Tokens“ im Profil; der Token wird nur einmal im Klartext angezeigt.

## Phase 25 – Nachbarn, Top-Verbraucher, Inventar, ZTP-Import
- **Nachbarn:** Poll-Hook alle 10 min liest `/ip/neighbor` (identity, platform, mac-address, address, interface,
  board, version; ANNAHME Felder) in die Tabelle `device_neighbors` (ersetzen je Gerät).
  - Tab „Nachbarn“ nur, wenn Einträge vorhanden sind.
  - Standort-Topologie auf der Standortseite: SVG mit Router → Nachbarn je Interface, Abgleich per MAC/Identity
    mit Plattform-Geräten.
- **Top-Verbraucher:** opt-in je Gerät.
  - `/ip/traffic-flow` (enabled, interfaces = WAN-Interfaces, Vorzustand merken) und
    `/ip/traffic-flow/target` (Hub-IP:2055, version=ipfix, `sdwan:flow`); ANNAHME Felder.
  - Neuer Container `flows` (Muster Syslog) mit eigenem IPFIX-Parser (Templates, IEs 1, 2, 8, 12, 10, 14; ANNAHME).
    Aggregation je 5 min in `flow_aggregates` (Gerät, WAN, Quelle, Ziel, Bytes, Pakete).
  - Auswertung Top-Hosts/Top-Ziele je WAN und Zeitraum. Aufbewahrung je Mandant (Default 7 Tage), Lösch-Job.
  - Datenschutz-Hinweis im UI.
- **Inventar:** Tabelle `device_inventory` (Kaufdatum, Garantie bis, Lieferant, Notizen; Seriennummer/Modell aus
  dem Gerät).
  - EOL-Liste `eol_models` als Seed (Struktur und Beispiel deaktiviert), MSP-pflegbar, Warnung bei Treffer bzw.
    Ablauf.
  - Seite „Inventar“ mit CSV-Export je Mandant.
- **ZTP-Massenimport:** `POST /ztp/import/preview` mit CSV-Text (Seriennummer, Modell, Standort, Vorlage;
  Standort/Vorlage per Name).
  - Validierung (Format, Duplikate, bereits vorhanden, unbekannter Standort/Vorlage) liefert Zeilen mit Fehlern.
  - `POST /ztp/import/commit` legt nur gültige Zeilen nach Bestätigung über die bestehende Stage-Logik an
    (`api/v1/ztp.py stage`, in einen Service ausgelagert).
  - UI-Dialog auf der ZTP-Seite.

## Entscheidungen
1. **Sicherung ohne Recipient:** Ohne age-Public-Key wird keine Sicherung geschrieben (Status „nicht konfiguriert“).
   Grund: `.env` enthält Schlüssel.
2. **Ablauf und Werkzeuge:** Die Sicherung läuft im Worker-Container (Volumes read-only). `backup.sh` ist nur der
   Aufruf. rclone deckt S3 und SFTP mit einem Werkzeug ab. `pyrage` statt age-Binary.
3. **Plattform-Alarm:** eigene Tabelle `platform_alerts` statt `Alert.tenant_id` nullable. Grund: `Alert` ist
   `TenantScoped` mit automatischem Mandanten-Filter; eine Änderung dort würde bestehendes Verhalten berühren.
4. **2FA für MSP-Admins** Pflicht per Setting (Default an), in den Tests aus. Bestehende Sitzungen bleiben gültig;
   die Pflicht greift bei der nächsten Anmeldung.
5. **Eigene TOTP-Implementierung** (RFC-Testvektoren) statt zusätzlicher Abhängigkeit.
6. **Sicherheitsmeldungen:** Seed ohne echte CVE-Daten (nur deaktiviertes Beispiel). Grund: Versionsbereiche nicht
   raten. Unbekannte Funktion → „möglicherweise betroffen“ ohne Alarm.
7. **Blockade:** gilt für Aktivieren und Ausrollen, Entfernen bleibt erlaubt.
8. **Vor-Ort-Zugang:** Die Firewall-Ausnahme läuft über die Liste `sdwan-local-access`. Die Regel ist immer
   kompiliert, bleibt aber ohne Mitglieder wirkungslos, also keine Verhaltensänderung an bestehenden Geräten.
   Ohne ermittelbare lokale Netze wird kein Vor-Ort-Benutzer angelegt.
9. **Offboarding:** Vor-Ort-Zugang standardmäßig behalten, dann nicht mehr als `sdwan:` markiert.
10. **Export:** Webhook-Export nur age-verschlüsselt; ZIP mit AES (`pyzipper`), kein ZipCrypto.
11. **API-Tokens:** gehasht, nie für Vor-Ort-Passwörter, 2FA oder Token-Verwaltung.
12. **Seeds:** EOL-Liste und Sicherheitsmeldungen enthalten nur Struktur plus deaktivierte Beispiele.
13. **Flows:** 5-min-Aggregate statt Einzel-Flows (Datenschutz, Datenmenge); Default-Aufbewahrung 7 Tage; opt-in.
14. **2FA-Notfall per CLI:** nur mit Server-Zugriff.
    - `docker compose exec api python -m app.cli reset-2fa <email>` und `… python -m app.cli unlock <email>`
      (Sperre aufheben).
    - Beide schreiben einen Audit-Eintrag (Akteur `cli`) und senden den Plattform-Webhook.
    - Dokumentiert in `docs/DISASTER-RECOVERY.md`.
    - Grund: Ein ausgesperrter einziger MSP-Admin darf die Plattform nicht unbenutzbar machen; wer Server-Zugriff
      hat, hat ohnehin volle Kontrolle.
15. **Vor-Ort-Zugang und MAC-WinBox:**
    - Das Netz des Service-Ports wird immer in `address=` des Vor-Ort-Benutzers aufgenommen.
    - Mandanten-Einstellung `local_admin_address_restrict` (Default an). Aus bedeutet `address=` leer. Der Schutz
      aus WAN bleibt dann über Firewall (`sdwan-local-access`), MAC-Server-Liste und `/ip service address`
      bestehen; bei aus setzt die Plattform die Dienst-Adressen winbox/ssh auf lokale Netze plus Tunnel. Die
      Änderung wird gemerkt und beim Deaktivieren zurückgestellt.
    - Beide Varianten werden implementiert und getestet.
    - ANNAHME (Labor), ausdrücklich zu prüfen: Anmeldung per MAC-WinBox mit address-beschränktem Benutzer.
      LABORTEST-Schritt mit Ergebnisfeld; scheitert sie, wird der Default auf aus geändert.
16. **Vor-Ort-Benutzer nicht anlegbar** (keine lokalen Netze ermittelbar):
    - Status `not_created` mit Grund, sichtbar im Gerätedetail und in der Geräteliste (Pill).
    - Erlaubte Netze je Gerät manuell angebbar (`manual_networks`). Validierung: keine Netze aus WAN-Interfaces
      bzw. WAN-Adressen, kein `0.0.0.0/0`, nur gültige CIDR. Danach wird angelegt.
    - Die Compliance-Regel `local_admin_present` schlägt dann fehl, kein stilles Grün.
17. **Rechte der Vor-Ort-Gruppe `sdwan-local`:** alle Policies der API-Gruppe außer `api`
    (`read, write, policy, reboot, test, ssh, sensitive, winbox, web`), nicht die eingebaute Gruppe `full`.
    Grund: Nach der bestehenden Annahme (Phase A1/A2) kann der API-Benutzer keine Gruppe mit mehr Rechten anlegen als
    er selbst hat. Die API-Gruppe dafür zu erweitern, wäre die bequemere, aber unsicherere Variante.
18. **Firewall-Ausnahme vor den Benutzerregeln:** `base:local-access` (tcp 22/8291) und `base:local-access-dhcp`
    (udp 67 für den Service-Port) stehen direkt nach established/invalid, also vor allen Policy-Regeln und vor
    `base:mgmt-only`. Keine Policy kann den Vor-Ort-Zugang aus LAN/Management aussperren; aus dem WAN nie, weil
    WAN-Interfaces nie Mitglied der Liste werden.
19. **Bestehende Geräte:** Es wird nichts automatisch angelegt. Automatisch nur nach dem Pairing (Onboarding/ZTP,
    abschaltbar je Mandant), sonst per Button oder Massenaktion. Grund: keine Router-Änderung ohne Benutzeraktion.
20. **Benutzername:** wird beim Anlegen des Datensatzes festgelegt; eine Namensänderung in den Einstellungen gilt
    für neue Geräte (kein Umbenennen auf bestehenden Routern).
21. **Offboarding „behalten“:** Objekte bekommen den Kommentar „lokaler Zugang (ehemals verwaltet)“; ein
    Service-Port wird zusätzlich in die defconf-Liste `LAN` aufgenommen (falls vorhanden), damit die wieder
    aktivierte Werks-Firewall ihn nicht verwirft; bei „Adressbeschränkung aus“ bleiben winbox/ssh auf die lokalen
    Netze beschränkt (ohne Tunnel).
22. **API-Tokens:**
    - Ablauf ist Pflicht (1–365 Tage, Default 90).
    - `read`-Tokens werden zentral bei jeder schreibenden Methode abgewiesen.
    - Tokens umgehen die 2FA nicht im eigentlichen Sinn: Sie werden nur in einer angemeldeten (ggf. 2FA-)Sitzung
      erstellt.
    - Auch die Einstellungen des Vor-Ort-Zugangs (Export-Webhook, age-Empfänger) sind per Token gesperrt, weil
      darüber Passwörter umgeleitet werden könnten.
23. **Compliance „API/SSH nur aus dem Tunnel“** toleriert die Netze eines aktiven Vor-Ort-Zugangs (bewusst
    lokal erlaubt); ohne aktiven Zugang unverändert.
24. **Nachbarn in eigener Tabelle** (je Poll ersetzt) statt in `facts`: Die Geräteliste lädt `facts`, Nachbarlisten
    würden sie aufblähen. In `facts` steht nur die Anzahl (Tab nur sichtbar, wenn > 0). Zuordnung zu
    Plattform-Geräten über Identity oder Adresse.
25. **Top-Verbraucher:**
    - Nur IPFIX (v10), kein NetFlow v5/v9.
    - Gespeichert wird „lokaler Host ↔ Gegenstelle je WAN“ ohne Ports; nur öffentlich↔privat wird gezählt.
    - Je Gerät und Speicherlauf höchstens 200 Paare je 5-Minuten-Intervall, der Rest als „andere“.
    - Exportiert werden nur die WAN-Interfaces der WAN-Konfiguration (ohne WAN-Konfiguration kein Einschalten).
    - Recht: Techniker (wie Syslog); Aufbewahrung 1–90 Tage je Mandant, Default 7.
26. **EOL-Abgleich:** exakter Modellvergleich (ohne Groß-/Kleinschreibung und Leerzeichen), keine Muster – im Zweifel
    kein Treffer statt falscher Warnung. Garantie-Hinweis 60 Tage vor Ablauf.
27. **ZTP-Import:** Anlegen über dieselbe Logik wie „Geräte vorbereiten“ (in `services/ztp_import.stage_device`
    ausgelagert, Verhalten unverändert). Beim Anlegen wird erneut geprüft; nur gültige Zeilen werden angelegt,
    und nur mit `confirm=true`. Zusätzlich zur bisherigen Prüfung: Gerätename darf im Mandanten nicht doppelt sein
    (nur beim Import, damit bestehendes Verhalten unverändert bleibt). Modell aus der CSV wird als Erwartung gespeichert.

## Verifikation
- Je Phase eine neue Testdatei: `test_phase21_platform_backup.py` … `test_phase25_*.py`.
  - Simulator-Tests für alle Router-Pfade.
  - Sicherheitstests: Token-Grenzen, Sperre, Replay.
  - Vor-Ort-Zugang: Default-Drop blockiert WinBox/SSH aus LAN/Management nicht, WAN nie; Rotation mit Fehler
    behält das alte Passwort.
  - DR-Test gegen echtes Postgres.
- Komplette pytest-Suite, `npm run build`, Screenshots neuer Seiten (hell/dunkel), Selbsttest grün mit neuen
  `PATH_SPECS`.
- Abschlussbericht im Plan-Dokument: Commits, Entscheidungen, Weggelassenes, vollständige Labor-Liste.

## Stand der Phasen

### Stand Phase 21 – Plattform-Sicherung und Disaster Recovery
- **Erledigt:**
  - Sicherungsmodul mit CLI, täglichem Worker-Job und Anforderung aus der Oberfläche.
  - age-Verschlüsselung (pyrage), lokale Aufbewahrung, optionales rclone-Ziel (S3/SFTP).
  - Status-Seite und Plattform-Alarm mit Mail/Webhook.
  - `deploy/backup.sh`, `deploy/restore.sh`, `docs/DISASTER-RECOVERY.md`.
  - Test: Dump einer echten PostgreSQL-DB, Restore in eine frische DB, Zeilen und Inhalte verglichen.
  - Migration 0030 (neue Tabellen).
- **Weggelassen:**
  - Automatisches Einspielen der InfluxDB-Sicherung (manueller Schritt in der Anleitung).
  - Sicherung ohne Verschlüsselung (bewusst nicht möglich).
- **Im Labor zu verifizieren:**
  - `postgresql-client-16` im Image und `pg_dump` gegen den Produktivserver.
  - rclone-Ziele und Aufbewahrung extern.
  - `influx backup`/`restore`.
  - Kompletter Restore auf einer frischen VM mit DNS-Umstellung (Router verbinden sich ohne Eingriff).

### Stand Phase 22 – Zwei-Faktor-Anmeldung (TOTP)
- **Erledigt:**
  - TOTP nach RFC 6238 (eigene Implementierung, Testvektoren) mit Wiederverwendungsschutz; 10 gehashte
    Wiederherstellungscodes.
  - Zweistufige Anmeldung mit erzwungener Einrichtung, Pflicht je Mandant und für MSP-Admins.
  - Deaktivieren nur mit Code (nicht bei Pflicht).
  - Sperre nach Fehlversuchen (Passwort und 2FA) und IP-Limit, Audit-Einträge.
  - Reset durch den MSP-Admin mit Webhook, Entsperren, CLI `reset-2fa`/`unlock`.
  - Oberfläche: Login, Profil-Dialog, Benutzerliste.
  - Migration 0031 (neue Spalten mit Defaults).
- **Entscheidung:** Die IP-Sperre zählt nur Fehlversuche (erfolgreiche Anmeldungen hinter einem gemeinsamen
  NAT sollen nicht blockieren).
- **Weggelassen:** WebAuthn/FIDO2 und „Gerät merken“ (nicht gefordert; sicherere Variante ohne Ausnahmen).
- **Im Labor zu verifizieren:** Kompatibilität mit gängigen Authenticator-Apps (QR/otpauth), Server-Uhrzeit (NTP).

### Stand Phase 23 – Sicherheitsmeldungen und Mindestversionen
- **Erledigt:**
  - Modell und Pflege (MSP) mit Versionsbereichen; Seed nur als deaktiviertes Beispiel.
  - Betroffenheit je Version und aktiver Funktion (verwaltete Features, `/ip/service` im Poll), Status
    „möglicherweise“ bei unbekannter Funktion.
  - Anzeige in Geräteliste, Gerätedetail, Firmware-Seite, Dashboard und eigener Seite; Alarmtyp
    `security_advisory`.
  - Blockade von Hotspot und WLAN bei high/critical.
  - Compliance-Regel in der MSP-Baseline (Test auf 7 Regeln angepasst, gewollte Änderung).
  - Migration 0032 (neue Tabelle).
- **Entscheidungen:**
  - Der Alarm ist nicht in den Standardregeln (bestehende Mandanten unverändert).
  - „Möglicherweise betroffen“ löst keinen Alarm aus.
  - Ohne bekannte Geräteversion wird nicht blockiert.
- **Weggelassen:** automatischer Import von Meldungen (laut Auftrag kein Scraping); Blockade weiterer Funktionen
  (VRRP, Mesh), weil diese für den Betrieb nötig sind – dort nur Anzeige und Alarm.
- **Im Labor zu verifizieren:** Versionsformat von `/system/resource` bzw. `routeros_version` bei rc/beta; Felder
  von `/ip/service` (disabled).

### Stand Phase 24 – Vor-Ort-Zugang (Break-Glass) und API-Tokens
- **Erledigt:**
  - Modell `local_access` je Gerät und `api_tokens` (Migration 0033, nur neue Tabellen).
  - Vor-Ort-Benutzer mit eigenem Zufallspasswort (24 Zeichen ohne 0/O/l/1/I, verschlüsselt gespeichert) in Gruppe
    `sdwan-local`.
  - Lokale Netze aus den Zonen Management/LAN bzw. der defconf-Liste `LAN`, WAN ausgeschlossen (WAN-Konfiguration,
    Listen `sdwan-wan`/`WAN`, DHCP-Clients); manuell angebbare Netze mit Validierung.
  - Status `not_created` mit Grund in Gerätedetail, Geräteliste und Übersicht.
  - Firewall-Grundregel, Interface-Liste `sdwan-local-access`, MAC-WinBox auf diese Liste (Vorzustand gemerkt).
  - Beide Varianten der Adressbeschränkung (an: `address=`; aus: `/ip service` winbox/ssh, Vorzustand gemerkt).
  - Optionaler Service-Port (Bridge-Port, Adresse, Pool, DHCP-Server und -Netz; Default-Netz 192.168.254.0/29
    änderbar), sein Netz immer in `address=`.
  - Anlegen nach dem Pairing (Post-Poll-Hook), per Button und per Massenaktion (Geräteliste, Seite
    „Vor-Ort-Zugang“).
  - Anzeige nur Admin/MSP-Admin mit Begründung, Audit und Webhook; Rotation manuell, nach Anzeige (4 h) oder im
    Intervall (Worker stündlich); bei Router-Fehler bleibt das alte Passwort.
  - Export als KeePass-CSV, age oder AES-ZIP; automatischer age-Export per Webhook nach Anlegen/Rotation.
  - Offboarding-Option „Vor-Ort-Zugang behalten“ (Default an).
  - Compliance-Regel „Vor-Ort-Zugang vorhanden“ in der MSP-Baseline (Test auf 8 Regeln angepasst, gewollte
    Änderung).
  - API-Tokens: `sdw_…`, gehasht, Ablauf, nur lesend oder Rolle, letzte Nutzung, widerrufbar,
    Audit `api_token.use`, OpenAPI-Beschreibung, Sperren für Vor-Ort-Passwörter, Export, 2FA und
    Token-Verwaltung; Dialog im Profil, Admin-Übersicht über `/api-tokens`.
  - `PATH_SPECS`: mac-winbox, bridge/port, ip/pool, dhcp-server, dhcp-server/network, dhcp-client; Simulator-Handler.
- **Weggelassen:**
  - Service-Port als Option der ZTP-Vorlage (nur je Gerät; im Zweifel weglassen, weil der Port je Modell
    verschieden ist).
  - „Volle Rechte“ im Sinne der Gruppe `full` (siehe Entscheidung 17).
  - Umbenennen bestehender Vor-Ort-Benutzer bei Namensänderung (Entscheidung 20).
- **Im Labor zu verifizieren:**
  - **MAC-WinBox-Anmeldung mit address-beschränktem Benutzer** (LABORTEST 21, Ergebnisfeld). Scheitert sie, wird der
    Default von `local_admin_address_restrict` auf aus geändert.
  - Anlegen der Gruppe `sdwan-local` mit den Policies aus `LOCAL_POLICIES` durch den API-Benutzer.
  - Feldname `allowed-interface-list` von `/tool/mac-server/mac-winbox`.
  - Service-Port: Bridge-Port entfernen/wiederherstellen, DHCP-Server-Felder (`address-pool`, `interface`), Vergabe an
    ein Notebook.
  - Firewall: WinBox/SSH aus LAN und Management bei Default-Drop, nicht aus dem WAN; MAC-WinBox nicht über WAN.
  - Offboarding „behalten“: Anmeldung mit dem Vor-Ort-Benutzer nach Wiederherstellung der Werks-Firewall.

### Stand Phase 25 – Nachbarn, Top-Verbraucher, Inventar, ZTP-Import
- **Erledigt:**
  - Migration 0034 (nur neue Tabellen: `device_neighbors`, `device_flows`, `flow_aggregates`, `device_inventory`,
    `eol_models`).
  - Nachbarn: Poll-Hook alle 10 min (`/ip/neighbor`), Tab „Nachbarn“ (nur mit Einträgen), Standort-Topologie als SVG
    (Sites → „Topologie“), Verlinkung erkannter Plattform-Geräte.
  - Top-Verbraucher: Opt-in je Gerät (Tab „Top-Verbraucher“), `/ip/traffic-flow` + Ziel `sdwan:flow` mit gemerktem
    Vorzustand, IPFIX-Collector als eigener Container (`flows`, UDP 2055 auf der Hub-IP), eigener IPFIX-Parser,
    5-Minuten-Aggregate, Top-Hosts/Top-Ziele je WAN und Zeitraum, Aufbewahrung je Mandant mit Lösch-Job,
    Datenschutz-Hinweis.
  - Inventar: Seite „Inventar“ mit Kaufdatum, Garantie, Lieferant, Notizen; Seriennummer/Modell aus dem Gerät;
    EOL-Liste als Seed (nur deaktiviertes Beispiel) und MSP-Pflege; Hinweise (EOL, Garantie abgelaufen/läuft ab);
    CSV-Export je Mandant.
  - ZTP-Massenimport: CSV (Datei oder Text) → Vorschau mit Fehlern je Zeile → Bestätigung → Anlegen gültiger Zeilen,
    danach Bootstrap-Scripts wie bei „Mehrere vorbereiten“.
  - `PATH_SPECS`: `/ip/neighbor`, `/ip/traffic-flow`, `/ip/traffic-flow/target`; Simulator-Handler.
- **Weggelassen:**
  - NetFlow v5/v9 und IPv6-spezifische Auswertung über die Adressfelder hinaus (IPv6-Adressen werden erkannt,
    aber nicht gesondert getestet).
  - Alarm bei EOL/Garantieablauf (nur Hinweis im Inventar; kein Alarmtyp ohne Auftrag).
  - Automatisches Umbenennen/Verknüpfen von Nachbarn über LLDP-Chassis-IDs (nur Identity/Adresse).
- **Im Labor zu verifizieren:**
  - Felder von `/ip/neighbor` (u. a. `interface` bei Bridge-Ports als `ether2,bridge`).
  - `/ip/traffic-flow set enabled=yes interfaces=…` und `/ip/traffic-flow/target add dst-address port version=ipfix`.
  - IPFIX-Pakete von RouterOS 7: Template-IDs, Informationselemente 1/2/8/12/10/14, Länge der Zähler (4/8 Byte),
    Interface-Index = Nummer aus `.id` von `/interface`.
  - Erreichbarkeit des Collectors (UDP 2055) über den Tunnel; Container `flows` im Netz-Namespace des Hubs.
  - Modellbezeichnungen (board-name) für den EOL-Abgleich.

## Abschlussbericht Phasen 21–25

### Commits
| Commit | Inhalt |
|--------|--------|
| 183aea8 | Plan für die Phasen 21–25 |
| dbb7db0 | Phase 21: Plattform-Sicherung und Disaster Recovery |
| 5d8cfb7 | Phase 22: Zwei-Faktor-Anmeldung (TOTP), Sperre nach Fehlversuchen |
| d009f8a | Phase 23: Sicherheitsmeldungen und Mindestversionen |
| b3307ee | Phase 24: Vor-Ort-Zugang (Break-Glass) und API-Tokens |
| (dieser Commit) | Phase 25: Nachbarn, Top-Verbraucher, Inventar, ZTP-Import; Abschlussbericht |

Davor in derselben Sitzung: bcd7e24 (defconf-Behandlung), 57e706e (defconf-Fingerabdruck), b1de1ed (Offboarding).

### Entscheidungen
Siehe Abschnitt „Entscheidungen“ (1–27) sowie die Entscheidungen in den Abschnitten „Stand Phase …“.

### Weggelassen (gesammelt)
- Phase 21: automatisches Einspielen der InfluxDB-Sicherung; Sicherung ohne Verschlüsselung.
- Phase 22: WebAuthn/FIDO2, „Gerät merken“, SSO (laut Auftrag).
- Phase 23: automatischer Import/Scraping von Meldungen; Blockade von VRRP/Mesh.
- Phase 24: Service-Port als ZTP-Vorlagen-Option; Gruppe mit „vollen Rechten“ (`full`); Umbenennen bestehender
  Vor-Ort-Benutzer.
- Phase 25: NetFlow v5/v9; Alarm für EOL/Garantie; LLDP-Chassis-Zuordnung.

### Im Labor zu verifizieren (vollständig)
1. Phase 21: `postgresql-client-16` und `pg_dump` im Image gegen den Produktivserver; rclone-Ziele (S3/SFTP) und
   Aufbewahrung; `influx backup`/`restore`; vollständiger Restore auf frischer VM mit DNS-Umstellung.
2. Phase 22: Authenticator-Apps (QR/otpauth), Server-Uhrzeit (NTP).
3. Phase 23: Versionsformat bei rc/beta in `/system/resource`; Felder von `/ip/service` (`disabled`).
4. Phase 24:
   - MAC-WinBox-Anmeldung mit address-beschränktem Benutzer (LABORTEST 21, Ergebnisfeld; bei Fehlschlag Default
     der Adressbeschränkung auf aus).
   - Gruppe `sdwan-local` mit `LOCAL_POLICIES` durch den API-Benutzer anlegbar.
   - `/tool/mac-server/mac-winbox allowed-interface-list`.
   - Service-Port: Bridge-Port, DHCP-Server-Felder, DHCP-Vergabe.
   - WinBox/SSH aus LAN/Management bei Default-Drop, nicht aus dem WAN; MAC-WinBox nicht über WAN.
   - Offboarding „behalten“: Anmeldung nach Wiederherstellung der Werks-Firewall.
5. Phase 25: `/ip/neighbor`-Felder; `/ip/traffic-flow`- und Ziel-Felder; IPFIX-Templates/IEs/Zählerlängen und
   Interface-Index; UDP 2055 über den Tunnel; board-name für den EOL-Abgleich.
6. Nachträge (defconf/Offboarding): siehe LABORTEST 10 (Werkszustand) und 17 (Offboarding).

