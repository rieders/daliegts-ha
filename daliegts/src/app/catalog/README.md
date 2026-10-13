# Bauteilkatalog (eingebaut)

Vorgefertigte Stammdaten für gängige Hobby-Elektronik: Wer „BC547“ auswählt, bekommt Beschreibung, Kategorie, Gehäuse, Parameter und Bild vorausgefüllt.

## Dateien

- `build_catalog.py`: Generator (nur Python-Standardbibliothek). Die Daten stehen als Code darin, Wertereihen (E12/E6, Elkos, Z-Dioden …) werden per Schleife erzeugt.
- `data/*.json`: generierte Ausgabe, **nicht von Hand bearbeiten**, sondern neu erzeugen.
- `../static/catalog/*.svg`: Gehäuse-Illustrationen (viewBox `0 0 200 150`, transparenter Hintergrund).

Neu erzeugen (aus dem Repo-Wurzelverzeichnis):

```
python3 app/catalog/build_catalog.py
```

Das Skript gibt die Anzahl je Datei aus und bricht mit Exit-Code 1 ab, wenn die Prüfung fehlschlägt. Geprüft wird: Keys eindeutig und gültig, Bilddatei vorhanden, jeder Parameter hat `name` und `value`, Footprint steht in `ALLOWED_FOOTPRINTS`. Die Ausgabe ist deterministisch (sortierte Keys, `ensure_ascii=False`, `indent=1`).

## Format

Eine Datei pro Gruppe: `{"group": "<Titel>", "parts": [ … ]}`. Ein Bauteil:

| Feld | Pflicht | Bedeutung |
|---|---|---|
| `key` | ja | global eindeutiger, stabiler Slug (klein, `a-z0-9._-`). **Darf sich nie ändern**, weil Lagerbestände darauf verweisen. |
| `name` | ja | kurzer Anzeigename, z. B. `BC547B` |
| `description` | ja | eine Zeile auf Deutsch, sachlich |
| `category` | ja | Pfad mit `/`, beginnt mit `Elektronik/` |
| `footprint` | ja | Gehäuse, exakt ein Eintrag aus `ALLOWED_FOOTPRINTS` |
| `manufacturer`, `mpn` | – | leer bei generischen Teilen |
| `unit` | ja | meist `Stk`, bei Meterware `m` |
| `params` | ja | Liste von `{"name", "value", "unit"?}` |
| `tags` | ja | Suchbegriffe, klein geschrieben, sortiert |
| `image` | ja | Dateiname in `static/catalog/` |
| `equivalents` | – | Vergleichstypen; Abweichungen stehen im Text, z. B. `"(ähnlich, Pinbelegung beachten!)"` |
| `datasheet_hint` | – | Suchbegriff für das Datenblatt |

### Parameterregeln

- `value` ist immer ein String mit Dezimalpunkt, die Einheit steht getrennt in `unit`.
- Widerstand, Kapazität, Induktivität und Frequenz stehen in Ingenieursnotation im Wert: `"4.7k"` Ω, `"100n"` F, `"2.2u"` F, `"16M"` Hz (`p n u m k M G`).
- Spannung, Strom und Leistung stehen als einfache Zahl mit `V`, `A`, `mA`, `W` oder `mW`.
- Bereiche schreibt man als `min–max` mit Halbgeviertstrich, z. B. `"2–6"` V.
- Die Parameternamen sind einheitlich: Widerstand, Toleranz, Leistung, Kapazität, Spannung, Dielektrikum, Induktivität, Strom, Frequenz, Typ, U_CE max, I_C max, U_DS max, I_D max, R_DS(on), U_R max, I_F max, U_Z, U_F typ, Ausgangsspannung, Ausgangsstrom, Eingangsspannung max, Farbe, Durchmesser, Polzahl, Rastermaß, Versorgungsspannung, Kanäle, Schnittstelle, Flash, RAM, EEPROM, Takt, Spulenspannung, Kontakte.
- Bei unsicheren Daten lässt man den Parameter lieber weg, statt ihn zu raten.

## Erweitern

1. Die passende `build_*()`-Funktion in `build_catalog.py` suchen und dort `add(gruppe, key, name, beschreibung, kategorie, footprint, bild, params, tags, …)` ergänzen. Eine neue Gruppe braucht einen Eintrag in `GROUP_TITLES`.
2. Für ein neues Gehäuse den Footprint in `ALLOWED_FOOTPRINTS` aufnehmen und, falls nötig, ein SVG in `static/catalog/` anlegen (unter 6 KB, gültiges XML). Sonst `generic.svg` verwenden.
3. Das Skript ausführen, bis die Validierung „OK“ meldet.

Exotische Teile, Werkzeug und Ähnliches werden später in der Anwendung von Hand angelegt und gehören nicht in diesen Katalog.
