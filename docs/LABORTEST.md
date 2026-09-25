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
- [ ] Eigene Gruppe für den API-Benutzer mit genau den nötigen Policies:

  ```
  /user group add name=sdwan-api policy=api,read,write,policy,reboot,test,ssh,sensitive
  ```

  `api, read, write` für alle Funktionen, `policy` für die temporären Fernzugriffs-Benutzer, `reboot` für
  Neustart und Firmware, `test` für Ping, `ssh` für den Backup-Export. `sensitive` ist optional: Ohne diese
  Policy fehlen Schlüssel und Passwörter im Export, das Backup reicht dann nicht für eine Wiederherstellung.
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
- [ ] **Hinweis Onboarding:** Das Onboarding-Skript legt den Benutzer `sdwan` derzeit in der Gruppe `full`
      an und setzt `api` auf die Hub-Adresse. Nach Schritt 2 deshalb umstellen:

  ```
  /user set [find name=sdwan] group=sdwan-api
  ```

  **Erwartet:** Der Selbsttest (Schritt 3) zeigt „Rechte API-Benutzer“ grün. Fehlt `sensitive`,
  ist die Zeile orange.

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
- [ ] Auf dem Router: `/user set [find name=sdwan] group=sdwan-api` (siehe Schritt 1).

## 3. Selbsttest

- [ ] Übersicht → Karte „Selbsttest“ → „Selbsttest ausführen“.
      **Erwartet:** Dauer unter 30 s. Alle Pfade sind erreichbar, und keine Zeile ist rot.
- [ ] Orange Zeilen einzeln aufklappen und bewerten. Mögliche Ursachen:
  - **Leere Pflicht-Tabelle:** Vor der WAN-Einrichtung (Schritt 4) kann `/tool netwatch` leer sein. Das
    ist in Ordnung.
  - **Fehlende Felder** wie `rtt-avg`/`loss-percent` bei Netwatch oder `master`/`backup` bei VRRP:
    Feldnamen notieren. Die Rolle wird dann über `running` abgeleitet.
  - **Rechte:** `sensitive` fehlt.
- [ ] Zeile „Paket-Update“: `latest-version` fehlt vor der ersten Update-Prüfung. Das ist nur ein Hinweis.
- [ ] Zeile „Uhrzeitabweichung“: unter 60 s.
- [ ] Zeilen „Dienst api“ und „Dienst ssh“: aktiv und für `10.100.0.1` erlaubt.
      Zur Gegenprobe `/ip service set ssh address=192.168.88.0/24` setzen und den Selbsttest wiederholen.
      **Erwartet:** SSH-Zeile rot mit „Backup-Export schlägt fehl“. Danach zurückstellen.
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
- [ ] Hinweis: Der Firmware-Reboot nutzt die 5-Minuten-Unterdrückung **nicht**. Ein Offline-Alarm ist
      möglich, wenn der Router länger als Poll-Kulanz plus Regel-Dauer weg ist. Beobachten und notieren.
- [ ] Nach dem Update den Selbsttest wiederholen und die neue Version im JSON-Export vergleichen.

## 8. Fernzugriff

- [ ] Kopf → „Fernzugriff starten“ → WinBox, 15 min, erlaubte Quelle = eigene IP.
      **Erwartet:** Verbindungsdaten mit Port aus dem Proxy-Bereich und temporärem Benutzer
      `sdwan-rs-…`. WinBox verbindet über `<server>:<port>`.
- [ ] Von einer anderen Quell-IP verbinden. **Erwartet:** Die Verbindung wird abgelehnt.
- [ ] SSH-Sitzung genauso testen.
- [ ] Sitzung beenden oder ablaufen lassen. **Erwartet:** Der temporäre Benutzer ist auf dem Router
      entfernt (`/user print`), der Port ist geschlossen, das Audit-Log enthält Start und Ende.
- [ ] **Prüfen:** Das Anlegen des temporären Benutzers (Gruppe `full`) klappt mit der Gruppe
      `sdwan-api`. Meldet RouterOS fehlende Rechte, die Meldung notieren. Die Gruppe braucht dann
      zusätzliche Policies.
- [ ] Hinweis: WebFig-Fernzugriff schaltet den Dienst `www` ein (nur Hub-Adresse). Nach dem Test
      `/ip service print` prüfen und `www` wieder abschalten.

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
