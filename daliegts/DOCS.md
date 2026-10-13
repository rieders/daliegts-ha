# DaLiegt’s für Home Assistant

DaLiegt’s verwaltet Bauteile, Werkzeug und Vorräte – und lässt das richtige Fach per WLED aufleuchten.
Als App läuft es direkt auf deinem Home Assistant und erscheint in der Seitenleiste.

## Erster Start

1. App starten, dann links in der Seitenleiste **DaLiegt’s** öffnen.
2. Du bist automatisch mit deinem Home-Assistant-Benutzer angemeldet. Der erste Benutzer wird Admin,
   weitere Home-Assistant-Benutzer bekommen die Gruppe „Familie“ (änderbar unter Verwaltung › Benutzer).
3. Lagerorte anlegen oder alte Daten importieren (Verwaltung › Import: Part-DB, InvenTree, DaLiegt’s-Export).

## Handy-Scanner

Die Kamera braucht HTTPS. Über die Home-Assistant-App bzw. Nabu Casa ist das automatisch der Fall.

## QR-Codes auf Etiketten

QR-Codes enthalten eine Adresse. Soll ein Handy ohne Home Assistant sie öffnen können:
unter **Netzwerk** Port 8000 freigeben und in den Optionen **Adresse für QR-Codes** eintragen,
z. B. `http://homeassistant.local:8000`. Über diesen Port meldet man sich mit Benutzername und Passwort an
(Passwort unter „Konto“ setzen).

## Datensicherung

Alle Daten liegen im App-Ordner und sind in den Home-Assistant-Backups enthalten. Zusätzlich legt DaLiegt’s
jede Nacht eine eigene Sicherung an (Verwaltung › Backup).

## Umzug von einer anderen Installation

Alte Installation: Verwaltung › Backup › Export. In der App: Verwaltung › Import › DaLiegt’s-Export.

## Lizenz

Die Grundversion ist kostenlos. DaLiegt’s Pro (Etikettendrucker, 3D-Drucker, Bestellplaner, Projekte,
Sprachsuche, mehrere WLED-Controller) wird unter Verwaltung › Lizenz freigeschaltet.
