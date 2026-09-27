# Labortest mit echter Hardware

Checkliste für den ersten Test mit einem **MikroTik L009UiGS-RM** (RouterOS 7) gegen die Plattform mit
`ROUTEROS_BACKEND=api`. Jeder Schritt hat ein erwartetes Ergebnis. Abweichungen bitte mit Uhrzeit notieren
und den JSON-Export des Selbsttests beilegen.

**Laboraufbau (Annahme, wie im VRRP-Szenario aus Phase 11):**

| Port | Verwendung | Beispiel |
|------|------------|----------|
| ether1 | Erstzugang/Onboarding (Werkszustand: DHCP-Client) | Internet über Labor-LAN |
| ether2 | Kassen-VLAN/-LAN zur FortiGate, VRRP und WAN1 | lokal `192.168.110.21/24`, VIP `192.168.110.1`, FortiGate `192.168.110.2` |
| ether8 | 5G-Modem (WAN2, Backup) | Gateway `192.168.8.1` |

Der UniFi-Switch verbindet ether2, die FortiGate und einen Test-Client im Kassen-Netz.

---

## 0. Voraussetzungen Server

- [ ] `.env`: `ROUTEROS_BACKEND=api`, `PUBLIC_URL=https://…`, `WG_HUB_ENDPOINT=…` gesetzt; Stack neu gestartet.
      **Erwartet:** `GET /api/v1/meta` zeigt `"simulator": false`, die Oberfläche hat keine Simulator-Knöpfe.
- [ ] UDP `51820` am Server/an der Firewall erreichbar, HTTPS-Proxy aktiv.
- [ ] Remote-Access-Ports (`REMOTE_PROXY_PORT_RANGE`, Standard `40000-40019`) aus dem Labornetz erreichbar.
- [ ] Mandant, Standort und eine Alarmregel „Gerät offline“ (Dauer 120 s, Empfänger = eigene Adresse) angelegt.
      **Erwartet:** Testmail über die Mail-Vorschau kommt an.

## 1. Router-Grundkonfiguration (vor dem Onboarding)

Ausgangspunkt: L009 im Werkszustand (defconf), Zugang per WinBox über ether2–ether8 bzw. die Bridge.
Die Tunnel-Adresse des Hubs ist die erste Adresse aus `WG_NETWORK` (Standard `10.100.0.0/16` → `10.100.0.1`).

- [ ] RouterOS und RouterBOARD-Firmware notieren (`/system resource print`, `/system routerboard print`).
      Für den Firmware-Test (Schritt 7) **nicht** vorab aktualisieren.
- [ ] Uhrzeit: `/system ntp client set enabled=yes servers=pool.ntp.org` und Zeitzone setzen.
      **Erwartet:** `/system clock print` zeigt die richtige Uhrzeit (± wenige Sekunden).
- [ ] **API-Gruppe:** Das Onboarding legt die Gruppe `sdwan-api` selbst an und setzt den API-Benutzer
      `sdwan` hinein, nie in `full`. Existiert die Gruppe schon, werden nur ihre Policies gesetzt. Zur
      Referenz die Policies (einzige Definition: `backend/app/routeros/schema.py`, `API_POLICIES`):

  ```
  read,write,api,policy,reboot,test,ssh,sensitive,winbox,web
  ```

  - `api, read, write`: alle Funktionen.
  - `policy`: Gruppe und temporäre Benutzer für den Fernzugriff.
  - `reboot`: Neustart und Firmware.
  - `test`: Ping.
  - `ssh`: Backup-Export.
  - `sensitive`: Schlüssel und Passwörter im Export (ohne sie ist das Backup unvollständig).
  - `winbox, web`: nötig, damit die Gruppe `sdwan-remote` für den Fernzugriff angelegt werden kann.
    Ohne diese Policies funktioniert der Fernzugriff nicht.
- [ ] Dienste auf das Nötige beschränken: `api` und `ssh` nur aus dem Tunnelnetz (Hub-Adresse), alles
      Unverschlüsselte aus:

  ```
  /ip service set api disabled=no address=10.100.0.1/32
  /ip service set ssh disabled=no address=10.100.0.1/32
  /ip service set www disabled=yes
  /ip service set telnet disabled=yes
  /ip service set ftp disabled=yes
  /ip service set winbox address=192.168.88.0/24
  ```

  Den WinBox-Zugang für die lokale Administration auf das eigene Verwaltungsnetz legen, im Beispiel das
  defconf-LAN `192.168.88.0/24`. Nach dieser Änderung geht SSH nur noch über den Tunnel.
## 2. Onboarding

- [ ] In der Oberfläche: Geräte → „Gerät hinzufügen“, Name z. B. `lab-l009`, Standort wählen.
- [ ] Den angezeigten Befehl im RouterOS-Terminal ausführen (`/tool fetch … ; /import …`).
      **Erwartet:** Ausgabe „SD-WAN: Onboarding abgeschlossen.“ und „Tunnel-IP 10.100.x.y“.
- [ ] **Erwartet in der Oberfläche (≤ 60 s):** Status „Online“. Modell `L009UiGS`, RouterOS-Version,
      Seriennummer und Architektur `arm` sind gefüllt. Uptime läuft, CPU-/Speicher-Kacheln haben Werte.
- [ ] Übersicht: Die Kacheln **Temperatur/Spannung** erscheinen nur, wenn der L009 Sensorwerte liefert.
      Werte notieren. Fehlen sie, ist das kein Fehler; `/system health print` zum Vergleich ausführen.
- [ ] Übersicht → „IP-Adressen“: Die DHCP-Adresse auf ether1 trägt die Kennzeichnung **DHCP**, die
      Bridge-Adresse `192.168.88.1/24` ist **statisch**, die Tunnel-Adresse ist als **Plattform** markiert.
- [ ] Auf dem Router: `/user print` und `/user group print where name=sdwan-api`.
      **Erwartet:** `sdwan` ist in der Gruppe `sdwan-api` mit genau den Policies aus Schritt 1.

## 3. Selbsttest

- [ ] Übersicht → Karte „Selbsttest“ → „Selbsttest ausführen“.
      **Erwartet:** Dauer unter 30 s. Alle Pfade sind erreichbar, und keine Zeile ist rot.
- [ ] Orange Zeilen einzeln aufklappen und bewerten. Mögliche Ursachen:
  - **Leere Pflicht-Tabelle:** Vor der WAN-Einrichtung (Schritt 4) kann `/tool netwatch` leer sein. Das
    ist in Ordnung.
  - **Fehlende Felder** wie `rtt-avg`/`loss-percent` bei Netwatch oder `master`/`backup` bei VRRP:
    Feldnamen notieren. Die Rolle wird dann über `running` abgeleitet.
  - **Rechte:** `sensitive`, `winbox` oder `web` fehlt (mit Begründung in der Zeile).
- [ ] Zeile „Paket-Update“: `latest-version` fehlt vor der ersten Update-Prüfung. Das ist nur ein Hinweis.
- [ ] Zeile „Uhrzeitabweichung“: unter 60 s.
- [ ] Zeilen „Dienst api“ und „Dienst ssh“: aktiv und für `10.100.0.1` erlaubt.
      Zur Gegenprobe `/ip service set ssh address=192.168.88.0/24` setzen und den Selbsttest wiederholen.
      **Erwartet:** SSH-Zeile rot mit „Backup-Export schlägt fehl“. Danach zurückstellen.
- [ ] Zeile „Rechte API-Benutzer“: grün, Gruppe `sdwan-api`.
- [ ] **„Rechte einschränken“ mit Totmannschaltung** (gilt für Geräte, die vor dieser Version verbunden
      wurden; im Labor nachstellen):
  1. `/user set [find name=sdwan] group=full` setzen und den Selbsttest ausführen.
     **Erwartet:** Zeile orange „API-Benutzer in Gruppe full – Umstellung empfohlen“ mit dem Button
     „Rechte einschränken“.
  2. Button ausführen und währenddessen `/system scheduler print detail` beobachten.
     **Erwartet:** Kurz erscheint `sdwan-revert-api-group` mit `start-date`/`start-time` = Router-Uhr
     + 3 Minuten, im Datumsformat des Routers, und `interval=1m`. Nach Erfolg zeigt die Oberfläche
     „Rechte eingeschränkt“, der Scheduler ist gelöscht und die Gruppe ist `sdwan-api`.
  3. **Scheduler läuft nach ~3 Minuten:** Ein von der Plattform angelegter Scheduler startet zum festen
     Zeitpunkt. Prüfen, dass `next-run` genau `start-date start-time` entspricht (Router-Uhr + 3 min,
     nicht Uhr + 4 min o. Ä.) und die Router-Zeitzone stimmt (`/system clock print`, siehe Schritt 1).
     Beobachtet: next-run = ______________________
  4. Fehlerfall: erneut `group=full` setzen, dann `/ip service set ssh address=192.168.88.0/24`, damit der
     Selbsttest nach der Umstellung rot wird. Danach den Button ausführen.
     **Erwartet:** orange „Rechte werden in ca. 3 Minuten automatisch zurückgestellt“ mit der Startzeit
     (Router-Zeit), der Scheduler bleibt stehen. Zum angezeigten Zeitpunkt ist `sdwan` wieder in `full`,
     und der Scheduler ist verschwunden. Im Log steht nur ein Lauf; ein zweiter Lauf eine Minute später
     wäre das Sicherheitsnetz und deutet darauf hin, dass das Zurückstellen im ersten Lauf fehlschlug. `ssh`
     danach zurückstellen und den Selbsttest erneut ausführen.
- [ ] „JSON“ exportieren und ablegen, als Referenz für RouterOS-Version und Modell.
- [ ] **Nach Schritt 4 und 5 den Selbsttest wiederholen.** Erst dann sind Netwatch, Routen und VRRP
      befüllt. **Erwartet:** VRRP und Netwatch grün, oder orange mit einem notierten Feldnamen.

## 4. WAN-Failover (FortiGate + 5G)

Einrichtung: Tab „WAN“, Modus Failover. WAN1 = ether2, Gateway `192.168.110.2`, also die echte
FortiGate-IP und **nicht** die VIP; Prüfziel `1.1.1.1`. WAN2 = ether8, Gateway `192.168.8.1`, Prüfziel
`9.9.9.9`. Speichern & anwenden.

- [ ] **Erwartet:** Push ok. Auf dem Router gibt es Routen und Netwatch mit Kommentar `sdwan:wan:…`.
      Kachel „Aktiver WAN“ = WAN1. Im Tab „WAN“ zeigen beide Leitungen „up“ und Latenz.
- [ ] Test-Client: Dauer-Ping `8.8.8.8` und eine offene TCP-Verbindung (z. B. SSH oder Stream) starten.
- [ ] **WAN1 unterbrechen:** Kabel FortiGate → Internet ziehen oder die FortiGate-WAN-Route entfernen.
      **Erwartet nach ca. 10–20 s:** Netwatch WAN1 „down“, Default-Route WAN1 deaktiviert, Verkehr über
      5G. Im Kopf erscheint „Standort läuft über …“ (orange), und es gibt einen Alarm „Backup-WAN aktiv“
      bzw. „WAN down“. Die Ping-Lücke beträgt ≤ ca. 20 s. Bestehende Verbindungen über WAN1 werden
      geleert, neue gehen über 5G.
- [ ] **Wiederherstellen:** **Erwartet:** Netwatch „up“, nach der Recovery-Verzögerung (Standard 30 s)
      zurück auf WAN1, Alarm behoben. Im Tab „WAN“ und in der Ereignisliste stehen beide Wechsel mit
      Zeitstempel.
- [ ] Metriken → Durchsatz: Die Backup-Phase ist als schattierter Bereich markiert.

## 5. VRRP gegen FortiGate

Einrichtung: Tab „VRRP“ → „VRRP einrichten“. Name `vrrp-kassen`, Interface ether2, VRID wie auf der
FortiGate, Priorität 100 (kleiner als die FortiGate, z. B. 200), VIP `192.168.110.1`, lokale Adresse
`192.168.110.21/24`, gekoppeltes WAN = WAN1, **Gegenstelle** `192.168.110.2`, Beschreibung
„FortiGate Labor (port5)“.

> **UniFi-Switch – IGMP-Snooping:** VRRP-Advertisements gehen an die Multicast-Adresse `224.0.0.18`
> (IP-Protokoll 112). Filtert der Switch diese Pakete, etwa durch IGMP-Snooping oder eine
> Multicast-Filterung im Kassen-Netz, sieht keiner die Hellos des anderen. **Symptom: FortiGate und
> MikroTik sind beide Master**, die VIP ist doppelt vergeben, und es gibt ARP-Flattern beim Client.
> Abhilfe: IGMP-Snooping für dieses Netz/VLAN abschalten oder sicherstellen, dass `224.0.0.x`
> (Link-Local-Multicast) geflutet wird. Außerdem müssen **VRRP-Version** (MikroTik hier v3) und **VRID**
> auf beiden Seiten gleich sein. Ein Versionsunterschied führt zum selben Symptom.

- [ ] **Erwartet nach dem Speichern:** Push ok, Rolle **Backup** („Hauptsystem aktiv“).
      Karte „Gegenstelle“: **Erreichbar** mit RTT. „Peer prüfen“ aktualisiert „geprüft vor …“.
      Auf der FortiGate: `get router info vrrp` zeigt sie als Master.
- [ ] Gegenprobe Split-Brain: Beide Seiten gleichzeitig Master? Dann IGMP-Snooping prüfen (siehe Hinweis).
- [ ] **Glasfaser an der FortiGate ziehen (WAN der FortiGate):**
      **Erwartet:** Die FortiGate sendet weiter Advertisements, die MikroTik-Rolle bleibt **Backup**. WAN1
      der MikroTik geht „down“ (Prüfziel über die FortiGate nicht erreichbar), der MikroTik selbst
      schaltet auf 5G um (wie Schritt 4). Clients mit der VIP als Gateway hängen weiter an der FortiGate:
      Ob die FortiGate die Master-Rolle abgibt, hängt von deren VRRP-Überwachung ab (`vrdst`). Verhalten
      notieren.
- [ ] Glasfaser wieder stecken. **Erwartet:** WAN1 „up“, Rückschwenk nach 30 s.
- [ ] **FortiGate-Port zum Kassen-Netz abschalten** (`port5` administrativ down):
      **Erwartet nach ca. 3–4 s:** Rolle **Master** (orange Karte). Die VIP liegt auf dem MikroTik. Die
      Default-Route von WAN1 ist sofort deaktiviert, Verkehr läuft über 5G. Die Gegenstelle ist spätestens
      nach der nächsten Abfrage **nicht erreichbar**. Es gibt einen Alarm „VRRP-Master“ per Mail. Der
      Test-Client pingt mit kurzer Lücke weiter über die VIP.
- [ ] Während Master: Poll-Dauer beobachten (Gerät bleibt „Online“, Werte aktualisieren sich weiter).
      Der Ping zur nicht erreichbaren Gegenstelle darf die Abfrage nicht blockieren.
- [ ] **Port wieder einschalten (Zurückschalten):** **Erwartet:** Mit Preemption übernimmt die FortiGate
      in wenigen Sekunden wieder. Die MikroTik-Rolle ist **Backup**, die Gegenstelle **erreichbar**.
      WAN1-Routen werden erst nach der Recovery-Verzögerung wieder aktiv, wenn die Netwatch „up“ meldet.
      Der Alarm ist behoben. Der Rollenverlauf zeigt die Master-Phase mit Dauer.
- [ ] Selbsttest wiederholen (siehe Schritt 3).

## 6. Backup

- [ ] Kopf → „Backup jetzt“. **Erwartet:** Im Tab „Backups“ steht ein neuer Eintrag mit Auslöser
      **Manuell**, „Erstellt von“ = eigene E-Mail und gekürzter Prüfsumme. Der Tooltip zeigt den vollen
      SHA-256, ein Klick kopiert ihn.
- [ ] Inhalt prüfen („Vollständiger Stand B“): Die Konfiguration ist vollständig. Mit `sensitive` sind
      WireGuard-Private-Keys enthalten, ohne `sensitive` fehlen sie (vgl. Selbsttest).
- [ ] Kleine Änderung auf dem Router (z. B. Kommentar an ether3), dann erneut „Backup jetzt“.
      **Erwartet:** Der Vergleich A ↔ B zeigt genau diese Zeile.
- [ ] Policy-Push (Firewall-Policies → Deploy auf `lab-l009`). **Erwartet:** Der Deploy ist erfolgreich,
      im Backups-Tab gibt es einen Eintrag **Nach Policy-Push**, erstellt vom Deploy-Starter.
- [ ] Download als `.rsc` funktioniert. Im Audit-Log stehen `backup.create` und `backup.download`.

## 7. Firmware-Update

- [ ] Firmware → „Nach Updates suchen“. **Erwartet:** Der L009 zeigt die installierte und die verfügbare
      Version. Danach ist im Selbsttest `latest-version` befüllt.
- [ ] Update-Job nur für `lab-l009` (Batch 1). **Erwartet:** Zuerst entsteht ein Backup **Vor
      Firmware-Update** mit Job-Ersteller. Dann folgen Installation und Reboot. Die neue Version wird
      verifiziert, anschließend das RouterBOARD-Upgrade mit zweitem Reboot. Der Job endet „abgeschlossen“.
- [ ] **Offline-Alarm während des Updates unterdrückt:** Ab der Installation zeigen Kopf und Geräteliste
      „Neustart läuft (Firmware-Update)“. Der Offline-Alarm ist 10 Minuten unterdrückt; vor dem
      RouterBOARD-Neustart beginnen die 10 Minuten neu. **Erwartet:** keine Offline-Mail während des
      Updates. Nach dem Job wird ein Ausfall wieder normal alarmiert. Gesamtdauer beider Neustarts notieren:
      ______
- [ ] Nach dem Update den Selbsttest wiederholen und die neue Version im JSON-Export vergleichen.

## 8. Fernzugriff

- [ ] Kopf → „Fernzugriff starten“ → WinBox, 15 min, erlaubte Quelle = eigene IP.
      **Erwartet:** Verbindungsdaten mit Port aus dem Proxy-Bereich und temporärem Benutzer
      `sdwan-rs-…`. WinBox verbindet über `<server>:<port>`.
- [ ] Von einer anderen Quell-IP verbinden. **Erwartet:** Die Verbindung wird abgelehnt.
- [ ] SSH-Sitzung genauso testen.
- [ ] Sitzung beenden oder ablaufen lassen. **Erwartet:** Der temporäre Benutzer ist auf dem Router
      entfernt (`/user print`), der Port ist geschlossen, das Audit-Log enthält Start und Ende.
- [ ] Gruppe prüfen: `/user group print where name=sdwan-remote`, `/user print where name~"sdwan-rs-"`.
      **Erwartet:** Gruppe `sdwan-remote` mit genau `ssh,read,write,test,winbox,web,reboot,sensitive`
      (ohne `policy`, `api` und `local`). Der temporäre Benutzer ist in dieser Gruppe, nicht in `full`.
- [ ] **Temporärer Benutzer kann sich per WinBox/SSH anmelden, aber keine Benutzer anlegen:** In der
      WinBox- bzw. SSH-Sitzung `/user add name=test group=read password=x` ausführen.
      **Erwartet:** RouterOS lehnt ab (fehlende Policy `policy`). Anmeldung, Anzeige und Konfiguration
      funktionieren.
- [ ] **Annahme verifizieren: Gruppe mit mehr Rechten als der eigene Benutzer.** Die Plattform und der
      Simulator gehen davon aus, dass RouterOS das Anlegen einer Gruppe mit Policies ablehnt, die der
      anlegende Benutzer selbst nicht hat (Simulator: `SIMULATOR_ENFORCE_GROUP_RIGHTS`). Test:
      1. `/user group remove [find name=sdwan-remote]` ausführen.
      2. In `sdwan-api` vorübergehend `winbox` entfernen: `/user group set sdwan-api policy=read,write,api,policy,reboot,test,ssh,sensitive,web`.
      3. Fernzugriff starten.
      **Erwartet laut Annahme:** klare Fehlermeldung „Gruppe sdwan-remote konnte nicht angelegt werden …“,
      kein temporärer Benutzer, kein Ausweichen auf `full`.
      Tatsächliches Verhalten von RouterOS: ______________________
      Danach `winbox` wieder ergänzen (oder den Selbsttest-Hinweis beachten).
- [ ] **Dienstzustand wird wiederhergestellt (WebFig):** Vorher `/ip service print` notieren
      (`www` laut Schritt 1 deaktiviert). WebFig-Sitzung starten.
      **Erwartet:** `www` ist aktiv, die Hub-Adresse ist ergänzt. Sitzung beenden.
      **Erwartet:** `www` ist wieder exakt wie vorher (deaktiviert, gleiche `address`).
- [ ] Zwei WebFig-Sitzungen parallel starten und die erste beenden. **Erwartet:** `www` bleibt aktiv.
      Nach dem Ende der zweiten ist `www` wieder deaktiviert.
- [ ] Eine WebFig-Sitzung mit kurzer Dauer (z. B. 5 min) ablaufen lassen, dabei den Plattform-Container
      neu starten (`docker compose restart api worker`). **Erwartet:** Nach dem Ablauf stellt der Worker
      `www` zurück (spätestens 30 s nach Ablauf).
- [ ] Gegenprobe: `www` vorher aktivieren (`disabled=no`), Sitzung starten und beenden.
      **Erwartet:** `www` bleibt aktiv; die Plattform stellt nur zurück, was sie selbst geändert hat.
      Danach `www` wieder deaktivieren.
- [ ] Gleiches Verhalten für WinBox/SSH, falls diese vorher deaktiviert bzw. eingeschränkt waren.

## 9. Neustart

- [ ] Kopf → „Neustart“. **Erwartet:** Der Dialog verlangt den exakten Gerätenamen und ist ohne ihn
      nicht bestätigbar.
- [ ] Bestätigen. **Erwartet:** Der Router startet neu (1–2 min). Kopf und Geräteliste zeigen
      „Neustart läuft“, auch wenn das Gerät zwischenzeitlich „Offline“ wäre. Es kommt **kein** Offline-Alarm
      und **keine** Mail. Nach der Rückkehr steht der Status auf „Online“ mit kleiner Uptime, und
      „Neustart läuft“ verschwindet. Im Audit-Log steht `device.reboot` mit Benutzer.
- [ ] Gegenprobe: Neustart auslösen und dem Router danach den Strom nehmen (> 5 min).
      **Erwartet:** Nach Ablauf der 5 Minuten verschwindet „Neustart läuft“, der normale Offline-Alarm
      löst aus und die Mail kommt.
- [ ] Strom wieder an. **Erwartet:** Gerät online, Alarm behoben.

## 10. Firewall-Editor (Phase 14)

Vorbereitung: WAN eingerichtet (Schritt 4). Auf dem L009 existieren die defconf-Firewallregeln (nicht verwaltet).

- [ ] Gerät → Firewall → „Firewall-Zonen“: `bridge` → LAN, ein freier Port (z. B. ether3) → Management,
      ggf. Gäste-VLAN → Gäste. „Speichern & anwenden“.
      **Erwartet:** `/interface list print` zeigt `sdwan-zone-lan`, `sdwan-zone-management` (Kommentar
      `sdwan:zone:…`), `/interface list member print` die Zuordnungen. defconf-Listen `LAN`/`WAN` sind
      unverändert.
- [ ] Firewall-Policies → „Policy“ → Modus „Einfach“. Bausteine „Standard-Härtung“ und „Gäste vom LAN
      isolieren“ einfügen, speichern, dem L009 zuweisen.
- [ ] Vorschau öffnen. **Erwartet:** RouterOS-Befehle und Diff („leer“ beim ersten Mal). Die Prüfung meldet
      keine Fehler, nur ggf. Warnungen zu leeren Zonen.
- [ ] „Ausrollen“. **Erwartet:** Der Dialog listet die defconf-Regeln als „würden nie mehr greifen“; ohne
      Häkchen wird das Gerät übersprungen und `/ip firewall filter print` ist unverändert.
- [ ] Erneut ausrollen, diesmal mit Häkchen für das Gerät. **Erwartet:**
  - Die verwalteten Regeln stehen **oben**, die erste ist `base:platform-hub`.
  - Die Plattform bleibt erreichbar (Status online).
  - WinBox aus der Management-Zone geht, aus dem LAN nicht.
  - Clients im LAN haben Internet sowie DHCP und DNS.
  - Aus dem Gästenetz ist das LAN nicht erreichbar.
- [ ] VPN-Mesh und VRRP (falls eingerichtet) funktionieren weiter. **Erwartet:** Mesh-Tunnel up,
      VRRP-Rolle unverändert.
- [ ] **Annahmen prüfen:**
  - `protocol=vrrp` wird von RouterOS akzeptiert. Ergebnis: ______
  - Address-List-Eintrag im Format `a.b.c.d-e.f.g.h` (Objekt „Bereich“) wird akzeptiert. Ergebnis: ______
  - `/ip firewall filter reset-counters` über die API (Button „Zähler zurücksetzen“) setzt nur die
    verwalteten Regeln zurück. Ergebnis: ______
- [ ] Trefferzähler: nach ≥ 5 min Verkehr zeigt die Regeltabelle Pakete je Regel. Regeln ohne Treffer seit
      7 Tagen sind markiert.
- [ ] Management-Zone von ether3 entfernen und erneut ausrollen. **Erwartet:** Lint-Fehler „Lokaler Zugriff
      (WinBox/SSH im LAN) nach dem Deploy nicht mehr möglich – nur noch über den Tunnel“. Ausrollen geht nur
      mit Bestätigung. Danach Zuordnung wiederherstellen.
- [ ] Selbsttest: Zeilen „Interface-Listen“ und „Interface-Listen-Mitglieder“ grün; bei „Firewall-Filter“
      ist `packets` vorhanden.

**Router im Werkszustand (defconf)** – Nachtrag Phase 14
- [ ] Testgerät per `/system reset-configuration` in den Werkszustand setzen, neu onboarden. Im Firewall-Tab
      erscheint „Vorschlag aus der Werkskonfiguration“: LAN → `bridge`, WAN → `ether1` (nur Kontrolle).
      „Vorschlag übernehmen“ und „Speichern & anwenden“.
- [ ] Einfache Policy mit Default-Drop zuweisen und ausrollen. **Erwartet:** Der Dialog zeigt die Gruppe
      „Werks-Firewall (defconf)“, die Option „defconf-Regeln deaktivieren“ ist an, das Gerät ist „bereit“.
      Danach `/ip firewall filter print where comment~"defconf"`: alle Regeln `X` (disabled), keine gelöscht.
      Internet aus dem LAN, WinBox aus der Management-Zone und über den Tunnel funktionieren.
- [ ] Eine eigene Regel ohne defconf-Kommentar anlegen und erneut ausrollen. **Erwartet:** Gerät wird
      übersprungen (nur diese eine Regel wird gelistet).
- [ ] Zuweisung der Policy entfernen. **Erwartet:** Die defconf-Regeln sind wieder aktiv; eine vorher vom
      Kunden selbst deaktivierte defconf-Regel bleibt aus. Alternativ: Button „defconf-Regeln wieder aktivieren“.
- [ ] **Annahme prüfen:** `.id` der defconf-Regeln bleibt nach einem Neustart gleich (Wiederherstellen nach Reboot).
      Ergebnis: ______
- [ ] Nach dem Deploy Export + `/import` (neue `.id`s), dann „defconf-Regeln wieder aktivieren“. **Erwartet:**
      Alle Regeln werden per Fingerabdruck gefunden und aktiviert. Eine defconf-Regel doppelt anlegen (deaktiviert)
      → diese Regel wird als „nicht eindeutig zuordenbar“ angezeigt und nicht angefasst.

## 11. Threat-Feeds (Phase 15)

- [ ] Threat-Feeds → „Spamhaus DROP (IPv4)“ → „Zuweisen“ an den L009 → „Jetzt laden“.
      **Erwartet:** Status „aktuell“, mehrere hundert Einträge, am Gerät „aktuell“.
      `/ip firewall address-list print count-only where list=sdwan-feed-spamhaus-drop4` entspricht der Zahl.
      Ergebnis/URL erreichbar: ______
- [ ] **Annahme prüfen:** Die Spamhaus-Datei `drop_v4.json` hat eine JSON-Zeile je Netz mit Feld `cidr`
      (sonst Feed-Format anpassen). Ergebnis: ______
- [ ] Freien Speicher notieren (`/system resource print`) vor und nach dem Laden: ______ / ______ MB.
      Schätzwert 200 Byte je Eintrag plausibel? ______
- [ ] IPv6-Feed zuweisen und laden. **Erwartet:** Einträge in `/ipv6 firewall address-list`
      (Annahme: gleiche Felder). Ergebnis: ______
- [ ] „Jetzt laden“ ein zweites Mal. **Erwartet:** keine Änderungen am Router (keine neuen `.id`s, nur
      Differenzen).
- [ ] Im Firewall-Editor den Baustein „Threat-Feeds eingehend verwerfen“ mit dem Feed-Objekt einfügen und
      ausrollen. **Erwartet:** Regeln mit `src-address-list=sdwan-feed-spamhaus-drop4` (Router und forward)
      sowie `dst-address-list=` ausgehend.
- [ ] Zuweisung entfernen. **Erwartet:** Die Liste ist auf dem Router weg; manuelle Address-Lists sind
      unverändert.

## 12. Compliance und Config-Suche (Phase 16)

- [ ] Compliance → Regelsets → „MSP-Baseline“ → „Zuweisen“ an den L009 → Flottenbericht → „Jetzt prüfen“.
      **Erwartet:** Zeile L009 mit Ergebnissen je Regel. Mit der Grundkonfiguration aus Schritt 1 sind www
      deaktiviert, NTP aktiv und API/SSH nur aus dem Tunnel „ok“.
- [ ] **Annahme prüfen:** `/system ntp client print` liefert `enabled=yes`. Die Regel „NTP-Client aktiv“
      zeigt „ok“ (nicht „?“). Ergebnis: ______
- [ ] `/ip service set www disabled=no` setzen, „Backup jetzt“. **Erwartet:** Nach dem Backup steht die Regel
      „Dienst www deaktiviert“ automatisch auf „verletzt“. Danach zurückstellen.
- [ ] CSV und PDF herunterladen und öffnen. **Erwartet:** gleiche Matrix.
- [ ] Config-Suche nach dem Namen des API-Benutzers bzw. einer IP aus der WAN-Konfiguration.
      **Erwartet:** Treffer mit Kontextzeilen und Link ins Gerät.
- [ ] Config-Suche nach einem bekannten WireGuard-Private-Key bzw. einem Passwort (nur wenn `sensitive`
      exportiert wird). **Erwartet:** kein Treffer; in anderen Treffern steht `private-key=***`.

## 13. Script-Bibliothek (Phase 17)

- [ ] Scripts → „Systeminformationen“ → „Ausführen“ auf dem L009 (Vorschau prüfen: Variablen ersetzt).
      **Erwartet:** Ausgabe wie im Terminal (resource, routerboard, update). Status „Erfolgreich“.
- [ ] **Annahme prüfen:** Ein mehrzeiliges Script läuft per SSH vollständig. Ergebnis: ______
- [ ] Eigenes „nur lesend“-Script mit einem Tippfehler (z. B. `/system resurce print`).
      **Erwartet:** Status „fehlgeschlagen“, Fehlertext aus der Ausgabe (`bad command name` o. ä.).
      Tatsächlicher Fehlertext: ______
- [ ] Beispiel „DNS-Cache leeren“ (ändernd) als Admin ausführen. **Erwartet:** Namenseingabe ist Pflicht.
      Vorher entsteht ein Backup „Vor Script“, danach ist der DNS-Cache leer (`/ip dns cache print`).
- [ ] Als Techniker: Das ändernde Script ist nicht ausführbar (Button fehlt, API 403).
- [ ] „Ausgaben durchsuchen“ nach „Gerät:“. **Erwartet:** Treffer der Systeminfo-Ausführung.

## 14. Wartungsfenster, Speedtest, Syslog (Phase 18)

**Wartungsfenster**
- [ ] Wartungsfenster „jetzt + 30 min“ für das Testgerät anlegen, dann WAN-Kabel am L009 ziehen.
      **Erwartet:** Alarm bleibt „Unterdrückt – Wartungsfenster …“, keine Mail. Nach Fensterende (Kabel noch
      gezogen) wird normal alarmiert.
- [ ] Firmware-Rollout mit „Nur in Wartungsfenstern starten“ außerhalb eines Fensters anlegen.
      **Erwartet:** Gerät „wartet auf Wartungsfenster“, Start erst im Fenster.

**Speedtest** (Voraussetzung: CHR/RouterOS als btest-Server, `/tool bandwidth-server set enabled=yes`,
`SPEEDTEST_SERVER` gesetzt)
- [ ] WAN-Tab → Speedtest → „Testen“. **Erwartet:** Dialog mit geschätztem Verbrauch; Ergebnis mit Down/Up.
- [ ] **Annahme prüfen:** `/tool bandwidth-test` liefert `rx-total-average`/`tx-total-average` (bps) mit
      `direction=receive`/`transmit` und `duration=10s`. Tatsächliche Feldnamen: ______
- [ ] Während des Tests `/ip route print where comment~"sdwan:speedtest"` → genau eine /32-Route über das
      Gateway des gewählten WAN. **Nach dem Test:** Route ist entfernt.
- [ ] Bei zwei WAN: Test über das Backup-WAN läuft tatsächlich über dieses (Traffic-Zähler am Interface).
- [ ] WAN mit Monatslimit: Start verlangt die Bestätigung der Volumenwarnung.

**Syslog**
- [ ] Tab „Log“ → Einstellungen → Topics `system`, `critical` aktivieren. Auf dem Router prüfen:
      `/system logging action print` → `sdwan-syslog` mit `target=remote`, `remote=<Hub-Tunnel-IP>`,
      `remote-port=514`, `src-address=<Tunnel-IP>`; `/system logging print` → Regeln mit `action=sdwan-syslog`.
- [ ] **Annahme prüfen:** Aktionen und Regeln haben kein `comment`-Feld (Erkennung über den Namen).
      Ergebnis: ______
- [ ] Container `syslog` läuft und empfängt: WinBox-Login am Router → Meldung erscheint im Tab „Log“ mit
      Schweregrad und Topics. Format der empfangenen Rohzeile (für den Parser): ______
- [ ] VRRP-Verlauf → Symbol „Log um diesen Zeitpunkt“ → Log zeigt ±15 min um den Übergang.
- [ ] Syslog abschalten → Aktion und Regeln sind entfernt, eigene Logging-Regeln bleiben.

## 15. WLAN (Phase 19)

Voraussetzung: ein Gerät mit `wifi`-Paket (z. B. hAP ax²/ax³), optional ein Gerät mit altem `wireless`-Treiber.

- [ ] Selbsttest am wifi-Gerät: Pfade `wifi*` grün. **Tatsächliche Felder** von `/interface/wifi/radio print`
      (erwartet `interface`, `bands`): ______
- [ ] Nach ≤ 10 min erscheint der Tab „WLAN“ mit Radios und Clients. Beim Gerät ohne WLAN (z. B. RB5009) gibt es
      keinen Tab.
- [ ] Profil „Test“ (WPA2/WPA3-PSK, VLAN, 80 MHz) anlegen, lokal zuweisen, ausrollen. Prüfen:
      `/interface wifi security|datapath|channel|configuration print where name~"sdwan-wifi"` und
      `/interface wifi print where comment~"sdwan:wifi"` → virtuelle APs je Radio. **Erwartet:** Handy verbindet
      sich, landet im VLAN. **Annahme prüfen:** Feldnamen `authentication-types`, `passphrase`, `vlan-id`,
      `client-isolation`, `width` (`20/40/80mhz`), `country=Austria`, `hide-ssid`. Abweichungen: ______
- [ ] Physische Radios und das Werks-WLAN sind danach unverändert (SSID, Konfiguration).
- [ ] Ländercode im Mandanten auf `DE` ändern, Profil ausrollen → `country=Germany`.
- [ ] Zeitplan 1–2 min in der Zukunft → Scheduler `sdwan-wifi-test-on|off` schalten die virtuellen APs.
- [ ] Enterprise-Profil mit RADIUS: `/radius print` zeigt den Eintrag mit `service=wireless`; Anmeldung mit
      802.1X funktioniert. Benötigte zusätzliche Felder (z. B. `eap-methods`): ______
- [ ] Monitor: Tab zeigt den Kanal je Radio. **Annahme prüfen:** `/interface/wifi/monitor … once` trennt keine
      Clients. Ergebnis: ______
- [ ] Gerät mit `wireless`-Treiber zuweisen und ausrollen → Status „wireless-Treiber“, im Router-Log **keine**
      Änderung (`/log print where topics~"system"`).
- [ ] CAPsMAN: Controller mit `/interface wifi capsman set enabled=yes`, einen CAP manuell anmelden. Profil an
      den Controller zuweisen, ausrollen → Provisioning-Regeln `sdwan:wifi:prov:*` stehen **hinter** vorhandenen
      Regeln; nach `/interface wifi provisioning` auf dem CAP sendet dieser die SSID. **Annahme prüfen:**
      `supported-bands`-Werte. Ergebnis: ______
- [ ] Gäste-Profil: „PSK rotieren“ → neues PSK auf dem Router; Aushang drucken, QR-Code mit iOS und Android
      scannen → Verbindung ohne Eintippen.
- [ ] Zuweisung entfernen und ausrollen → alle `sdwan-wifi`-Objekte weg, Werks-WLAN läuft weiter.

## 16. Gäste-Portal / Hotspot (Phase 20)

Voraussetzung: Gäste-VLAN-Interface mit IP-Adresse und DHCP-Server am Testgerät, ein Handy/Laptop im Gästenetz.

- [ ] Selbsttest: Pfade `hotspot*` grün (Standardprofile vorhanden).
- [ ] Hotspot mit Vorlage „Gastronomie“ auf dem Gäste-VLAN anlegen und ausrollen. Prüfen: `/ip hotspot print`,
      `/ip hotspot profile print where name~"sdwan-hs"`, `/file print where name~"sdwan-hs"`.
      **Annahme prüfen:** Upload-Ziel und `html-directory` passen (Seite erscheint). Tatsächlicher Pfad: ______
- [ ] Gast öffnet eine HTTP-Seite → Portal erscheint (DE/EN umschaltbar, Logo/Farben). Ohne Checkbox kein Login.
      Mit Checkbox → online. **Annahme prüfen:** Trial-Login mit `username=T-$(mac-esc)`. Ergebnis: ______
- [ ] Sitzungsdauer und Bandbreite greifen (`/ip hotspot active print`, Speedtest am Handy).
- [ ] Walled-Garden-Host ist ohne Anmeldung erreichbar.
- [ ] Vorlage „Hotel“: Voucher-Profil (z. B. 60 min, 100 MB) und 10 Voucher erzeugen → `/ip hotspot user print`
      zeigt die Codes mit `limit-uptime`/`limit-bytes-total`. A4-Druck prüfen, QR-Code scannen → Anmeldung ohne
      Eintippen. **Annahme prüfen:** Login per `…/login?username=CODE&password=` und `http-pap` mit leerem Passwort.
      Ergebnis: ______
- [ ] Nach Nutzung: Status „aktiv“, Online-Zeit und Volumen in der Liste (≤ 5 min). Nach Ablauf „verbraucht“.
- [ ] Vorlage „Büro-Gäste“ (Formular): Formular absenden → Eintrag unter „Gäste → Registrierungen“, danach online.
      **Annahme prüfen:** Plattform per `/ip hotspot walled-garden ip` (`dst-host`) erreichbar, auch über HTTPS.
      Ergebnis: ______
- [ ] Mehr als 10 Registrierungen in 10 min vom selben Gerät → Antwort 429.
- [ ] Live-Ansicht: Gast trennen (muss sich neu anmelden), Gast sperren (Klick: MAC in `/ip hotspot ip-binding`
      mit `type=blocked`; Voucher: `disabled=yes`), wieder entsperren.
- [ ] Hotspot löschen → alle `sdwan-hs`-Objekte entfernt, Standardprofile unverändert.
- [ ] Access-Point eines anderen Herstellers am Gäste-VLAN: Portal funktioniert identisch.
- [ ] Firewall-Editor: Policy für das Gerät ohne Gäste-Isolation → Hinweis „Gäste vom LAN isolieren“.

## 17. Offboarding (zum Schluss)

Voraussetzung: das Testgerät mit möglichst vielen Funktionen (Firewall-Policy mit Default-Drop auf Werks-Router,
WAN, VRRP, Syslog, WLAN, Hotspot, offene Fernzugriffs-Sitzung).

- [ ] Gerätedetail → „Entfernen …“. **Erwartet:** Die Vorschau listet die Kategorien und die Anzahl der
      defconf-Regeln. Warnungen erscheinen, wenn der Router ohne Plattform keine Default-Route bzw. kein Masquerade
      hätte.
- [ ] Als Techniker ist der Button nicht vorhanden (API 403). Ohne korrekten Gerätenamen ist er gesperrt.
- [ ] „Router bereinigen und entfernen“. **Erwartet:** Schritte 1–6 grün. Danach auf dem Router (Konsole/WinBox
      lokal):
      - `/ip firewall filter print where comment~"defconf"` → aktiv.
      - Nach ca. 1 Minute: `print where comment~"sdwan"` in allen Menüs leer.
      - `/user print`, `/user group print`, `/interface wireguard print` → keine sdwan-Einträge.
      - Der Scheduler `sdwan-offboard` hat sich entfernt.
      - Dienste www/winbox/ssh stehen wie vor dem Fernzugriff.
- [ ] **Annahme prüfen:** Der Scheduler läuft vollständig durch, obwohl der API-Benutzer und der Tunnel in seinem
      eigenen Lauf entfernt werden. Ergebnis: ______
- [ ] Router hat weiterhin Internet über die eigene (defconf-)Konfiguration, LAN-Clients ebenfalls.
- [ ] Offboarding-Archiv: Eintrag mit Protokoll, Backup-Download funktioniert.
- [ ] Abbruch testen: Während des Bereinigens den Tunnel trennen (z. B. WAN kurz ziehen). **Erwartet:** Das
      Protokoll zeigt den fehlgeschlagenen Schritt, das Gerät bleibt in der Plattform, defconf ist bereits wieder
      aktiv.
- [ ] Zweites Gerät „Nur aus der Plattform entfernen“. **Erwartet:** Deutliche Warnung. Der Router ist danach
      unverändert (sdwan-Objekte, API-Benutzer und Tunnel bestehen weiter).

## 18. Plattform-Sicherung und Wiederherstellung (Phase 21)

- [ ] `age-keygen` auf einem Admin-Rechner, Public Key in `.env` (`PLATFORM_BACKUP_AGE_RECIPIENT`),
      `docker compose up -d`. Plattform-Sicherung → „Jetzt sichern“. **Erwartet:** Status „erfolgreich“, Datei unter
      `./backups`, Teile `db.dump`, `env`, `hub/hub.key`, `hub/wg0.conf` ohne Warnungen.
- [ ] **Annahme prüfen:** Das Image enthält `pg_dump` 16 (PGDG) passend zum Server. Ergebnis: ______
- [ ] Optional rclone-Ziel (S3 oder SFTP): Datei erscheint extern; Aufbewahrung löscht alte Dateien.
      Tatsächliches Verhalten von `rclone delete --min-age`: ______
- [ ] Falschen Recipient setzen → Status „fehlgeschlagen“, Mail „Plattform-Sicherung fehlgeschlagen“, nach
      erfolgreicher Sicherung „Behoben“.
- [ ] **Wiederherstellung auf frischer VM** mit DNS-Umstellung des Hub-Namens: `deploy/restore.sh`. **Erwartet:**
      Anmeldung mit den alten Zugangsdaten, alle Router werden ohne Eingriff online. Dauer bis alle online sind:
      ______
- [ ] Testwiederherstellung ohne DNS-Umstellung (Abschnitt 4 der DR-Anleitung): Daten vollständig, Router-Backups
      lesbar.

## 19. Zwei-Faktor-Anmeldung (Phase 22)

- [ ] Profil → Zwei-Faktor → einrichten mit einer Authenticator-App (z. B. Aegis, Google Authenticator).
      **Erwartet:** QR wird erkannt, Code aktiviert 2FA, 10 Wiederherstellungscodes zum Sichern.
- [ ] Abmelden/Anmelden: Passwort, dann Code. Derselbe Code ein zweites Mal wird abgelehnt.
- [ ] Anmeldung mit einem Wiederherstellungscode; danach „9 übrig“.
- [ ] MSP-Admin ohne 2FA (neue Installation, `MFA_ENFORCE_SUPERUSER=true`): Die Anmeldung erzwingt die Einrichtung.
- [ ] Benutzerseite: „Zwei-Faktor für alle Benutzer verpflichtend“ → Techniker muss bei der nächsten Anmeldung
      einrichten.
- [ ] 5× falsches Passwort → „vorübergehend gesperrt“ (auch mit richtigem Passwort). Admin „Sperre aufheben“.
- [ ] MSP-Admin „2FA zurücksetzen“ → Plattform-Webhook kommt an (falls `PLATFORM_WEBHOOK_URL` gesetzt).
- [ ] CLI: `docker compose exec api python -m app.cli reset-2fa <email>` und `… unlock <email>` → Audit-Log
      zeigt `via: cli`.
- [ ] Uhrzeit des Servers prüfen (NTP) – bei Abweichung > 30 s schlagen Codes fehl. Ergebnis: ______

## 20. Sicherheitsmeldungen (Phase 23)

- [ ] Eine echte Meldung aus den MikroTik-Security-Advisories eintragen (Version des Testgeräts im Bereich,
      Funktion „allgemein“). **Erwartet:** Geräteliste, Gerätedetail, Firmware-Seite und Dashboard zeigen das
      Gerät als betroffen mit „behoben ab“.
- [ ] Meldung mit Funktion „winbox“: Nach ≤ 10 min wird das Gerät als betroffen geführt (WinBox aktiv); nach
      `/ip service disable winbox` und ≤ 10 min nicht mehr.
- [ ] Meldung „Hotspot“, Schweregrad hoch: Hotspot anlegen → abgelehnt mit Hinweis „erst Firmware aktualisieren“.
      Nach Firmware-Update über die Firmware-Seite ist das Anlegen möglich.
- [ ] Alarmregel „Sicherheitsmeldung betrifft Gerät“ anlegen → Alarm; nach dem Update behoben.
- [ ] Compliance MSP-Baseline: Regel „Keine bekannten Sicherheitsmeldungen“ schlägt fehl bzw. ist nach dem
      Update ok.

## 21. Vor-Ort-Zugang und API-Tokens (Phase 24)

Voraussetzung: Router im Werkszustand (defconf) onboarden; Notebook am LAN, zweites Notebook am WAN-Port-Netz.

- [ ] Nach dem Onboarding (≤ 1 Poll-Intervall): Gerätedetail „Vor-Ort-Zugang“ aktiv; auf dem Router
      `/user print detail where name=localadmin` → Gruppe `sdwan-local`, `address=` nur LAN-Netz(e);
      `/user group print where name=sdwan-local` → Policies wie `LOCAL_POLICIES` (**ANNAHME:** Anlegen durch den
      API-Benutzer klappt).
- [ ] `/tool mac-server mac-winbox print` → `allowed-interface-list=sdwan-local-access` (**ANNAHME:** Feldname).
- [ ] Passwort anzeigen (mit Begründung) → Audit-Eintrag und Webhook vorhanden.
- [ ] WinBox per IP und SSH vom LAN-Notebook mit `localadmin` → Anmeldung klappt.
- [ ] Firewall-Policy mit Default-Drop ausrollen → WinBox/SSH vom LAN weiterhin möglich.
- [ ] WinBox/SSH vom WAN-Notebook → nicht erreichbar; MAC-WinBox über den WAN-Port → Router nicht sichtbar.
- [ ] **ANNAHME (Labor) ausdrücklich: MAC-WinBox-Anmeldung** vom LAN-Notebook (Neighbors → MAC-Adresse) mit dem
      address-beschränkten Benutzer `localadmin`.
      **Ergebnis:** ☐ Anmeldung klappt ☐ Anmeldung abgelehnt („login failure“)
      Bei „abgelehnt“: Default der Einstellung „Adressbeschränkung des Vor-Ort-Benutzers“ auf aus ändern
      (`local_access.tenant_settings`) und diesen Schritt mit „aus“ wiederholen:
      **Ergebnis (aus):** ☐ Anmeldung klappt ☐ abgelehnt
- [ ] Einstellung „Adressbeschränkung“ aus, Zugang erneut anlegen → `/user` ohne `address`, `/ip service print`
      winbox/ssh `address=` LAN-Netz + Hub-IP; WinBox vom WAN weiterhin nicht möglich.
- [ ] Service-Port (z. B. ether5) einrichten → Port nicht mehr in der Bridge, Notebook an ether5 bekommt per DHCP
      eine Adresse aus 192.168.254.0/29, WinBox auf 192.168.254.1 klappt. Service-Port wieder aus → Port zurück in
      der Bridge (**ANNAHME:** DHCP-Server-/Bridge-Port-Felder).
- [ ] Router ohne Zonen und ohne defconf-Liste LAN: Status „nicht angelegt“ mit Grund in Gerätedetail und
      Geräteliste; Compliance „Vor-Ort-Zugang vorhanden“ schlägt fehl. Netz manuell angeben → angelegt.
- [ ] Rotation: neues Passwort funktioniert, altes nicht mehr. Tunnel kurz trennen, Rotation auslösen → Fehler,
      altes Passwort funktioniert weiter und wird weiter angezeigt.
- [ ] Export age und ZIP → mit `age -d` bzw. 7-Zip öffnen, in KeePass als CSV importieren.
- [ ] Offboarding mit „Vor-Ort-Zugang behalten“ → nach Ablauf des Scripts Anmeldung per WinBox aus dem LAN mit dem
      exportierten Passwort möglich (Werks-Firewall aktiv); Kommentar „lokaler Zugang (ehemals verwaltet)“.
- [ ] API-Token „nur lesen“ erstellen → `curl -H "Authorization: Bearer sdw_…" …/api/v1/devices` klappt, POST
      liefert 403; Token widerrufen → 401.

## 22. Nachbarn, Top-Verbraucher, Inventar, ZTP-Import (Phase 25)

- [ ] Switch/zweiter MikroTik am LAN: nach ≤ 10 min Tab „Nachbarn“ mit Interface, Identity, Modell, MAC, Adresse
      (**ANNAHME:** Felder von `/ip/neighbor`). Ist der Nachbar ein Plattform-Gerät, ist er verlinkt.
- [ ] Standorte → „Topologie“: Router und Nachbarn je Interface.
- [ ] Top-Verbraucher einschalten → `/ip traffic-flow print` enabled=yes, interfaces = WAN;
      `/ip traffic-flow target print` → Hub-IP, Port 2055, version=ipfix (**ANNAHME:** Feldnamen).
- [ ] Vom LAN-Notebook Datenverkehr erzeugen (Download); nach ≤ 2 min: Top-Host = Notebook-IP, Top-Ziel = Server-IP,
      WAN korrekt zugeordnet (**ANNAHME:** IPFIX-IEs und Interface-Index).
      **Ergebnis:** ☐ Werte plausibel ☐ keine Daten ☐ WAN falsch zugeordnet
- [ ] Ausschalten → Ziel entfernt, `/ip traffic-flow` wieder im Vorzustand.
- [ ] Inventar: Kaufdatum/Garantie pflegen, EOL-Eintrag für das Testmodell anlegen → Hinweis; CSV-Export in Excel
      öffnen (Umlaute korrekt).
- [ ] ZTP-Import: CSV mit einer gültigen und einer fehlerhaften Zeile → Vorschau zeigt den Fehler; nach Bestätigung
      nur die gültige Zeile angelegt; Bootstrap-Script auf dem Testgerät ausführen → Provisionierung wie gewohnt.

---

## Ergebnis

| Schritt | Ergebnis | Abweichung / Notiz |
|--------:|----------|--------------------|
| 1 Grundkonfiguration | ☐ ok ☐ Abweichung | |
| 2 Onboarding | ☐ ok ☐ Abweichung | |
| 3 Selbsttest | ☐ ok ☐ Abweichung | JSON-Export: |
| 4 WAN-Failover | ☐ ok ☐ Abweichung | Umschaltzeit: |
| 5 VRRP | ☐ ok ☐ Abweichung | Umschaltzeit: |
| 6 Backup | ☐ ok ☐ Abweichung | |
| 7 Firmware | ☐ ok ☐ Abweichung | von … auf … |
| 8 Fernzugriff | ☐ ok ☐ Abweichung | |
| 9 Neustart | ☐ ok ☐ Abweichung | |
| 10 Firewall-Editor | ☐ ok ☐ Abweichung | |
| 11 Threat-Feeds | ☐ ok ☐ Abweichung | |
| 12 Compliance/Suche | ☐ ok ☐ Abweichung | |
| 13 Scripts | ☐ ok ☐ Abweichung | |
| 14 Wartung/Speedtest/Syslog | ☐ ok ☐ Abweichung | |
| 15 WLAN | ☐ ok ☐ Abweichung | |
| 16 Gäste-Portal | ☐ ok ☐ Abweichung | |
| 17 Offboarding | ☐ ok ☐ Abweichung | |
| 18 Plattform-Sicherung | ☐ ok ☐ Abweichung | |
| 19 Zwei-Faktor | ☐ ok ☐ Abweichung | |
| 20 Sicherheitsmeldungen | ☐ ok ☐ Abweichung | |
| 21 Vor-Ort-Zugang/API-Tokens | ☐ ok ☐ Abweichung | MAC-WinBox mit Adressbeschränkung: ☐ ok ☐ abgelehnt |
| 22 Nachbarn/Flows/Inventar/Import | ☐ ok ☐ Abweichung | IPFIX: |
