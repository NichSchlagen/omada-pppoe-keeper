# omada-pppoe-keeper

Hält die PPPoE-Verbindung eines Omada-Gateways (z.B. ER8411) über den Omada-Controller am Leben.
Nutzt [tplink-omada-client](https://pypi.org/project/tplink-omada-client/), um den WAN-Port
zu trennen und neu zu verbinden.

- Prüft per Ping auf mehrere IPs (1.1.1.1, 9.9.9.9, 8.8.8.8), ob das Internet erreichbar ist.
- Ist es länger als `DOWN_GRACE` (180 s) weg, wird die PPPoE-Verbindung neu aufgebaut.
- Zwischen zwei Eingriffen liegt mindestens `COOLDOWN` (600 s), auch wenn der Controller nicht erreichbar war.
- Eigene tägliche Zwangstrennung zu einer festen Zeit (`FORCE_AT`, Standard 04:00), siehe unten.
- Zustand in `/var/lib/omada-pppoe-keeper/state.json`, atomar geschrieben.
- Exit-Codes: 0 = alles in Ordnung, 1 = offline bzw. Eingriff nötig/fehlgeschlagen, 2 = Konfigurationsfehler.

## Zwangstrennung bei Omada

Viele deutsche Provider trennen PPPoE-Verbindungen spätestens nach 24 Stunden (Zwangstrennung).
Omada-Gateways bauen die Verbindung danach oft nicht sauber wieder auf:

- Der Router hängt in `PADI timed out` und ist bis zu 15 Minuten offline, manchmal auch länger
  ([TP-Link Community](https://community.tp-link.com/en/home/uc/post/3580704?page=3)).
- Ein ER8411 verband sich nach einem Verbindungsverlust gar nicht mehr von selbst, erst ein Klick
  auf "Connect" im Controller half
  ([TP-Link Community](https://community.tp-link.com/en/smart-home/forum/topic/855340)).
- TP-Link hat in der ER8411-Firmware 1.4.1 einen Fehler behoben, bei dem sich PPPoE-Verbindungen
  "might occasionally fail to reconnect automatically". Das Problem tritt trotzdem weiter auf.

Vermutlich läuft beim Provider noch die alte PPP-Session, während der Router schon neu wählt.
Ein manuelles Trennen mit PADT und anschließendes Verbinden klappt dagegen zuverlässig.

Das Skript trennt deshalb einmal täglich selbst, zu einer Zeit, die nicht stört (`FORCE_AT`,
Standard 04:00): Disconnect, 10 Sekunden warten, damit das PADT rausgeht, dann Connect. Damit
beginnen die 24 Stunden beim Provider neu, und seine eigene Zwangstrennung fällt nicht mehr
mitten in den Tag. Fällt die Leitung trotzdem aus, verbindet die Überwachung nach `DOWN_GRACE` neu.

## Setup

```bash
sudo mkdir -p /opt/omada-pppoe-keeper
sudo python3 -m venv /opt/omada-pppoe-keeper/venv
sudo /opt/omada-pppoe-keeper/venv/bin/pip install tplink-omada-client
sudo install -m 755 omada-pppoe-keeper.py /opt/omada-pppoe-keeper/
sudo install -m 600 omada-pppoe-keeper.env.example /etc/omada-pppoe-keeper.env
sudo nano /etc/omada-pppoe-keeper.env         # Controller, Zugangsdaten, Gateway-MAC eintragen

# Testlauf: trennt sofort und verbindet neu
sudo sh -c 'set -a; . /etc/omada-pppoe-keeper.env; /opt/omada-pppoe-keeper/venv/bin/python /opt/omada-pppoe-keeper/omada-pppoe-keeper.py --force'
```

Controller-Benutzer: Omada-Controller -> Admin -> lokalen Benutzer mit Schreibrechten auf die Site anlegen.

## Automatisch ausführen

**systemd (empfohlen)**, alle 2 Minuten, Logs im Journal:

```bash
sudo cp systemd/omada-pppoe-keeper.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now omada-pppoe-keeper.timer
journalctl -u omada-pppoe-keeper -f            # Logs ansehen
```

**oder cron:**

```bash
*/2 * * * * set -a; . /etc/omada-pppoe-keeper.env; /opt/omada-pppoe-keeper/venv/bin/python /opt/omada-pppoe-keeper/omada-pppoe-keeper.py >> /var/log/omada-pppoe-keeper.log 2>&1
```
