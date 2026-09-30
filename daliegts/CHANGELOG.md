# Changelog

## v0.22.2 – 2026-09-30

- Home Assistant kann DaLiegt’s jetzt direkt aus `github.com/rieders/Lagerverwaltung` installieren: `repository.yaml` im Hauptordner, App-Beschreibung in `daliegts/` mit fertigen Images von ghcr.io. Das separate Repository `daliegts-ha` und der Token entfallen.

## v0.22.1 – 2026-09-30

- Home-Assistant-App über GitHub: Die Action `ha-app.yml` baut bei jedem Versions-Tag fertige Images (amd64, aarch64) auf ghcr.io und aktualisiert das öffentliche App-Repository `daliegts-ha` (nur Beschreibung/Icons, kein Quellcode). In Home Assistant genügt dann das Repository hinzufügen; Updates erscheinen dort automatisch. Anleitung in `docs/HOME-ASSISTANT-APP.md`.

## v0.22.0 – 2026-09-30

- **Grundversion und DaLiegt’s Pro**: Lizenzschlüssel (Ed25519-signiert, offline geprüft) unter Verwaltung › Lizenz. Pro: Etiketten-Direktdruck, 3D-Drucker & Filament, Bestellplaner, Projekte/Stücklisten, Sprachsuche für Home Assistant, mehr als ein WLED-Controller. Ohne Lizenz bleiben alle Daten erhalten; „Nachdrucken“ öffnet dann den Browserdruck. Menüpunkte zeigen „Pro“. Lizenzen erstellen: `python -m app.license_tool` (siehe `docs/LIZENZEN-ERSTELLEN.md`).
- **Home-Assistant-App**: DaLiegt’s läuft direkt auf Home Assistant OS mit eigenem Eintrag in der Seitenleiste (Ingress). Der HA-Benutzer wird automatisch angemeldet, Daten sind in den HA-Backups enthalten. Bauen mit `./scripts/build_ha_app.sh`, Anleitung in `docs/HOME-ASSISTANT-APP.md`.

## v0.21.0 – 2026-09-30

- **LED-Ketten einmessen**: In der Lageransicht bei einer LED-Kette „🧭 Einmessen“ – am Streifen pulsiert die erste LED des nächsten Abschnitts grün, du tippst das Magazin an, an dem sie leuchtet. So ergibt sich die Reihenfolge der zusammengesteckten Magazine von selbst, ohne LEDs zu zählen. Mit ◀ ▶ lässt sich der Startpunkt um einzelne LEDs korrigieren (Kabelbrücken), „Rückgängig“ nimmt das letzte Magazin zurück.
- **„🎨 Prüfen“**: jedes Magazin der Kette leuchtet 20 Sekunden in einer eigenen Farbe, derselbe Farbpunkt erscheint am Modul in der Wandansicht.
- Neue Anleitung `docs/LED-VERKABELUNG.md`: Magazine modular verkabeln (Stecker rein/raus, Belegung, Strom, Reihenfolge, Einmessen).

## v0.20.1 – 2026-09-30

- **Neuer Name: DaLiegt’s.** Überall sichtbar umbenannt (Titel, Menü, Anmeldung, Einrichtung, Handy-App, Testdruck, Hilfetexte). Technisch bleibt alles beim Alten: Installationsordner `/opt/lagerwerk`, Datenbank, Updates, API-Header `X-Lagerwerk`, Home-Assistant-Befehl `LagerwerkWo`, Exportformat v1 (alte Exporte lassen sich weiter einspielen).
- KiCad: Artikelcode im Symbolfeld „DaLiegts“ wird erkannt (das bisherige Feld „Lagerwerk“ weiterhin).

## v0.20.0 – 2026-09-30

- **Zwei Etiketten je neuer Filament-Spule**: beim Anlegen „Spule + Verpackung“ (oder nur eins) – werden gleich auf den Etikettendrucker geschickt. Beide tragen denselben Bestands-Code (S-…), egal was du scannst, es ist diese Spule. Neue Vorlagen „Filament – Spule 40×16“ und „Filament – Verpackung 62×29“ (im Designer anpassbar); neues Feld „Angelegt am“ für Bestandsetiketten.
- Nach dem Anlegen erscheint eine Karte mit Knöpfen für beide Etiketten (ohne Etikettendrucker: Browserdruck).
- **Nachdruck** eines einzelnen Etiketts mit demselben Code (Standardvorlage, Standarddrucker, ohne Umweg): beim Lagerort/Fach, in der Lageransicht beim gewählten Fach, beim Artikel (Artikel-Etikett und je Bestand; Filament: Spule/Verpackung), bei eingesetzten Spulen und CFS-Plätzen.
- Etikettenseite: „Etikett kaputt oder unleserlich?“ – aufgedruckten Code abtippen (L-/A-/S- oder InvenTree-Code) und nachdrucken.

## v0.19.1 – 2026-09-30

- 3D-Druck: **„＋ Neue Spule anlegen“** direkt auf der Druckerseite (Material, Farbe, Marke, Restgewicht, Lagerort) – legt Artikel (Art Filament, Einheit g) und Spule an und setzt sie auf Wunsch gleich in den Drucker bzw. einen freien CFS-Platz. Jede Spule ist ein eigener Bestand.
- Spulenliste zeigt auch Artikel aus der Kategorie „Filament“ (und Unterkategorien); ist sie leer, steht ein Hinweis darin.

## v0.19.0 – 2026-09-30

- **Weitere Etikettendrucker**: Druckersprache wählbar – **Brother QL**, **Zebra (ZPL)**, **TSPL** (TSC, Xprinter, Munbyn, Godex …) und **Systemdrucker über CUPS** (z. B. DYMO LabelWriter, Laserdrucker). Netzwerk (Port 9100), USB am Server (`usb:/dev/usb/lp0`) oder CUPS-Warteschlange. Auflösung 203/300/600 dpi, Etikettengröße, Drehung, Versatz, Schwärzung; TSPL mit Lücke und „Farben umkehren“. Testdruck, Messmuster, Vorschau und Direktdruck für alle.
- **Brother-Treiber selbst geschrieben** (nach Brothers Raster-Befehlsreferenz) – die GPL-Bibliothek `brother_ql_inventree` ist entfernt. Ausgabe byte-genau geprüft gegen die bisherige Bibliothek (1572 Kombinationen aus Modell, Rolle, Schneiden, Schwärzung). Alle Abhängigkeiten sind jetzt MIT/BSD/Apache (siehe `docs/LIZENZEN.md`).
- Etiketten werden in der Auflösung des Druckers gezeichnet (scharfe QR-Codes auch bei 203 dpi).
- Behoben: Testdruck auf einer frischen Installation ohne Etikettenvorlagen.
- Migration 0013: Spalten `driver`, `dpi`, `media_w_mm`, `media_h_mm`, `gap_mm`, `invert` an `label_printers`.

## v0.18.0 – 2026-09-29

- **Neue Navigation**: Seitenleiste links mit Icons und Gruppen (Lager · Projekte · Einkauf · Werkstatt · Daten), Verwaltung und Konto unten; per « auf schmale Icon-Leiste einklappbar (wird gemerkt). Kopfzeile mit großer Suche (Taste „/“), „＋ Neu“-Menü (Artikel, Katalog, Foto erkennen, Projekt), LEDs aus und Scannen.
- **Am Handy**: Tab-Leiste unten (Start · Suchen · Scannen · Buchen · Menü) mit großem Scan-Knopf; das Menü öffnet als Schublade.
- Haltbarkeit, Inventur und 3D-Druck sind direkt im Menü erreichbar.

## v0.17.0 – 2026-09-29

- 3D-Druck: **Druckermodell** (K1, K1C, K1 Max, K1 SE, K2, Hi) und Option **„CFS vorhanden“** mit 4 Plätzen – Spulen je Platz scannen oder wählen. Mit einer Spule im CFS wird automatisch abgebucht, bei mehreren fragt Lagerwerk nach (bis der Verbrauch je Platz anhand eines Mitschnitts eingebaut ist). Warnung, wenn keine CFS-Spule genug Filament hat.
- **Druckmodelle**: Lagerwerk holt die Dateiliste vom Drucker (beim Verbinden, stündlich, per Knopf) mit geplantem Verbrauch aus dem Slicer und merkt sich nach jedem Druck den tatsächlichen. Tabelle „Reicht das eingelegte Filament?“ mit ✓/✗ je Spule; beim Nachdrucken kommt die Warnung schon beim Start.
- **Mitschnitt** der Drucker-Telemetrie (2/12/24 Std.) zum Herunterladen – Grundlage für die CFS-Unterstützung je Platz.
- Migration 0012: Spalten `model`, `has_cfs`, `cfs_slots`, `recording_until` an `printers_3d`, Tabelle `print_models`.

## v0.16.0 – 2026-09-29

- **3D-Druck: Filamentverbrauch automatisch** (Menü „3D-Druck“, Verwaltung › 3D-Drucker): Creality K1/K1C/K1 Max mit Original-Firmware (WebSocket Port 9999, kein Root). Spule per Scan oder Liste einsetzen; nach jedem Druck – auch abgebrochen – wird der tatsächliche Verbrauch in g (bzw. kg/m) abgebucht. Kennt der Drucker das geplante Gewicht der Datei, wird mit dem Verhältnis aus dem Slicer gerechnet, sonst über Durchmesser und Dichte (PLA/PETG/ABS/ASA/TPU/PC/PA aus dem Namen). Warnung „Spule reicht nicht“ beim Druckstart (Seite, Startseite, Home-Assistant-Sensor). Drucke ohne eingesetzte Spule werden gemerkt und können nachgetragen werden.
- **„Wo ist …?“ per Sprache** (Verwaltung › Home Assistant): API `GET /api/v1/wo?q=…` liefert einen Antwortsatz und lässt das Fach über WLED aufleuchten; fertige Home-Assistant-Konfiguration (Custom Sentences, rest_command, intent_script, Sensoren) zum Kopieren und Testfeld.
- **Datenblätter offline**: verlinkte Datenblätter werden als PDF beim Artikel gespeichert – nachts automatisch (bis 40), per Knopf am Artikel oder alle auf einmal (Verwaltung › Info-Provider). Webseiten mit PDF-Link werden aufgelöst; Fehlschläge werden 30 Tage nicht wiederholt.
- **Haltbarkeit** (`/ablauf`, Knopf „⏳ Haltbarkeit“ auf der Startseite): abgelaufen / in 30 Tagen / später, dazu Garantien, die bald enden. Kachel auf der Startseite, Zähler im Home-Assistant-Sensor.
- Migration 0011: Tabelle `printers_3d`.

## v0.15.0 – 2026-09-29

- **Buchen** (neuer Menüpunkt, PC mit Handscanner und Handy-Kamera):
  - 📤 **Ausbuchen**: Artikel scannen → Menge (Vorschlag 1) → Enter. Mehrere Bestände: Auswahl per Klick oder Nummer + Enter; erst das Fach scannen grenzt ein. Ampel-Artikel: Füllstand danach.
  - 🔀 **Umbuchen**: Artikel scannen → Ziel-Fach scannen → fertig (ganze Menge, optional Teilmenge). Artikel ohne Bestand/ohne Lagerort werden so zugeordnet (Menge abfragen).
  - 📥 **Einräumen**: Ziel-Fach scannen, dann nacheinander alle Artikel, die dorthin kommen.
  - „Zuletzt gebucht“ mit **↶ rückgängig** (Gegenbuchung „Storno“ in der Historie).
  - Scannt der Handscanner versehentlich ins Mengenfeld, wird nicht gebucht, sondern der neue Code verarbeitet.
- **InvenTree-Etiketten**: Bestandsetiketten `{"stockitem": N}` wählen genau den importierten Bestand; auch verstümmelte Scans eines Scanners mit US-Tastaturbelegung (`ÜÄstockitemÄÖ 12*`) und die Kurzform `INV-SI12` werden erkannt.

## v0.14.0 – 2026-09-28

- **Bestellplaner** im Projekt („🛒 Bestellplaner“): Fehlteile für N Platinen + auf Wunsch offene Einkaufsliste + Standardteile unter Mindestbestand (einzeln abwählbar) in einer Bestellung. Bereits bestellte oder schon gelistete Teile werden nicht doppelt gekauft.
- Ziel wählbar: günstigste Summe, wenige Pakete bevorzugen (gedachter Aufschlag je weiteres Paket) oder möglichst alles bei einem Händler. Vergleichstabelle „Alles bei …“ je Händler mit Summe inkl. Versand und was dort fehlt; jede Variante ansehen und übernehmen.
- „🔍 Online suchen“ fragt für alle Artikel des Bedarfs die eingeschalteten Händler ab (aktuelle Preise + neue Angebote); mehrdeutige Treffer landen wie gewohnt unter „Entscheidungen“.
- Fehlende Versandkosten direkt im Planer nachtragen; nicht zugeordnete Stücklistenteile per Knopf als Artikel anlegen.
- „Übernehmen“ setzt alles mit festem Händler auf die Einkaufsliste – dort Warenkorb-Export je Händler (CSV / Schnellbestellung) und „bestellt“-Status.

## v0.13.2 – 2026-09-28

- `install.sh`: Paketinstallation hängt nicht mehr stumm – bereits vorhandene Pakete werden übersprungen, apt-Ausgabe ist sichtbar, Zeitlimits für apt-Sperre und Download; schlägt nur die Schrift-Installation fehl, läuft die Installation weiter.

## v0.13.1 – 2026-09-28

- Etiketten-Vorlagen: **Zeilenumbruch je Zeile** („↵ nein / 2 Zeilen / 3 Zeilen“). Lange Namen brechen wortweise um statt abgeschnitten zu werden; erst wenn auch die letzte erlaubte Zeile voll ist, kommt „…“. Gilt für Browser-Druck und Direktdruck.

## v0.13.0 – 2026-09-28

- **Direktdruck auf Brother-QL-Netzwerkdrucker** (Etiketten → „🖨 Drucker“): Drucker mit IP-Adresse, Modell und eingelegter Rolle (Endlosband 12–102 mm oder Einzeletiketten) anlegen. Lagerwerk zeichnet die Etiketten selbst (300 dpi, gleiche Aufteilung wie die Vorschau) und schickt sie direkt an Port 9100 – kein Druckdialog, keine Skalierungsprobleme.
- **Testdruck**: Messmuster (Rahmen + 5-mm-Striche) oder Beispiel-Etikett je Art; Bildvorschau, wie es aus dem Drucker kommt; „Verbindung prüfen“; Feinjustierung (Versatz quer, Drehung 90/270°, Schwärzung).
- Etiketten-Seite: „🖨 n Etiketten an Drucker senden“ (Lagerorte auf Wunsch gleich als gedruckt markieren). Vorlagen-Designer: Testdruck auch ungespeicherter Vorlagen.
- Neue Abhängigkeit `brother_ql_inventree` (Brother-Rasterformat); `install.sh` installiert zusätzlich die Schriften `fonts-dejavu-core` und `fonts-liberation`.
- Migration 0010: Tabelle `label_printers`.

## v0.12.1 – 2026-09-28

- Behoben: **WLED ließ sich nicht ausschalten** – „Alle LEDs aus“ startete sofort wieder die Effektwand (Ruhemodus). Jetzt bleibt die Wand aus, bis wieder ein Lagerort angezeigt wird; erst danach zählt die Wartezeit für die Effektwand neu.
- Nach dem Anzeigen eines Lagerorts geht die Wand aus und startet den Effekt erst nach der eingestellten Wartezeit (vorher sofort).
- „Ruhemodus pausieren“ schaltet einen laufenden Effekt sofort aus und bleibt auch nach einem Neustart gespeichert (auch per API `/api/idle-pause`).

## v0.12.0 – 2026-09-28

- **Etiketten-Vorlagen** (Etiketten → „✎ Vorlagen gestalten“): für jede Etikettenart (Lagerort/Fach, Artikel, Bestand) beliebig viele Vorlagen. Je Vorlage: Format (Brother-Band, A4-Bogen oder eigene Größe), QR-Code / Strichcode / beides / keiner, Code links oder rechts, und bis zu 12 frei wählbare Zeilen (Feld, Schriftgröße, fett, freier Text). Live-Vorschau mit echten Daten, eine Vorlage je Art als Standard (★).
- **Artikel-Etiketten**: Name, Beschreibung, Wert, Parameter, MPN, Hersteller, Gehäuse, Kategorie, Lagerort (kurz/voll/Code), Bestand, Mindestbestand, Lieferant, Preis, Tags, Code. Auswahl per Suche (mehrere Artikel), „alle Artikel in Lagerort …“, 🏷 auf der Artikelseite oder Mehrfachauswahl in der Artikelliste („🏷 Etiketten drucken“).
- **Bestands-Etiketten** (Charge/Tüte) zusätzlich mit Menge, Charge, Seriennummer, Ablaufdatum; 🏷 je Bestand auf der Artikelseite.
- Die Etiketten-Seite zeigt immer eine Vorschau – ohne Auswahl mit Beispieldaten.
- Migration 0009 legt die Tabelle `label_templates` an und füllt Standardvorlagen.

## v0.11.2 – 2026-09-28

- Behoben: Etiketten-Vorschau brach mit „int_parsing … ort“ ab, wenn das Lagerort-Feld leer war oder im Browser nicht aufgelöst werden konnte. Der Lagerort wird jetzt auf dem Server erkannt.
- Lagerort-Eingaben (Etiketten, Artikel-Filter, Verschieben, Übertragen, Umlagern …) akzeptieren jetzt auch einen eindeutigen Namen („Kleinteilemagazin1“) oder das Pfadende („Regalwand / Kleinteilemagazin2 / Fach4“); bei Mehrdeutigkeit gibt es einen Hinweis statt einer Fehlerseite.
- Etiketten: „Unterorte einschließen“ lässt sich jetzt auch abwählen.

## v0.11.1 – 2026-09-28

- WLED-Ruhemodus als **Effektwand** einfacher einrichten: Feld „Start nach … Minuten ohne Nutzung“ (war nur intern auf 5 min), „Presets vom WLED laden“ (Auswahl mit Namen statt Nummer raten) und „▶ Effekt jetzt testen“.

## v0.11.0 – 2026-09-28

- **Auslager-Assistent** (▶ im Auftrag): führt Fach für Fach durch die Entnahme, in Laufreihenfolge nach Lagerort.
  - Groß: Lagerort, Bauteil, Bestückungsreferenz (z. B. „C1, C2“) und Menge; Mini-Ansicht des Magazins mit markiertem Fach (bei noch nicht angeordneten Möbeln die Nachbarfächer).
  - Mit WLED leuchtet das Fach automatisch.
  - **Handscanner**: Fach- oder Artikeletikett scannen – „✔ Richtiges Fach“ bzw. „✗ Falsches Fach“; Enter bestätigt die Entnahme.
  - „Fach hat weniger“ (tatsächliche Menge eintragen, Rest wird woanders gesucht oder bleibt offen), „Überspringen“, „Letzte Entnahme rückgängig“.
  - Am Ende: fehlende Teile mit Bestellstatus, Auftrag abschließen.
  - Für Handy optimiert.

## v0.10.1 – 2026-09-28

- **Bestellstatus überall sichtbar**: „🛒 zu bestellen“ (steht auf der Einkaufsliste, noch nicht bestellt) und „📦 bestellt“ (mit Datum, Lieferant und Bestellnummer, noch nicht eingetroffen) – in der Artikelliste, auf der Artikelseite, in Aufträgen, Projekten und in der Lageransicht beim Fach.
- Aufträge: je Fehlteil „muss bestellt werden“, „bestellt am … bei …“ oder „nicht auf der Einkaufsliste“; Übersicht „x zu bestellen / y bestellt“ und **„Alle als bestellt markieren“** (mit Bestellnummer) direkt im Auftrag. Die Auftragsliste zeigt die Zähler ebenfalls.
- Projekte: bei Fehlmengen „bestellt“ / „zu bestellen“ / „nicht bestellt“.
- Artikel unter Mindestbestand ohne Bestellung: Hinweis mit Link zum Nachbestellen.
- Startseite: Kacheln „🛒 zu bestellen“ und „📦 bestellt, unterwegs“.
- Einkaufsliste: Verweis „📋 Auftrag“ auch bei bereits bestellten Positionen.

## v0.10.0 – 2026-09-28

- **Projekte & Baugruppen** (neuer Menüpunkt „Projekte“): die Stückliste einer Platine oder Baugruppe speichern, z. B. „Lichtschranke Rev. A“.
  - **Direkt aus KiCad**: Schaltplan (.kicad_sch), Netzliste (.xml/.net) oder Stücklisten-CSV hochladen. Gleiche Bauteile werden zusammengefasst (R1, R2 …), Versorgungssymbole ignoriert, „nicht bestücken“ (DNP) erkannt, mehrteilige Symbole einmal gezählt.
  - **Automatische Zuordnung** zu Lagerartikeln: Lagerwerk-Code, MPN oder LCSC-Nummer im Symbol, sonst Wert + Gehäuse („10k“ + „R_0805“ findet „MD-0805 10K“, „4k7“ findet „4,7KOhm“). Bei mehreren Treffern Vorschläge zur Auswahl; einmal von Hand zugeordnet wird für künftige Importe gemerkt.
  - Die **Leiterplatte selbst** kann gleich als Artikel angelegt werden und gehört zur Stückliste.
  - **„Wie viele bauen?“**: Bedarf, freier Bestand und Fächer (📍, 💡) für N Stück; zeigt, wie oft das Projekt mit freiem Bestand baubar ist.
  - **Bauen**: legt einen Auftrag „Lichtschranke ×3“ an – Teile reserviert, Fehlteile auf der Einkaufsliste, Pickliste und schrittweise Entnahme wie bei Aufträgen.
  - Neuimport einer geänderten KiCad-Datei ersetzt die Stückliste, von Hand zugeordnete Teile werden wiedererkannt.
- Datenbank-Migration 0008 (Projekte, läuft automatisch mit Backup).

## v0.9.0 – 2026-09-28

- **Aufträge** (neuer Menüpunkt): Auftrag anlegen (Titel, für wen/Anlage, fällig am), Bauteile hinzufügen – per Suche oder als **Stückliste einfügen** („10 BC547“, „4x 100nF 0805“ oder CSV aus KiCad/Excel mit Menge/Wert/Referenz).
  - **Reservierung**: Bauteile sind ab dem Anlegen reserviert. Knappe Teile bekommen Aufträge mit höherer Priorität zuerst, sonst der ältere Auftrag. Am Artikel steht „X reserviert für AU-…, Y frei“.
  - **Fehlteile** kommen automatisch auf die Einkaufsliste (mit Verweis auf den Auftrag). Beim Wareneingang deckt die neue Ware den offenen Bedarf von selbst.
  - **Nach und nach entnehmen**: je Position Menge und Fach wählen oder „Alles Verfügbare entnehmen“; Rückgabe ins Lager möglich. Entnahmen stehen in der Historie mit „Auftrag AU-…“.
  - **Pickliste** zum Ausdrucken, nach Lagerort sortiert; 💡 zeigt alle Fächer des Auftrags; 📍 führt in die Lageransicht.
  - Status offen → in Arbeit → erledigt/storniert; beim Abschließen/Stornieren werden Reservierungen und noch nicht bestellte Fehlteile freigegeben.
  - Auftragscodes (AU-00001) sind scanbar; neues Recht „Aufträge“ (Familie: anlegen).
- Startseite: Kachel „Aufträge offen“.
- Datenbank-Migration 0007 (neue Tabellen für Aufträge, läuft automatisch mit Backup).

## v0.8.1 – 2026-09-28

- Lageransicht: **Ausgewähltes Fach deutlich hervorgehoben** – gelb mit rotem Rand, pulsierender Markierungsring darum, die übrigen Fächer werden abgeblendet (auch nach Scan oder „📍 Wo?“). Klick daneben oder Esc hebt die Auswahl auf. Suchtreffer kräftiger grün.

## v0.8.0 – 2026-09-28

- **Alte Import-Orte übertragen** (🔀 im Lagerorte-Baum, auf jeder Lagerort-Seite und unter Verwaltung › Import › Aufräumen › „Alte Lagerorte zuordnen“):
  - **Fächer 1:1 auf ein Möbel übertragen**: z. B. „Wandregal oben101…133“ aus Part-DB auf „Kleinteilemagazin 2“. Die Fachnummer im alten Namen bestimmt das neue Fach (oben104 → Fach 4), wahlweise der Reihe nach; Vorschau vor dem Übertragen.
  - **Ganzen Ort zusammenführen**: Inhalt, Unterorte (gleichnamige werden zusammengelegt) und Etiketten wandern in den neuen Ort, der alte verschwindet.
  - Alte Etiketten (Part-DB, InvenTree, Lagerwerk-Code des alten Orts) zeigen danach auf das neue Fach. Gleiche Artikel im selben Fach werden zu einem Bestand zusammengelegt. Vorher automatische Sicherung.
- **Artikelliste überarbeitet**:
  - Ohne Suchbegriff werden alle Artikel angezeigt (A–Z, seitenweise).
  - Neue Filter: Lagerort per Name/Pfad/Code (statt ID), „Platzhalter aus dem Import“, „Bestand ohne Lagerort“, „ohne Bestand“.
  - **Mehrfachauswahl**: Artikel anhaken (oder „alle“) und gemeinsam umlagern, einer Kategorie zuordnen oder löschen.

## v0.7.1 – 2026-09-27

- **Lagerorte-Baum bearbeiten** (im Admin-Modus): Jeder Eintrag hat beim Überfahren (am Handy immer) die Knöpfe ＋ Unterort anlegen, ✎ umbenennen, ⤴ verschieben nach … (Name, Pfad oder Code – alles darunter wandert mit) und 🗑 löschen (bei Inhalt mit Zielort, damit nichts verloren geht). Nach dem Anlegen bleibt man im Baum, der neue Ort ist aufgeklappt und markiert.
- Ziehen mit ⠿ verbessert: über einem zugeklappten Ort kurz verweilen klappt ihn auf, leere Orte zeigen beim Ziehen eine Ablagezone.
- Die Zahl im Baum zählt Bestände jetzt inklusive Unterorte.

## v0.7.0 – 2026-09-27

- **Räume unterteilen**: In der Lageransicht unter „✎ Anordnen › Raum unterteilen“ Bereiche wie „Regalwand“ oder „Schreibtisch“ anlegen und Module per „In Bereich verschieben“ hineinlegen. Jeder Bereich hat eine eigene, größere Ansicht; oben wechselt man per Reiter („Ganzer Raum“, Regalwand, Schreibtisch …). Codes und Etiketten bleiben gleich.
  - „Ganzer Raum“ zeigt die Bereiche als Karten mit freien Fächern; die Suche markiert, in welchem Bereich die Treffer liegen, und springt per Klick dorthin.
- **Lagerort-Übersicht aufgeräumt**: Orte mit Unterorten zeigen zuerst die Unterorte (mit Anzahl Bestände), die lange Artikelliste ist eingeklappt.
- **Aufräumen nach dem Import** (Verwaltung › Import › Aufräumen): Platzhalter „Unbenannter Lagerartikel …“ und leere Hilfsorte „InvenTree-Lagerort …“ auf einen Klick löschen – mit automatischer Sicherung vorher.
- **Handscanner am PC** (USB/Funk, z. B. EY-034): Etikett scannen, ohne vorher ein Feld anzuklicken – Lagerwerk springt zum Fach (in der Lageransicht markiert), zum Artikel oder Bestand; Lieferanten-Etiketten öffnen „Bauteil erkennen“. Im Scanner-Menü (📷) funktionieren alle Modi (Einlagern, Entnehmen, Inventur) auch mit dem Handscanner. Codes von Scannern mit US-Tastaturbelegung (ß statt -, y/z vertauscht) werden erkannt.

## v0.6.0 – 2026-09-27

- **Lageransicht als Hauptansicht** (neuer Menüpunkt „Lager“): Raum oben auswählen, alle Magazine und Regale mit ihren Fächern erscheinen.
  - Räume mit angeordneten Möbeln öffnen direkt diese Ansicht; die bisherige Liste gibt es über „Liste“.
  - **Frei oder belegt** auf einen Blick: freie Fächer sind dunkel und mit „frei“ beschriftet. „… von … frei“ hebt alle freien Fächer gelb hervor.
  - **Fach antippen**: Inhalt mit Menge („Widerstand 10k – 100 Stk“), direkt entnehmen – oder „Lagerfach ist frei“ mit „＋ Artikel hier einlagern“.
  - **Wo liegt das? – auch ohne WLED**: Suche markiert die Fächer und listet die Fundstellen mit Menge; antippen springt zum Fach. Am Artikel und im Scanner führt „📍 Wo?“ direkt in die Ansicht.
  - Die Ansicht zoomt auf die Möbel statt auf die leere Wand.
- **Stichproben-Inventur** (🎲 in der Lageransicht und unter Lagerorte): Raum oder Möbel wählen, Anzahl Fächer festlegen, Lagerwerk zieht zufällige Fächer.
  - Keine Wiederholungen: Fächer, die noch nie oder am längsten nicht geprüft wurden, kommen zuerst dran. Fortschritt „x von y Fächern geprüft“.
  - Je Fach Mengen bestätigen oder korrigieren; nur Abweichungen werden gebucht. Zählen im Scanner oder am Artikel zählt ebenfalls als geprüft.
  - „Fächer an der Wand zeigen“ markiert die Stichprobe in der Lageransicht.
- Datenbank-Migration 0006 (nur neue Spalte „zuletzt geprüft“, läuft automatisch mit Backup).
- Behoben: Die Bodenlinie in der Wandansicht war auch außerhalb des Bearbeitens sichtbar.

## v0.5.0 – 2026-09-27

- **Statistik** (neuer Menüpunkt):
  - **Lagerwert** in Euro mit Aufteilung nach Kategorie, Lagerort und Art sowie die wertvollsten Bestände.
  - Wert je Einheit: Kaufpreis am Artikel, sonst zuletzt bezahlter Preis (Wareneingang), sonst günstigster Shop-Preis. Artikel ohne Preis werden aufgelistet.
  - **Buchungen über die Zeit** (30 Tage, 90 Tage, 12 Monate, 5 Jahre): Anzahl, eingelagerte und entnommene Stück, Wert der Entnahmen, Buchungsarten, wer bucht, meist entnommene Artikel.
  - **Einkäufe** je Zeitraum und je Lieferant aus der Einkaufsliste.
  - **Bestandsliste mit Werten als CSV** (für Inventur oder Versicherung, öffnet direkt in Excel).
- Startseite zeigt den Lagerwert (nur mit Recht „Preise lesen“).

## v0.4.1 – 2026-09-27

- Lagermöbel: **leere Plätze** (Lücken ohne Fach), z. B. `* 3` = Spalte 3 in allen Reihen leer. Bleiben beim Umbauen erhalten und werden in Vorlagen gespeichert.
- „Als Vorlage speichern“ übernimmt jetzt auch Benennung und Zählrichtung.

## v0.4.0 – 2026-09-27

- **Bauteil erkennen** (📷 Erkennen auf Start- und Artikelseite):
  - **Tüten-Etikett scannen**: DigiKey/Mouser (ECIA-DataMatrix), LCSC- und TME-QR liefern Hersteller-Nr., Bestellnummer, Menge und Charge. Bekannte Artikel werden im Scanner direkt erkannt (z. B. zum Einlagern), unbekannte lassen sich mit einem Klick anlegen.
  - **Foto-KI** (optional, eigener Claude-API-Schlüssel): liest Aufdruck, Farbringe, Wert, Toleranz und Gehäuse; Farbringe werden von Lagerwerk selbst nachgerechnet.
  - Ergebnis ist immer ein **Vorschlag**: unsichere Felder sind markiert, Rückfragen lassen sich beantworten („Neu analysieren“), passende Shop-Angebote sind vorausgewählt, vorhandene Artikel und Katalogeinträge werden angezeigt.
- Einstellungen unter *Verwaltung › Info-Provider › Bilderkennung* (Schlüssel wird nicht exportiert).

## v0.3.3 – 2026-09-27

- Lagermöbel: „Schubladen je Reihe“ zählt jetzt Schubladen – eine breite (zusammengefasste) zählt als eine. Vorher entstand bei Reihen mit breiter Schublade rechts ein Loch. Breite Schubladen bekommen LEDs über ihre ganze Breite.

## v0.3.2 – 2026-09-27

- Wandansicht: **„Raster ändern / Vorlage anwenden“** auch für Magazine, die schon ein Raster haben – z. B. um eine falsche Zählrichtung zu korrigieren. LED-Ketten werden danach neu berechnet.
- Lagerort-Seite: „Fächer neu im Raster zuordnen“ bietet ebenfalls die Vorlagen an.

## v0.3.1 – 2026-09-27

- Neue Zählrichtung **blockweise**: je Reihenblock links, Mitte oben/unten, rechts – passend zu Magazinen mit großen Schubladen außen und zwei kleinen in der Mitte.
- Neue Vorlage **„Magazin groß/2×klein/groß + breit unten (17 Fächer)“** (wie Kleinteilemagazin1).
- „Raster festlegen“ in der Wandansicht: mehrere zusammengefasste Schubladen und Zählrichtung wählbar.

## v0.3.0 – 2026-09-27

- **Info-Provider** (Verwaltung › Info-Provider): Reichelt, Pollin, LCSC (ohne Schlüssel) sowie Mouser, DigiKey, TME (kostenloser Schlüssel). Anleitung: [docs/INFO_PROVIDER.md](docs/INFO_PROVIDER.md).
- **Bestellnummern tragen sich selbst ein**: Artikel mit Hersteller-Nr. werden beim Anlegen/Ändern bei allen Shops gesucht; eindeutige Treffer werden mit Preisstaffel, Lagerbestand und Link übernommen.
- **Nachfragen statt raten**: mehrdeutige/unsichere Treffer und fehlende Angaben landen unter *Entscheidungen*.
- **Preise aktualisieren & vergleichen** am Artikel und für die ganze Einkaufsliste (mit Fortschrittsanzeige), optional jede Nacht.
- **Online suchen** am Artikel: Treffer aller Shops mit Kosten für die Nachbestellmenge (inkl. Mindestmenge), Übernahme per Haken inkl. Hersteller-Nr., Hersteller, Datenblatt und Bild.
- **Wandansicht**: importierte Magazine (z. B. Kleinteilemagazin1–10 aus InvenTree) zeigen ihre Fächer sofort; mit „Raster festlegen“ (Vorlage oder eigenes Raster inkl. breiter Schublade) werden sie dauerhaft angeordnet – Namen, Codes und Etiketten bleiben.
- API-Schlüssel werden nie exportiert. Diagnose: `python -m app.cli provider-test <shop> <suche>`.
- Migration 0005 (neue Tabelle `provider_candidates`, neue Spalten an `supplier_parts`/`items`), neue Abhängigkeit beautifulsoup4.

## v0.2.1 – 2026-09-27

- Etiketten: QR-Codes füllen jetzt die vorgesehene Fläche aus (vorher zu klein gedruckt) und haben einen Ruhebereich für sicheres Scannen.
- Neue Vorlage **Brother Endlos 55×16 mm** (Fachname, Möbel, Code) und **eigene Etikettengröße** (Breite × Höhe in mm).
- Gewählte Etikettenvorlage lässt sich als Standard merken.

## v0.2.0 – 2026-09-27

- **Lieferanten & Hersteller** (neuer Menüpunkt):
  - Standard-Shops mit einem Klick (Reichelt, Conrad, Pollin, Mouser, DigiKey, LCSC, TME, Amazon, AliExpress, eBay), Such-Link frei anpassbar
  - Versandkosten, „versandkostenfrei ab“, Mindestbestellwert, Kundennummer
  - Doppelte Einträge zusammenführen (z. B. aus dem Part-DB-Import)
- **Bezugsquellen am Artikel**: mehrere Lieferanten mit Bestellnummer, Verpackungseinheit, Preisstaffel („1: 0,12; 10: 0,08“), Produkt-Link, „bevorzugt“; zeigt das günstigste Angebot für die Nachbestellmenge. Neues Feld **Nachbestellmenge**.
- **Einkaufsliste** (neuer Menüpunkt):
  - Vorschläge aus dem Mindestbestand, Artikel oder freier Text, 🛒 am Artikel und im Scanner
  - automatische Verteilung auf den günstigsten Lieferanten; optional **Versand optimieren** (bündelt, wenn Preis + Versand dadurch sinkt)
  - je Lieferant CSV und „Schnellbestellung kopieren“ (Bestellnr.;Menge), „Bestellt“ markieren
  - **Wareneingang**: bucht die Menge in den üblichen Lagerort ein
  - API: `GET/POST /api/v1/shopping`
- **Wandansicht** (🧱 an Räumen, Wänden, Regalen):
  - Module (Magazine, Regale, Kisten) maßstäblich anordnen, per Drag & Drop mit Einrasten
  - neue Module rechts, links, darüber oder darunter anfügen – aus Vorlage, eigenem Raster oder als Kiste
  - Suche in der Wand: Treffer leuchten auf, „💡 Treffer zeigen“
  - neue Vorlagen mit Maßen (Stapelmagazine, Regale); eigene Vorlagen speichern die Maße mit
- **Umbauen**: Reihen/Böden und Fächer eines Möbels nachträglich ändern; bestehende Fächer behalten Name, Code und Etikett, wegfallende müssen leer sein.
- **LED-Ketten**: ein WLED-Streifen durch mehrere Module; Reihenfolge, Lücken und Verkabelung je Modul einstellen, LED-Nummern werden fortlaufend berechnet (auch nach dem Umbauen).
- Neues Recht **Einkaufsliste & Bestellungen** (Familie: anlegen).
- Handy-App: nach einem Update werden CSS/JS neu geladen (Service-Worker-Cache mit Versionsnummer).
- Datenbank-Migration 0004 (nur neue Tabellen/Spalten), Exportformat bleibt v1 (neue Felder optional).

## v0.1.1 – 2026-09-26

- WLED: Der Blinkcode für Abteile wird jetzt fest ab Anzeigestart getaktet. Vorher konnten einzelne Blinks je nach Zeitpunkt verschluckt werden.
- Getestet mit Python 3.11 bis 3.13; Debian 12 und 13 werden unterstützt.

## v0.1.0 – 2026-09-26

Erste Version.

- **Lagerorte**:
  - Baum mit Drag & Drop und Text-Massenanlage
  - Lagermöbel-Assistent (Raster, breite Schubladen, Abteile) und Vorlagen
  - Raster nachträglich zuordnen
- **Artikel**:
  - Parameter mit Einheiten und Bereichssuche, Varianten
  - Bestandsmodi exakt, grob (Ampel) oder ohne Menge
  - Seriennummern und Chargen, Verleih, Kauf und Garantie
  - Anhänge, Zusammenführen von Artikeln
- **Bauteil-Katalog**: 906 Bauteile mit eigenen Gehäusebildern, Vergleichstypen inkl. DDR.
- **WLED**:
  - Anzeige mit Timeout, Blinkcode bzw. Abschnitt für Abteile, mehrere Treffer in Farben
  - LED-Assistent für ganze Möbel, Feinjustierung im Raster
  - Ruhemodus (Preset oder Bestandsampel)
- **Handy-PWA**: Kamera-Scanner (nativ oder zxing-wasm) mit den Modi Suchen, Einlagern, Entnehmen, Umlagern, Inventur, Schnell-Neuanlage und Etiketten zuordnen.
- **Etiketten**: QR/Code128 für Schubladenfront, Band und A4-Bögen, in Rasterreihenfolge.
- **Benutzer**: Gruppen mit feinen Rechten, Admin-Modus, PIN-Login, API-Tokens, Audit-Log mit Rückgängig.
- **Import**: Part-DB 0.x (SQL-Dump) und InvenTree (SQLite, Tracking-CSV, Medien), idempotent.
- **Betrieb**:
  - Backups mit Rotation, offenes Exportformat v1
  - sichere Migrationen mit Probelauf
  - `install.sh` und `update.sh` mit Rollback
