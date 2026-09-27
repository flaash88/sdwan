# Plan: Behebung der Audit-Funde (AP1–AP4)

Grundlage ist `docs/AUDIT-2026-09.md`. Umgesetzt werden die Arbeitspakete AP1–AP4 und zusätzlich AUDIT-028, für den Vorgaben vorliegen. AP5–AP7 kommen in einem späteren Durchgang.

## Arbeitsweise

**Commits:** je Arbeitspaket ein Commit und ein Push auf `claude/mikrotik-sdwan-platform-qs2sti`.

**Tests:**
- Für jeden behobenen Fund wird der xfail(strict)-Test in `backend/tests/audit/` zum normalen Test.
- Funde ohne Test bekommen einen neuen, soweit möglich.
- Die komplette Suite ist grün, `npm run build` läuft fehlerfrei.

**Bericht:** In `docs/AUDIT-2026-09.md` steht je Fund der Status „behoben in `<commit>`“ bzw. „teilweise behoben“ mit Begründung.

**Anhalten nur, wenn:**
- Tests nicht grün zu bekommen sind,
- eine Änderung bestehende Router-Konfiguration ohne Benutzeraktion verändern würde,
- eine Migration Daten verlieren würde.

Alle Router-Änderungen in diesem Plan greifen nur bei einer Benutzeraktion: Onboarding, ZTP-Bootstrap, „Rechte einschränken“ oder WAN anwenden. Migrationen sind nur additiv.

## AP1 – Router-Injection und Transport

**Script-Escaping zentral** in `app/routeros/naming.py`:
- `routeros_str(value)`: RouterOS-String in Anführungszeichen, maskiert `\`, `"` und `$`. Steuerzeichen werden abgelehnt.
- `script_comment(value)`: Kommentarzeile ohne Zeilenumbruch, nur sichere Zeichen.
- `validate_label(value)`: für Freitext-Namen. Keine Steuerzeichen, keine RouterOS-Sonderzeichen `"\$[]{};`, 1–64 Zeichen.

**AUDIT-001:**
- Gerätename wird mit `validate_label` geprüft, beim Anlegen und beim Ändern (Pydantic-Validator), sonst 422.
- Im Onboarding-Script erscheint der Name nur über `script_comment`.
- Test: der xfail-Test wird normal.

**AUDIT-002:**
- WAN-Name (API und ZTP-Vorlage `wan.links[].name`) wird mit `validate_label` geprüft.
- Im Netwatch-Script wird er über `routeros_str` gesetzt.
- Bestehende Namen werden nicht umgeschrieben; die Scripts ändern sich erst beim nächsten „WAN anwenden“.

**AUDIT-026:**
- Alle Validierungs-Regex mit `^…$` plus `match` werden auf `fullmatch` bzw. `\Z` umgestellt: onboarding `_SAFE`, ztp `_IFACE`/`_HOST`/`_TZ`, wan, vrrp, wlan, hotspot, `SERIAL_RE` und die weiteren Fundstellen aus der Suche.
- Neuer Test: je Validator wird ein Wert mit abschließendem `\n` abgelehnt.

**AUDIT-027:**
- `identity_pattern`: Nur die Platzhalter `{tenant} {site} {name} {serial}` sind erlaubt, geprüft per Regex. Ersetzt wird ohne `str.format`.

**AUDIT-051:**
- Script-Bibliothek: Parameterwerte werden beim Einsetzen immer als RouterOS-String gequotet (`routeros_str`), auch an ungequoteten Vorlagenstellen.
- Seriennummer: strenges Muster beim Anlegen und im Bootstrap nur über `script_comment`.
- Content-Disposition mit bereinigtem Namen.

**AUDIT-005:**
- Onboarding-Befehl, Onboarding-Script, Pair-Aufruf und ZTP-Bootstrap aktivieren vor `/tool fetch` den eingebauten Root-Zertifikatsspeicher.
- **ANNAHME** zu Syntax und Mindestversion: `/certificate settings set builtin-trust-anchors=trusted`, ab RouterOS 7.19. Beides wird im LABORTEST geprüft; die Werte liegen als Konstanten in `app/routeros/schema.py`.
- Aufruf über `[:parse "…"]` innerhalb `:do {} on-error={}`. Auf älteren Versionen, die den Parameter nicht kennen, bricht das Script mit `:error "SD-WAN: RouterOS zu alt oder Zertifikat nicht prüfbar – bitte auf RouterOS >= 7.19 aktualisieren"` ab.
- Danach `/tool fetch … check-certificate=yes`. Schlägt die Prüfung fehl, folgt dieselbe `:error`-Meldung; auf `check-certificate=no` wird nie zurückgefallen.
- Im ZTP-Bootstrap: Aktivierung beim Import. Im Wiederhol-Scheduler protokolliert ein Fehlschlag den Hinweis auf Zertifikat oder Version.
- Oberfläche: Hinweis auf die Mindestversion im Onboarding-Dialog und auf der ZTP-Seite.
- Selbsttest: Zeile „Zertifikatsspeicher“ aus `/certificate/settings`. Liefert der Router das Feld nicht, erscheint ein oranger Hinweis.
- LABORTEST: Onboarding im Werkszustand mit aktueller und älterer 7.x.

**AUDIT-007:**
- Hat das Gerät eine Seriennummer, ist sie im Pair-Request Pflicht und muss passen.
- Pairing liest das Gerät mit `SELECT … FOR UPDATE`, damit es kein Doppel-Pairing gibt.

**AUDIT-053:**
- Neue Einstellung `ZTP_TOKEN_TTL_DAYS` (Default 180, wie bisher).
- Das Token wird beim Pairing verbraucht. Der Bootstrap-GET bleibt bewusst nicht verbrauchend, weil der Wiederhol-Scheduler ihn braucht; mit 007 und 005 ist das Token ohne passende Seriennummer wertlos.
- Status: teilweise behoben, mit Begründung.

**AUDIT-028:**
- `ftp` kommt in `API_POLICIES`; `REMOTE_POLICIES` und die Schnittmenge des Vor-Ort-Zugangs bleiben Teilmengen (Test).
- Neue Geräte erhalten `ftp` über das Pair-Script.
- Bestehende Gruppen bekommen es erst bei „Rechte einschränken“ bzw. dessen Abgleich. Lehnt RouterOS die Selbsterweiterung ab (ANNAHME, der Simulator bildet das ab), erscheint eine klare Meldung mit dem Einzeiler für die lokale Konsole.
- Selbsttest: orange, wenn `ftp` fehlt und das Gerät eine Hotspot-Instanz hat.
- MSP-Baseline (Seed): neue Regel „/ip service ftp deaktiviert“.
- LABORTEST: Hotspot-Upload auf echter Hardware.

## AP2 – Anmeldung und Geheimnisse

**AUDIT-003:**
- Eigene ASGI-Middleware `TrustedProxyMiddleware` für HTTP und WebSocket.
- Nur wenn der direkte Absender in `TRUSTED_PROXIES` liegt (IPs/CIDR, Default leer = keinem Header vertrauen), wird `X-Forwarded-For` von rechts nach links gelesen. Vertrauenswürdige Hops werden übersprungen, der erste fremde gilt als Client. `X-Forwarded-Proto` wird dann übernommen.
- Uvicorn läuft mit `--no-proxy-headers` statt `--forwarded-allow-ips "*"`.
- Compose:
  - `frontend` bekommt die feste Adresse `${SDWAN_NET}.30`.
  - `TRUSTED_PROXIES` ist per Default genau diese Adresse.
- install.sh setzt im Caddy-/nginx-Modus zusätzlich das Gateway `${SDWAN_NET}.1`, über das der Host-Proxy ankommt. update.sh ergänzt das bei bestehenden Installationen, wenn die Variable fehlt.
- Wirkt auf Login-Limit, Hotspot-Registrierung, Audit-IP, `last_used_ip` und den Default von `allowed_cidr` beim Fernzugriff (AUDIT-019, Teil IP).
- Doku für den Modus „eigener Proxy“.

**AUDIT-006 und AUDIT-023:**
- `app/secrets_check.py` prüft in Produktion `SECRET_KEY` (Default, `change-me…`, < 32 Zeichen), `HUB_TOKEN`, `BOOTSTRAP_ADMIN_PASSWORD` und `INFLUX_TOKEN` (wenn Influx aktiv).
- Start bricht mit `RuntimeError` ab und nennt die Variable samt Befehl `openssl rand -hex 32`.
- `deploy/check-secrets.sh` prüft `.env` auf dieselben Werte plus `GRAFANA_ADMIN_PASSWORD`, `INFLUX_ADMIN_PASSWORD` und `POSTGRES_PASSWORD`.
- update.sh ruft es nach `git pull` und vor jedem Build/Neustart auf und bricht bei Fund ab, ohne etwas neu zu starten. install.sh ruft es am Ende auf.

**AUDIT-009:**
- `GET /backups/{id}` und `/diff` maskieren für Rollen unter Techniker (auch `read`-Tokens) mit der Maskierung der Config-Suche.
- `/download` gibt es nur ab Techniker.

**AUDIT-029:**
- Migration 0037, additiv: `users.token_version` (int, Default 0).
- JWT-Claim `tv`; bei Abweichung 401.
- Erhöht wird bei Passwortänderung (Admin-PATCH), 2FA-Reset und Deaktivierung.
- Alte Tokens ohne `tv` gelten als Version 0.

**AUDIT-034:** Der Hub-Token-Vergleich läuft auf Bytes; ungültige Header ergeben 401.

**AUDIT-024:**
- Für unbekannte Konten läuft ein Dummy-bcrypt, damit das Timing gleich ist.
- Die Kontosperre bleibt bewusst: Die Kopplung an die IP würde Botnetz-Brute-Force erleichtern. Die Sperre ist zeitlich begrenzt und der Admin kann entsperren.
- Status: teilweise behoben.

## AP3 – SSRF und Web-Härtung

**AUDIT-004 und AUDIT-012:**
- Gemeinsamer URL-Validator in `app/net_guard.py`:
  - DNS-Auflösung;
  - alle Adressen müssen `is_global` sein (schließt 100.64/10, privat und Docker-Netz aus);
  - Verbindung auf die geprüfte IP gepinnt (httpx-Transport mit fester Adresse, SNI/Host bleibt);
  - keine automatischen Redirects: max. 3, jeder Hop wird geprüft.
- Genutzt von Threat-Feeds (beim Anlegen/Ändern und bei jedem Abruf) und Webhooks.
- Tests werden normal.

**AUDIT-010:** Sicherheits-Header in `frontend/nginx.conf`:
- CSP mit `frame-ancestors 'self'`;
- `X-Frame-Options SAMEORIGIN`;
- `nosniff`;
- `Referrer-Policy`;
- `Permissions-Policy`;
- HSTS nur über `X-Forwarded-Proto=https`, im Caddy-/nginx-Modus zusätzlich am Host-Proxy.

**AUDIT-011:**
- WebSocket per Einmal-Ticket: `POST /auth/ws-ticket` liefert ein Ticket mit 30 s Gültigkeit, einmalig verwendbar (Redis/Memory).
- Die WS-URL trägt nur das Ticket.
- Rechte werden alle 60 s nachgeprüft: Benutzer aktiv, `token_version`, Tenant aktiv; sonst Close 4401.
- Das alte `token=` bleibt als Übergang erhalten. Status: teilweise, der Parameter entfällt in AP7.

**AUDIT-052:**
- Das Frontend nutzt für die WebSocket-Verbindung das Ticket.
- JWT bleibt im `localStorage`, die Umstellung auf Cookie wäre ein größerer Umbau. Das Risiko wird über die CSP (010) gesenkt.
- Status: teilweise.

**AUDIT-025:** `/api/v1/meta` liefert ohne Anmeldung nur `version` und `product_*`. Die übrigen Felder gibt es unter `/api/v1/meta/full`, nur angemeldet; das Frontend wird angepasst.

**AUDIT-030:** Backend-Image mit `USER app`. Entrypoint und Routen:
- Die Route ins Managementnetz (`ip route replace`) braucht `NET_ADMIN`.
- Der Entrypoint setzt sie als root und wechselt dann per `setpriv` auf `app`.
- Die Volumes des Workers (Backups) bekommen die passenden Rechte.

**AUDIT-037 und AUDIT-040:**
- fastapi-Update, damit starlette ≥ 0.49.1 kommt.
- pyzipper ≥ 0.4.0.
- Suite wird gegen die neuen Versionen geprüft.

## AP4 – Robustheit von Poll und Jobs

**Sperre je Gerät** (`app/locks.py`, für AUDIT-014 und AUDIT-016):
- Mit Redis: `SET NX EX` mit Eigentümer-Token und Freigabe per Lua-Compare-Delete. Ohne Redis: In-Memory-Sperre.
- Reentrant im selben asyncio-Kontext (ContextVar), damit verschachtelte Aufrufe nicht blockieren.
- Genutzt von:
  - Policy-Deploy: wartet bis 60 s, sonst Geräte-Ergebnis „gesperrt – anderer Vorgang läuft“;
  - Post-Poll-Hooks: kein Warten, Gerät wird im Zyklus übersprungen;
  - Firmware- und Script-Tick: Überspringen, nächster Tick;
  - Offboarding: 409.
- Hängende Deploys: Beim Start von API und Worker werden `queued`/`running`-Deployments als `aborted` markiert („abgebrochen – Neustart während des Vorgangs“). Die Background-Tasks laufen im API-Prozess und gehen beim Neustart verloren.

**AUDIT-013:** Ein Geräte- oder Hook-Fehler wird je Gerät isoliert (`except Exception` mit Log, `gather(return_exceptions=True)`), Commit und Post-Hooks laufen für alle anderen.

**AUDIT-015:** `facts` wird im Poll nur feldweise zusammengeführt (frisch gelesenes `facts` + Poll-Felder unter Zeilensperre bzw. Merge), statt aus einer alten Kopie zu überschreiben.

**AUDIT-017:** `TimeoutExpired`/`SubprocessError` werden zu `PlatformBackupError`. Beim Start wird ein älterer `running`-Datensatz auf `failed` gesetzt und der Alarm ausgelöst. `_sha` läuft im Thread.

**AUDIT-018:**
- Script-Läufe und Firmware-Jobs: der Status „in Ausführung“ wird committed, bevor der Router angesprochen wird.
- Ein Fehler je Job wird isoliert; ein Job im Status „in Ausführung“ nach einem Neustart wird nicht erneut ausgelöst, sondern als „unbekannt – bitte prüfen“ markiert.

**AUDIT-033:**
- Alarm-Versand erst nach Commit.
- SLA-Monat und WAN-Volumen-Monat in der Zeitzone des Mandanten.
- Monatsberichte und nächtliches Backup isolieren Fehler je Mandant bzw. Gerät.

**AUDIT-043:** Leader-Sperre für den Worker (Redis mit Verlängerung, sonst durchgehend). Ein zweiter Worker wartet als Standby.

## Nicht in diesem Durchgang

AP5–AP7 folgen später, darunter 019 (Rest: 0.0.0.0/0), 020, 021, 022, 031, 032, 035, 036, 038, 039, 041, 042, 044, 045, 046, 047, 048, 050, 054 und 055.

## Stand (umgesetzt)

| Paket | Commit | Funde |
|---|---|---|
| AP1 | `ca57683` | 001, 002, 005, 007, 026, 027, 028, 051 behoben; 053 teilweise |
| AP2 | `41ccab3` | 003, 006, 009, 023, 029, 034 behoben; 019 (Client-IP) und 024 (Timing) teilweise |
| AP3 | `c4e34ce` | 004, 010, 012, 025, 030, 037, 040 behoben; 011 und 052 teilweise |
| AP4 | `61b998f` | 008, 013, 014, 015, 016, 017, 018, 033, 043 behoben |

**Abweichungen vom Plan:**
- **AUDIT-008 (ReDoS)** war im Bericht keinem Arbeitspaket zugeordnet und wurde mit AP4 erledigt: Ablehnung wiederholter
  Alternativen, dazu Suche mit dem Modul `regex` und hartem Zeitlimit statt Unterprozess.
- **AUDIT-030:** kein `USER` im Dockerfile, weil die Route ins Management-Netz beim Start root/NET_ADMIN braucht.
  Stattdessen gibt der Entrypoint die Rechte per `setpriv` ab. Der Worker läuft bewusst als root (`RUN_AS_ROOT`),
  weil er die root-eigene `.env` sichert.
- **AUDIT-016:** Deploys laufen weiter als Background-Task im API-Prozess. Die Verlagerung in den Worker entfällt, weil
  Gerätesperre und Erkennung hängender Deploys die Folgen abdecken.
- **AUDIT-024:** Die Kontosperre bleibt ohne IP-Kopplung (Begründung im Bericht).
- **AUDIT-011:** `?token=` bleibt als Übergang erhalten und entfällt in AP7.
- **Zusätzlich:** Die starlette-Konstante `HTTP_422_UNPROCESSABLE_ENTITY` wurde durch `…_CONTENT` ersetzt (Deprecation mit starlette 1.x).

**Keine Router-Änderung ohne Benutzeraktion:** Neue bzw. geänderte Router-Befehle gelten nur für Onboarding und
ZTP-Bootstrap, „Rechte einschränken/abgleichen“ und „WAN anwenden“.

**Migration:** nur `0037` (`users.token_version`, additiv).

**Offen für AP5–AP7:** siehe Behebungsstand in `docs/AUDIT-2026-09.md`. Ihre xfail-Tests bleiben aktiv.

**Neue LABORTEST-Punkte:** 23 (Zertifikatsspeicher, ftp), 24 (Proxy, Geheimnisse), 25 (Header, WS, SSRF, Container),
26 (Sperren, Neustarts).
