"""Grunddaten einer frischen Installation (idempotent, additiv)."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Equivalent, ParameterTemplate, Setting
from .locations import ensure_builtin_templates
from .permissions import ensure_default_groups

CATEGORY_STARTERS = {
    "Elektronik Standard": """Elektronik
  Passive Bauelemente
    Widerstände
    Kondensatoren
    Induktivitäten
  Halbleiter
    Dioden
    Transistoren
  ICs
  Mikrocontroller
  Module
  Optoelektronik
  Elektromechanik
    Steckverbinder
    Schalter & Taster
    Relais
""",
    "Elektronik + DDR/Röhren": """Elektronik
  DDR-Bauelemente
    Halbleiter
    ICs
    Passiv
  Röhren
""",
    "Werkstatt/Werkzeug": """Werkzeug
  Handwerkzeug
    Zangen
    Schraubendreher
    Schlüssel
  Elektrowerkzeug
    Akkugeräte
    Löten
  Messen & Prüfen
Befestigung
  Schrauben
  Muttern
  Scheiben
  Dübel
Verbrauchsmaterial
  Kleber
  Klebeband
  Schleifmittel
""",
    "Haushalt/Garten": """Haushalt
  Leuchtmittel
  Batterien & Akkus
  Kabel & Adapter
Garten
  Gartengeräte
  Bewässerung
  Saatgut & Dünger
""",
    "3D-Druck": """3D-Druck
  Filament
    PLA
    PETG
    TPU
  Ersatzteile
    Düsen
    Riemen & Lager
""",
}

PARAM_TEMPLATES = [
    ("Widerstand", "Ω"), ("Toleranz", "%"), ("Leistung", "W"), ("Kapazität", "F"), ("Spannung", "V"), ("Induktivität", "H"),
    ("Strom", "A"), ("Frequenz", "Hz"), ("U_CE max", "V"), ("I_C max", "A"), ("U_DS max", "V"), ("I_D max", "A"),
    ("U_Z", "V"), ("Ausgangsspannung", "V"), ("Ausgangsstrom", "A"), ("Versorgungsspannung", "V"), ("Farbe", ""),
    ("Typ", ""), ("Dielektrikum", ""), ("Polzahl", ""), ("Rastermaß", "mm"), ("Durchmesser", "mm"), ("Material", ""),
    ("Gewicht", "g"), ("Länge", "mm"),
]

DEFAULT_SETTINGS = {
    "guest_enabled": False,
    "pin_login_enabled": True,
    "scan_regex_mappings": [],
    "web_search_urls": [
        {"name": "Google", "url": "https://www.google.com/search?q={query}"},
        {"name": "Google Bilder", "url": "https://www.google.com/search?tbm=isch&q={query}"},
        {"name": "Datenblatt (alldatasheet)", "url": "https://www.alldatasheet.com/view.jsp?Searchword={query}"},
        {"name": "Radiomuseum", "url": "https://www.radiomuseum.org/searchmodels.cfm?q={query}"},
        {"name": "eBay verkauft", "url": "https://www.ebay.de/sch/i.html?_nkw={query}&LH_Sold=1&LH_Complete=1"},
        {"name": "Kleinanzeigen", "url": "https://www.kleinanzeigen.de/s-{query}/k0"},
        {"name": "LCSC", "url": "https://www.lcsc.com/search?q={query}"},
        {"name": "Mouser", "url": "https://www.mouser.de/c/?q={query}"},
    ],
    "default_label_preset": "drawer",
}


def seed(db: Session) -> None:
    ensure_default_groups(db)
    ensure_builtin_templates(db)
    from .walls import ensure_template_sizes

    ensure_template_sizes(db)
    for key, value in DEFAULT_SETTINGS.items():
        if db.get(Setting, key) is None:
            db.add(Setting(key=key, value=value))
    have = {t.name for t in db.scalars(select(ParameterTemplate))}
    for name, unit in PARAM_TEMPLATES:
        if name not in have:
            db.add(ParameterTemplate(name=name, unit=unit, kind="number" if unit else "text"))
    if not db.scalar(select(func.count(Equivalent.id))):
        from .items import seed_equivalents_from_catalog

        seed_equivalents_from_catalog(db)
    db.commit()
