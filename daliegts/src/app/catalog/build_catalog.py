#!/usr/bin/env python3
"""Erzeugt den eingebauten Bauteilkatalog von Lagerwerk.

Aufruf (aus dem Repo-Wurzelverzeichnis):
    python3 app/catalog/build_catalog.py

Schreibt je Gruppe eine JSON-Datei nach app/catalog/data/ und prüft am Ende:
eindeutige Keys, vorhandene Bilddateien, vollständige Parameter, erlaubte Footprints.
Nur Python-3.11-Standardbibliothek, deterministische Ausgabe.
"""
from __future__ import annotations

import json
import os
import re
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
IMG_DIR = os.path.normpath(os.path.join(HERE, "..", "static", "catalog"))

ALLOWED_FOOTPRINTS = {
    # SMD passiv
    "0402", "0603", "0805", "1206", "1210", "Tantal Case A", "Tantal Case B", "CD54",
    # SMD aktiv
    "SOT-23", "SOT-223", "SOT-89", "SOD-123", "SOD-80 (MiniMELF)", "SMA (DO-214AC)", "SMB", "SMC",
    "SO-4", "SO-8", "SO-14", "SO-16", "SSOP-20", "SSOP-24", "TSSOP-16", "MSOP-10",
    "TQFP-32", "LQFP-48", "QFN-28", "QFN-56",
    # THT aktiv
    "TO-92", "TO-18", "TO-126", "TO-220", "TO-220-5", "TO-247", "TO-3", "Multiwatt-15",
    "DO-35", "DO-41", "DO-201", "KBP", "KBPC",
    "DIP-4", "DIP-6", "DIP-8", "DIP-14", "DIP-16", "DIP-18", "DIP-20", "DIP-24", "DIP-28", "DIP-40",
    # THT passiv
    "THT axial", "THT radial", "Elko radial", "MLCC THT", "Potentiometer", "Trimmer",
    "HC-49/S", "Uhrenquarz",
    # Opto
    "LED 3mm", "LED 5mm", "LED 5050", "LED-Streifen", "7-Segment",
    # Elektromechanik
    "Stiftleiste 2.54", "Buchsenleiste 2.54", "JST-XH", "JST-PH", "Molex KK", "Dupont", "IDC",
    "Schraubklemme", "USB-C", "Micro-USB", "Mini-USB", "USB-A", "DC-Buchse", "Klinke 3.5", "RJ45",
    "XT60", "Wago", "Sicherung 5x20", "Relais", "SIL-4", "Taster", "Schalter", "Drehgeber",
    # Sonstiges
    "Modul", "Sonstiges",
}

GROUP_TITLES = {
    "widerstaende": "Widerstände & Potentiometer",
    "kondensatoren": "Kondensatoren",
    "induktivitaeten": "Induktivitäten & Ferrite",
    "dioden": "Dioden & Gleichrichter",
    "transistoren": "Transistoren & MOSFETs",
    "spannungsregler": "Spannungsregler & -wandler",
    "ics_logik": "Logik-ICs & Treiber",
    "ics_analog": "Analog-, Schnittstellen- & Peripherie-ICs",
    "mikrocontroller": "Mikrocontroller & Funkmodule (Chips)",
    "module": "Module & Entwicklungsboards",
    "leds": "LEDs",
    "optoelektronik": "Optoelektronik & Anzeigen",
    "steckverbinder": "Steckverbinder & Sockel",
    "schalter_taster": "Schalter & Taster",
    "quarze": "Quarze & Resonatoren",
    "sicherungen": "Sicherungen",
    "relais": "Relais",
    "sensoren": "Sensoren",
    "ddr": "DDR-/RGW-Bauelemente",
}

GROUPS: dict[str, list[dict]] = {g: [] for g in GROUP_TITLES}


def de(x) -> str:
    """Zahl für deutschen Fließtext (Dezimalkomma)."""
    return str(x).replace(".", ",")


def add(group, key, name, description, category, footprint, image, params=(), tags=(),
        manufacturer="", mpn="", equivalents=None, datasheet_hint=None, unit="Stk"):
    plist = []
    for p in params:
        if p is None:
            continue
        d = {"name": p[0], "value": str(p[1])}
        if len(p) > 2 and p[2]:
            d["unit"] = p[2]
        plist.append(d)
    part = {
        "key": key,
        "name": name,
        "description": description,
        "category": category,
        "footprint": footprint,
        "manufacturer": manufacturer,
        "mpn": mpn,
        "unit": unit,
        "params": plist,
        "tags": sorted(set(t.lower() for t in tags if t)),
        "image": image,
    }
    if equivalents:
        part["equivalents"] = list(equivalents)
    if datasheet_hint:
        part["datasheet_hint"] = datasheet_hint
    GROUPS[group].append(part)
    return part


# =============================================================== Hilfen für Wertereihen
E12 = ["1.0", "1.2", "1.5", "1.8", "2.2", "2.7", "3.3", "3.9", "4.7", "5.6", "6.8", "8.2"]
E6 = ["1.0", "1.5", "2.2", "3.3", "4.7", "6.8"]


def series(mantissas, decades=6, top=True):
    vals = [Decimal(m) * (Decimal(10) ** d) for d in range(decades) for m in mantissas]
    if top:
        vals.append(Decimal(10) ** decades)
    return vals


def eng(v: Decimal) -> str:
    """Ohm-Wert -> Ingenieursnotation '4.7k', '1M', '120'."""
    for div, pre in ((Decimal(10) ** 6, "M"), (Decimal(1000), "k"), (Decimal(1), "")):
        if v >= div or div == 1:
            s = format((v / div).normalize(), "f")
            return s + pre
    raise ValueError(v)


def rkm(v: Decimal) -> str:
    """Schlüssel-Notation: 4k7, 1r2, 10r, 1m (=1 MΩ)."""
    s = eng(v)
    pre = s[-1] if s[-1] in "kM" else "r"
    num = s[:-1] if pre != "r" else s
    if "." in num:
        a, b = num.split(".")
        return (a + pre + b).lower()
    return (num + pre).lower()


def ohm_de(v: Decimal) -> str:
    s = eng(v)
    if s[-1] in "kM":
        return f"{de(s[:-1])} {s[-1]}Ω"
    return f"{de(s)} Ω"


COLORS = ["schwarz", "braun", "rot", "orange", "gelb", "grün", "blau", "violett", "grau", "weiß"]
MULT = {-2: "silber", -1: "gold", **{i: COLORS[i] for i in range(10)}}


def color_code_5(v: Decimal) -> str:
    e = v.adjusted() - 2
    sig = int((v / (Decimal(10) ** e)).to_integral_value())
    digits = [COLORS[int(c)] for c in f"{sig:03d}"]
    return "-".join(digits + [MULT[e], "braun"])


def vkey(val: str) -> str:
    """'2.2n' -> '2n2', '4.7u' -> '4u7', '100n' -> '100n' (RKM-Notation für Keys)."""
    if "." in val:
        a, b = val[:-1].split(".")
        return f"{a}{val[-1]}{b}"
    return val


def cap_de(val: str) -> str:
    unit = {"p": "pF", "n": "nF", "u": "µF", "m": "mF"}[val[-1]]
    return f"{de(val[:-1])} {unit}"


# =============================================================== Widerstände
def build_resistors():
    cat_smd = "Elektronik/Passiv/Widerstände/SMD"
    cat_tht = "Elektronik/Passiv/Widerstände/THT"
    smd = [("0603", "0.1", "1", E12, "1 A"), ("0805", "0.125", "1", E12, "2 A"), ("1206", "0.25", "1", E6, "2 A")]
    for size, power, tol, ser, jumper_i in smd:
        add("widerstaende", f"res-{size}-0r", f"0 Ω ({size})",
            f"SMD-Nullohm-Brücke, Bauform {size}", cat_smd, size, "smd-chip-resistor.svg",
            [("Widerstand", "0", "Ω"), ("Strom", jumper_i.split()[0], "A")],
            ["widerstand", "smd", size, "0r", "brücke", "jumper"])
        for v in series(ser):
            k = rkm(v)
            add("widerstaende", f"res-{size}-{k}", f"{ohm_de(v)} ({size})",
                f"SMD-Dickschichtwiderstand {ohm_de(v)}, {tol} %, {de(power)} W, Bauform {size}",
                cat_smd, size, "smd-chip-resistor.svg",
                [("Widerstand", eng(v), "Ω"), ("Toleranz", tol, "%"), ("Leistung", power, "W")],
                ["widerstand", "smd", size, k, eng(v).lower()])
    for v in series(E12):
        k = rkm(v)
        add("widerstaende", f"res-tht-{k}", f"{ohm_de(v)} ¼ W",
            f"Metallschichtwiderstand {ohm_de(v)}, 1 %, 0,25 W, axial (Farbcode {color_code_5(v)})",
            cat_tht, "THT axial", "resistor-tht.svg",
            [("Widerstand", eng(v), "Ω"), ("Toleranz", "1", "%"), ("Leistung", "0.25", "W"),
             ("Farbcode", color_code_5(v))],
            ["widerstand", "tht", "metallschicht", k, eng(v).lower()])

    cat_pot = "Elektronik/Passiv/Potentiometer"
    for val, kind, code in (("1k", "linear", "B1K"), ("10k", "linear", "B10K"), ("100k", "linear", "B100K"),
                            ("10k", "logarithmisch", "A10K"), ("100k", "logarithmisch", "A100K")):
        add("widerstaende", f"pot-16mm-{code.lower()}", f"Poti {val}Ω {kind[:3]}. ({code})",
            f"Dreh-Potentiometer {de(val)}Ω {kind}, Ø 16 mm, 3 Lötpins, Achse 6 mm (z. B. Typ WH148)",
            cat_pot, "Potentiometer", "potentiometer.svg",
            [("Widerstand", val, "Ω"), ("Typ", kind)],
            ["potentiometer", "poti", code, val, kind])
    cat_trim = "Elektronik/Passiv/Trimmer"
    for val, code in (("1k", "102"), ("10k", "103"), ("100k", "104")):
        add("widerstaende", f"trim-3296w-{val}", f"3296W {val}Ω",
            f"Präzisions-Spindeltrimmer {de(val)}Ω, 25 Umdrehungen, 0,5 W, stehend (Aufdruck {code})",
            cat_trim, "Trimmer", "trimmer.svg",
            [("Widerstand", val, "Ω"), ("Typ", "Spindeltrimmer 25 Gänge"), ("Leistung", "0.5", "W")],
            ["trimmer", "3296w", "spindeltrimmer", val, code], "Bourns", f"3296W-1-{code}LF",
            datasheet_hint="Bourns 3296")
        add("widerstaende", f"trim-6mm-{val}", f"Trimmer 6 mm {val}Ω",
            f"Einstelltrimmer {de(val)}Ω, einfach, 6 mm, stehend (Aufdruck {code})",
            cat_trim, "Trimmer", "trimmer.svg",
            [("Widerstand", val, "Ω"), ("Typ", "Einstelltrimmer 1 Gang")],
            ["trimmer", "einstellregler", val, code])


# =============================================================== Kondensatoren
def build_caps():
    cat_mlcc = "Elektronik/Passiv/Kondensatoren/Keramik SMD"
    mlcc = {
        "0603": [("10p", "50", "C0G"), ("15p", "50", "C0G"), ("18p", "50", "C0G"), ("22p", "50", "C0G"),
                 ("33p", "50", "C0G"), ("47p", "50", "C0G"), ("100p", "50", "C0G"), ("220p", "50", "C0G"),
                 ("470p", "50", "C0G"), ("1n", "50", "X7R"), ("2.2n", "50", "X7R"), ("4.7n", "50", "X7R"),
                 ("10n", "50", "X7R"), ("22n", "50", "X7R"), ("47n", "50", "X7R"), ("100n", "50", "X7R"),
                 ("220n", "25", "X7R"), ("470n", "16", "X7R"), ("1u", "25", "X5R"), ("2.2u", "10", "X5R"),
                 ("4.7u", "10", "X5R"), ("10u", "6.3", "X5R")],
        "0805": [("10p", "50", "C0G"), ("22p", "50", "C0G"), ("33p", "50", "C0G"), ("47p", "50", "C0G"),
                 ("100p", "50", "C0G"), ("220p", "50", "C0G"), ("470p", "50", "C0G"), ("1n", "50", "X7R"),
                 ("2.2n", "50", "X7R"), ("4.7n", "50", "X7R"), ("10n", "50", "X7R"), ("22n", "50", "X7R"),
                 ("47n", "50", "X7R"), ("100n", "50", "X7R"), ("220n", "50", "X7R"), ("470n", "25", "X7R"),
                 ("1u", "25", "X7R"), ("2.2u", "25", "X5R"), ("4.7u", "25", "X5R"), ("10u", "16", "X5R"),
                 ("22u", "6.3", "X5R")],
        "1206": [("100n", "50", "X7R"), ("1u", "50", "X7R"), ("10u", "25", "X5R"), ("22u", "16", "X5R")],
    }
    for size, rows in mlcc.items():
        for val, volt, diel in rows:
            add("kondensatoren", f"cap-{size}-{vkey(val)}", f"{cap_de(val)} {volt} V ({size})",
                f"Keramik-Vielschichtkondensator (MLCC) {cap_de(val)}, {de(volt)} V, {diel}, Bauform {size}",
                cat_mlcc, size, "smd-chip-capacitor.svg",
                [("Kapazität", val, "F"), ("Spannung", volt, "V"), ("Dielektrikum", diel)],
                ["kondensator", "mlcc", "keramik", "smd", size, val, diel])
    cat_ktht = "Elektronik/Passiv/Kondensatoren/Keramik THT"
    for val, diel in (("10p", "C0G"), ("15p", "C0G"), ("22p", "C0G"), ("27p", "C0G"), ("33p", "C0G"),
                      ("100p", "C0G"), ("1n", "X7R"), ("10n", "X7R"), ("100n", "X7R"), ("1u", "X7R")):
        add("kondensatoren", f"cap-tht-{val}", f"{cap_de(val)} 50 V MLCC RM2,54",
            f"Keramik-Vielschichtkondensator {cap_de(val)}, 50 V, {diel}, bedrahtet, Rastermaß 2,54 mm",
            cat_ktht, "MLCC THT", "mlcc-tht.svg",
            [("Kapazität", val, "F"), ("Spannung", "50", "V"), ("Dielektrikum", diel), ("Rastermaß", "2.54", "mm")],
            ["kondensator", "keramik", "mlcc", "tht", val, diel])
    cat_elko = "Elektronik/Passiv/Kondensatoren/Elektrolyt"
    elkos = [("1u", "50"), ("2.2u", "50"), ("4.7u", "50"), ("10u", "25"), ("10u", "50"), ("22u", "25"),
             ("22u", "50"), ("47u", "16"), ("47u", "25"), ("47u", "50"), ("100u", "16"), ("100u", "25"),
             ("100u", "35"), ("100u", "63"), ("220u", "16"), ("220u", "25"), ("220u", "35"), ("470u", "16"),
             ("470u", "25"), ("470u", "35"), ("1000u", "16"), ("1000u", "25"), ("1000u", "35"),
             ("2200u", "16"), ("2200u", "25"), ("4700u", "16"), ("4700u", "25")]
    for val, volt in elkos:
        add("kondensatoren", f"elko-{vkey(val)}-{volt}v", f"Elko {cap_de(val)} {volt} V",
            f"Aluminium-Elektrolytkondensator {cap_de(val)}, {volt} V, radial (gepolt!)",
            cat_elko, "Elko radial", "elko.svg",
            [("Kapazität", val, "F"), ("Spannung", volt, "V"), ("Typ", "Aluminium-Elko")],
            ["kondensator", "elko", "elektrolyt", "radial", val, f"{volt}v"])
    cat_tant = "Elektronik/Passiv/Kondensatoren/Tantal"
    for val, volt in (("1u", "35"), ("4.7u", "25"), ("10u", "16"), ("10u", "25"), ("22u", "16"), ("47u", "10")):
        add("kondensatoren", f"tant-tht-{vkey(val)}-{volt}v", f"Tantal {cap_de(val)} {volt} V",
            f"Tantal-Perlkondensator {cap_de(val)}, {volt} V, radial bedrahtet (gepolt!)",
            cat_tant, "THT radial", "tantal.svg",
            [("Kapazität", val, "F"), ("Spannung", volt, "V"), ("Typ", "Tantal")],
            ["kondensator", "tantal", "tht", val, f"{volt}v"])
    for val, volt, case in (("10u", "16", "A"), ("22u", "16", "B"), ("47u", "10", "B")):
        add("kondensatoren", f"tant-smd-{case.lower()}-{val}-{volt}v", f"Tantal {cap_de(val)} {volt} V Case {case}",
            f"SMD-Tantalkondensator {cap_de(val)}, {volt} V, Case {case} ({'3216' if case == 'A' else '3528'}), gepolt",
            cat_tant, f"Tantal Case {case}", "tantal-smd.svg",
            [("Kapazität", val, "F"), ("Spannung", volt, "V"), ("Typ", "Tantal")],
            ["kondensator", "tantal", "smd", f"case-{case.lower()}", val])
    cat_film = "Elektronik/Passiv/Kondensatoren/Folie"
    for val in ("10n", "22n", "47n", "100n", "220n", "470n", "1u"):
        add("kondensatoren", f"film-mks-{val}-63v", f"MKS {cap_de(val)} 63 V",
            f"Folienkondensator (Polyester, MKS/MKT) {cap_de(val)}, 63 V, Rastermaß 5 mm",
            cat_film, "THT radial", "film-cap.svg",
            [("Kapazität", val, "F"), ("Spannung", "63", "V"), ("Dielektrikum", "PET"), ("Rastermaß", "5", "mm")],
            ["kondensator", "folie", "mks", "mkt", val])


# =============================================================== Induktivitäten
def build_inductors():
    cat = "Elektronik/Passiv/Induktivitäten"
    for val in ("10u", "22u", "47u", "100u", "220u", "470u", "1m"):
        disp = cap_de(val).replace("F", "H")
        add("induktivitaeten", f"ind-axial-{val}", f"Drossel {disp} axial",
            f"Festinduktivität {disp}, axial bedrahtet (Bauform wie Widerstand, Farbcode)",
            f"{cat}/Drosseln THT", "THT axial", "inductor-axial.svg",
            [("Induktivität", val, "H")], ["induktivität", "drossel", "spule", "axial", val])
    for val in ("10u", "22u", "47u", "100u"):
        disp = cap_de(val).replace("F", "H")
        add("induktivitaeten", f"ind-cd54-{val}", f"CD54 {disp}",
            f"SMD-Speicherdrossel {disp}, Bauform CD54 (ca. 5,8 × 5,2 mm), für Schaltregler",
            f"{cat}/Speicherdrosseln SMD", "CD54", "inductor-smd.svg",
            [("Induktivität", val, "H")], ["induktivität", "speicherdrossel", "smd", "cd54", val])
    add("induktivitaeten", "ferrite-0805-600r", "Ferritperle 600 Ω (0805)",
        "SMD-Ferritperle 600 Ω @ 100 MHz, Bauform 0805, zur HF-Entstörung",
        f"{cat}/Ferrite", "0805", "smd-chip-generic.svg",
        [("Impedanz", "600", "Ω"), ("Frequenz", "100M", "Hz")],
        ["ferrit", "ferritperle", "emv", "0805"])


# =============================================================== Dioden
def build_diodes():
    g = "dioden"
    c = "Elektronik/Halbleiter/Dioden"
    add(g, "1n4148", "1N4148", "Kleinsignal-Schaltdiode 100 V / 200 mA, schnell (4 ns), DO-35 Glas",
        f"{c}/Universal", "DO-35", "do-35.svg",
        [("Typ", "Si-Schaltdiode"), ("U_R max", "100", "V"), ("I_F max", "200", "mA"), ("t_rr", "4", "ns")],
        ["diode", "schaltdiode", "kleinsignal", "do-35"], equivalents=["1N914", "LL4148 (SMD)"], datasheet_hint="1N4148")
    add(g, "ll4148", "LL4148", "Kleinsignal-Schaltdiode 100 V / 200 mA, SMD-Version der 1N4148, MiniMELF",
        f"{c}/Universal", "SOD-80 (MiniMELF)", "minimelf.svg",
        [("Typ", "Si-Schaltdiode"), ("U_R max", "100", "V"), ("I_F max", "200", "mA"), ("t_rr", "4", "ns")],
        ["diode", "schaltdiode", "smd", "minimelf"], equivalents=["1N4148 (THT)"], datasheet_hint="LL4148")
    add(g, "1n4148w", "1N4148W", "Kleinsignal-Schaltdiode 100 V, SMD-Version der 1N4148, SOD-123",
        f"{c}/Universal", "SOD-123", "sod-123.svg",
        [("Typ", "Si-Schaltdiode"), ("U_R max", "100", "V")],
        ["diode", "schaltdiode", "smd", "sod-123"], equivalents=["1N4148 (THT)"], datasheet_hint="1N4148W")
    for i, v in enumerate((50, 100, 200, 400, 600, 800, 1000), start=1):
        add(g, f"1n400{i}", f"1N400{i}", f"Gleichrichterdiode {v} V / 1 A, DO-41",
            f"{c}/Gleichrichter", "DO-41", "do-41.svg",
            [("Typ", "Si-Gleichrichter"), ("U_R max", v, "V"), ("I_F max", "1", "A")],
            ["diode", "gleichrichter", "do-41", "1n400x"],
            equivalents=[f"1N400{j} (höhere Sperrspannung)" for j in range(i + 1, 8)] + ["M7 (SMD, 1000 V)"],
            datasheet_hint="1N4001-1N4007")
    add(g, "m7", "M7 (SMD 1N4007)", "Gleichrichterdiode 1000 V / 1 A, SMD-Version der 1N4007, SMA",
        f"{c}/Gleichrichter", "SMA (DO-214AC)", "sma.svg",
        [("Typ", "Si-Gleichrichter"), ("U_R max", "1000", "V"), ("I_F max", "1", "A")],
        ["diode", "gleichrichter", "smd", "sma"], equivalents=["1N4007 (THT)"], datasheet_hint="M7 SMA diode")
    for n, v in (("1n5404", 400), ("1n5408", 1000)):
        add(g, n, n.upper(), f"Gleichrichterdiode {v} V / 3 A, DO-201AD",
            f"{c}/Gleichrichter", "DO-201", "do-201.svg",
            [("Typ", "Si-Gleichrichter"), ("U_R max", v, "V"), ("I_F max", "3", "A")],
            ["diode", "gleichrichter", "do-201", "3a"], datasheet_hint="1N5400-1N5408")
    add(g, "uf4007", "UF4007", "Ultraschnelle Gleichrichterdiode 1000 V / 1 A, t_rr 75 ns, DO-41",
        f"{c}/Gleichrichter", "DO-41", "do-41.svg",
        [("Typ", "Ultrafast"), ("U_R max", "1000", "V"), ("I_F max", "1", "A"), ("t_rr", "75", "ns")],
        ["diode", "ultrafast", "schnell", "do-41"], datasheet_hint="UF4007")
    for n, v in (("1n5817", 20), ("1n5818", 30), ("1n5819", 40)):
        add(g, n, n.upper(), f"Schottky-Diode {v} V / 1 A, DO-41",
            f"{c}/Schottky", "DO-41", "do-41.svg",
            [("Typ", "Schottky"), ("U_R max", v, "V"), ("I_F max", "1", "A")],
            ["diode", "schottky", "do-41"], datasheet_hint="1N5817-1N5819")
    add(g, "1n5822", "1N5822", "Schottky-Diode 40 V / 3 A, DO-201AD",
        f"{c}/Schottky", "DO-201", "do-201.svg",
        [("Typ", "Schottky"), ("U_R max", "40", "V"), ("I_F max", "3", "A")],
        ["diode", "schottky", "do-201", "3a"], datasheet_hint="1N5822")
    add(g, "ss14", "SS14", "SMD-Schottky-Diode 40 V / 1 A, SMA",
        f"{c}/Schottky", "SMA (DO-214AC)", "sma.svg",
        [("Typ", "Schottky"), ("U_R max", "40", "V"), ("I_F max", "1", "A")],
        ["diode", "schottky", "smd", "sma"], equivalents=["1N5819 (THT)"], datasheet_hint="SS14")
    add(g, "ss34", "SS34", "SMD-Schottky-Diode 40 V / 3 A; Gehäuse je nach Hersteller SMA oder SMC – prüfen",
        f"{c}/Schottky", "SMA (DO-214AC)", "sma.svg",
        [("Typ", "Schottky"), ("U_R max", "40", "V"), ("I_F max", "3", "A")],
        ["diode", "schottky", "smd", "sma", "3a"], equivalents=["1N5822 (THT)"], datasheet_hint="SS34")
    for n, v in (("bat85", 30), ("bat43", 30)):
        add(g, n, n.upper(), f"Kleinsignal-Schottky-Diode {v} V / 200 mA, DO-35 Glas",
            f"{c}/Schottky", "DO-35", "do-35.svg",
            [("Typ", "Schottky"), ("U_R max", v, "V"), ("I_F max", "200", "mA")],
            ["diode", "schottky", "kleinsignal", "do-35"], datasheet_hint=n.upper())
    for sfx, cfg in (("", "Einzeldiode"), ("a", "Doppeldiode, gemeinsame Anode"),
                     ("c", "Doppeldiode, gemeinsame Kathode"), ("s", "Doppeldiode in Serie")):
        add(g, f"bat54{sfx}", f"BAT54{sfx.upper()}", f"Schottky-Kleinsignaldiode 30 V / 200 mA, {cfg}, SOT-23",
            f"{c}/Schottky", "SOT-23", "sot-23.svg",
            [("Typ", "Schottky"), ("Konfiguration", cfg), ("U_R max", "30", "V"), ("I_F max", "200", "mA")],
            ["diode", "schottky", "smd", "sot-23", "bat54"], datasheet_hint="BAT54")

    zc = f"{c}/Z-Dioden"
    zvolts = ["2.7", "3.3", "3.9", "4.7", "5.1", "5.6", "6.2", "6.8", "7.5", "8.2", "9.1", "10", "12", "15", "18", "24"]
    for v in zvolts:
        code = v.replace(".", "V")
        disp = de(v)
        add(g, f"bzx55c{code.lower()}", f"BZX55C{code}", f"Z-Diode {disp} V, 0,5 W, DO-35 Glas",
            zc, "DO-35", "do-35.svg",
            [("Typ", "Z-Diode"), ("U_Z", v, "V"), ("Leistung", "0.5", "W")],
            ["diode", "zener", "z-diode", "do-35", f"{v}v"],
            equivalents=[f"ZMM{code} (SMD MiniMELF)"], datasheet_hint="BZX55")
        add(g, f"zmm{code.lower()}", f"ZMM{code}", f"Z-Diode {disp} V, 0,5 W, SMD MiniMELF",
            zc, "SOD-80 (MiniMELF)", "minimelf.svg",
            [("Typ", "Z-Diode"), ("U_Z", v, "V"), ("Leistung", "0.5", "W")],
            ["diode", "zener", "z-diode", "smd", "minimelf", f"{v}v"],
            equivalents=[f"BZX55C{code} (THT)"], datasheet_hint="ZMM55")
    z1w = {"3.3": 4728, "3.9": 4730, "4.7": 4732, "5.1": 4733, "5.6": 4734, "6.2": 4735, "6.8": 4736,
           "7.5": 4737, "8.2": 4738, "9.1": 4739, "10": 4740, "12": 4742, "15": 4744, "18": 4746, "24": 4749}
    for v, n in z1w.items():
        add(g, f"1n{n}a", f"1N{n}A", f"Z-Diode {de(v)} V, 1 W, DO-41",
            zc, "DO-41", "do-41.svg",
            [("Typ", "Z-Diode"), ("U_Z", v, "V"), ("Leistung", "1", "W")],
            ["diode", "zener", "z-diode", "do-41", "1w", f"{v}v"], datasheet_hint="1N4728A-1N4764A")

    bc = f"{c}/Brückengleichrichter"
    add(g, "mb6s", "MB6S", "SMD-Brückengleichrichter 600 V / 0,5 A, Gehäuse MBS (SOIC-4)",
        bc, "SO-4", "bridge-smd.svg",
        [("U_R max", "600", "V"), ("I_F max", "0.5", "A")], ["brückengleichrichter", "gleichrichter", "smd"],
        datasheet_hint="MB6S")
    add(g, "db107", "DB107", "Brückengleichrichter 1000 V / 1 A, DIP-4 (DB-1)",
        bc, "DIP-4", "dip-4.svg",
        [("U_R max", "1000", "V"), ("I_F max", "1", "A")], ["brückengleichrichter", "gleichrichter", "dip"],
        datasheet_hint="DB107")
    add(g, "kbp307", "KBP307", "Brückengleichrichter 1000 V / 3 A, flach, Reihenanschluss (KBP)",
        bc, "KBP", "bridge-rectifier.svg",
        [("U_R max", "1000", "V"), ("I_F max", "3", "A")], ["brückengleichrichter", "gleichrichter", "kbp"],
        datasheet_hint="KBP307")
    add(g, "b40c1500", "B40C1500", "Brückengleichrichter für 40 V~ Anschlussspannung, 1,5 A (mit C-Last), Bauform prüfen",
        bc, "KBP", "bridge-rectifier.svg",
        [("Eingangsspannung max", "40", "V"), ("I_F max", "1.5", "A")], ["brückengleichrichter", "gleichrichter", "b40"],
        datasheet_hint="B40C1500")
    add(g, "kbpc3510", "KBPC3510", "Hochstrom-Brückengleichrichter 1000 V / 35 A, Metallgehäuse mit Flachsteckern, auf Kühlkörper montieren",
        bc, "KBPC", "bridge-rectifier.svg",
        [("U_R max", "1000", "V"), ("I_F max", "35", "A")], ["brückengleichrichter", "gleichrichter", "hochstrom"],
        datasheet_hint="KBPC3510")


# =============================================================== Transistoren
def build_transistors():
    g = "transistoren"
    c = "Elektronik/Halbleiter/Transistoren"

    def bjt(key, name, pol, vce, ic, ic_unit, fp, img, desc_extra="", eq=None, dar=False, ptot=None):
        typ = ("Darlington " if dar else "") + pol
        sub = ("Darlington " if dar else "") + pol
        ic_txt = f"{de(ic)} {ic_unit}"
        desc = (f"{pol}-{'Darlington-' if dar else ''}{'Leistungstransistor' if ic_unit == 'A' and float(ic) >= 1 else 'Kleinsignaltransistor'} "
                f"{vce} V / {ic_txt}, {fp}{desc_extra}")
        params = [("Typ", typ), ("U_CE max", vce, "V"), ("I_C max", ic, ic_unit)]
        if ptot:
            params.append(("Leistung", ptot[0], ptot[1]))
        add(g, key, name, desc, f"{c}/{sub}", fp, img, params,
            ["transistor", "bipolar", pol.lower(), fp.lower()] + (["darlington"] if dar else []),
            equivalents=eq, datasheet_hint=key.upper())

    bjt("bc547", "BC547B", "NPN", 45, 100, "mA", "TO-92", "to-92.svg", eq=["BC548", "BC549", "BC847 (SMD)", "2N3904 (ähnlich, Pinbelegung beachten!)"], ptot=("500", "mW"))
    bjt("bc548", "BC548B", "NPN", 30, 100, "mA", "TO-92", "to-92.svg", eq=["BC547", "BC549", "BC848 (SMD)"], ptot=("500", "mW"))
    bjt("bc549", "BC549C", "NPN", 30, 100, "mA", "TO-92", "to-92.svg", ", rauscharm", eq=["BC550", "BC548"], ptot=("500", "mW"))
    bjt("bc546", "BC546B", "NPN", 65, 100, "mA", "TO-92", "to-92.svg", eq=["BC547 (geringere Spannung)"], ptot=("500", "mW"))
    bjt("bc557", "BC557B", "PNP", 45, 100, "mA", "TO-92", "to-92.svg", eq=["BC558", "BC559", "BC857 (SMD)", "2N3906 (ähnlich, Pinbelegung beachten!)"], ptot=("500", "mW"))
    bjt("bc558", "BC558B", "PNP", 30, 100, "mA", "TO-92", "to-92.svg", eq=["BC557", "BC559"], ptot=("500", "mW"))
    bjt("bc556", "BC556B", "PNP", 65, 100, "mA", "TO-92", "to-92.svg", eq=["BC557 (geringere Spannung)"], ptot=("500", "mW"))
    bjt("bc337", "BC337-40", "NPN", 45, 800, "mA", "TO-92", "to-92.svg", eq=["BC338", "BC817 (SMD)"], ptot=("625", "mW"))
    bjt("bc327", "BC327-40", "PNP", 45, 800, "mA", "TO-92", "to-92.svg", eq=["BC328", "BC807 (SMD)"], ptot=("625", "mW"))
    bjt("2n2222a", "2N2222A", "NPN", 40, 600, "mA", "TO-92", "to-92.svg", " (Kunststoff PN2222A; Original im Metallgehäuse TO-18)", eq=["PN2222A", "MMBT2222A (SMD)"])
    bjt("2n3904", "2N3904", "NPN", 40, 200, "mA", "TO-92", "to-92.svg", eq=["MMBT3904 (SMD)", "BC547 (ähnlich, Pinbelegung beachten!)"], ptot=("625", "mW"))
    bjt("2n3906", "2N3906", "PNP", 40, 200, "mA", "TO-92", "to-92.svg", eq=["MMBT3906 (SMD)", "BC557 (ähnlich, Pinbelegung beachten!)"], ptot=("625", "mW"))
    bjt("bd139", "BD139", "NPN", 80, "1.5", "A", "TO-126", "to-126.svg", eq=["BD135/BD137 (geringere Spannung)"], ptot=("12.5", "W"))
    bjt("bd140", "BD140", "PNP", 80, "1.5", "A", "TO-126", "to-126.svg", eq=["BD136/BD138 (geringere Spannung)"], ptot=("12.5", "W"))
    bjt("tip31c", "TIP31C", "NPN", 100, 3, "A", "TO-220", "to-220.svg", ptot=("40", "W"))
    bjt("tip32c", "TIP32C", "PNP", 100, 3, "A", "TO-220", "to-220.svg", ptot=("40", "W"))
    bjt("tip41c", "TIP41C", "NPN", 100, 6, "A", "TO-220", "to-220.svg", ptot=("65", "W"))
    bjt("tip42c", "TIP42C", "PNP", 100, 6, "A", "TO-220", "to-220.svg", ptot=("65", "W"))
    bjt("tip120", "TIP120", "NPN", 60, 5, "A", "TO-220", "to-220.svg", dar=True, eq=["TIP121", "TIP122 (100 V)"], ptot=("65", "W"))
    bjt("tip122", "TIP122", "NPN", 100, 5, "A", "TO-220", "to-220.svg", dar=True, eq=["TIP120 (60 V)"], ptot=("65", "W"))
    bjt("tip125", "TIP125", "PNP", 60, 5, "A", "TO-220", "to-220.svg", dar=True, eq=["TIP127 (100 V)"], ptot=("65", "W"))
    bjt("tip127", "TIP127", "PNP", 100, 5, "A", "TO-220", "to-220.svg", dar=True, eq=["TIP125 (60 V)"], ptot=("65", "W"))
    bjt("2n3055", "2N3055", "NPN", 60, 15, "A", "TO-3", "to-3.svg", eq=["MJ2955 (PNP-Komplement)"], ptot=("115", "W"))
    bjt("mj2955", "MJ2955", "PNP", 60, 15, "A", "TO-3", "to-3.svg", eq=["2N3055 (NPN-Komplement)"], ptot=("115", "W"))
    bjt("bc817", "BC817-40", "NPN", 45, 500, "mA", "SOT-23", "sot-23.svg", eq=["BC337 (THT)"])
    bjt("bc807", "BC807-40", "PNP", 45, 500, "mA", "SOT-23", "sot-23.svg", eq=["BC327 (THT)"])
    bjt("bc847", "BC847B", "NPN", 45, 100, "mA", "SOT-23", "sot-23.svg", eq=["BC547 (THT)"])
    bjt("bc857", "BC857B", "PNP", 45, 100, "mA", "SOT-23", "sot-23.svg", eq=["BC557 (THT)"])
    bjt("mmbt3904", "MMBT3904", "NPN", 40, 200, "mA", "SOT-23", "sot-23.svg", eq=["2N3904 (THT)"])
    bjt("mmbt3906", "MMBT3906", "PNP", 40, 200, "mA", "SOT-23", "sot-23.svg", eq=["2N3906 (THT)"])
    bjt("mmbt2222a", "MMBT2222A", "NPN", 40, 600, "mA", "SOT-23", "sot-23.svg", eq=["2N2222A (THT)"])

    def fet(key, name, ch, vds, idv, idu, fp, img, rds=None, extra="", eq=None):
        sub = f"MOSFET {ch}-Kanal"
        rtxt = f", R_DS(on) {de(rds[0])} {rds[1]}" if rds else ""
        desc = f"{ch}-Kanal-MOSFET {vds} V / {de(idv)} {idu}{rtxt}, {fp}{extra}"
        params = [("Typ", f"{ch}-Kanal MOSFET"), ("U_DS max", vds, "V"), ("I_D max", idv, idu)]
        if rds:
            params.append(("R_DS(on)", rds[0], rds[1]))
        add(g, key, name, desc, f"{c}/{sub}", fp, img, params,
            ["transistor", "mosfet", f"{ch.lower()}-kanal", fp.lower()] + (["logic-level"] if "Logic" in extra else []),
            equivalents=eq, datasheet_hint=name)

    fet("2n7000", "2N7000", "N", 60, 200, "mA", "TO-92", "to-92.svg", ("5", "Ω"), eq=["BS170 (Pinbelegung beachten!)", "2N7002 (SMD)"])
    fet("bs170", "BS170", "N", 60, 500, "mA", "TO-92", "to-92.svg", ("5", "Ω"), eq=["2N7000 (Pinbelegung beachten!)"])
    fet("2n7002", "2N7002", "N", 60, 115, "mA", "SOT-23", "sot-23.svg", eq=["2N7000 (THT)"])
    fet("bss138", "BSS138", "N", 50, 220, "mA", "SOT-23", "sot-23.svg", extra=", Logic-Level, oft für Pegelwandler")
    fet("ao3400", "AO3400A", "N", 30, "5.7", "A", "SOT-23", "sot-23.svg", extra=", Logic-Level")
    fet("ao3401", "AO3401A", "P", 30, 4, "A", "SOT-23", "sot-23.svg", extra=", Logic-Level")
    fet("irlml2502", "IRLML2502", "N", 20, "4.2", "A", "SOT-23", "sot-23.svg", ("45", "mΩ"), extra=" (bei U_GS 4,5 V), Logic-Level")
    fet("irlml6402", "IRLML6402", "P", 20, "3.7", "A", "SOT-23", "sot-23.svg", ("65", "mΩ"), extra=" (bei U_GS −4,5 V), Logic-Level")
    fet("irlz44n", "IRLZ44N", "N", 55, 47, "A", "TO-220", "to-220.svg", ("22", "mΩ"), extra=", Logic-Level", eq=["IRFZ44N (kein Logic-Level)"])
    fet("irfz44n", "IRFZ44N", "N", 55, 49, "A", "TO-220", "to-220.svg", ("17.5", "mΩ"), eq=["IRLZ44N (Logic-Level)"])
    fet("irf540n", "IRF540N", "N", 100, 33, "A", "TO-220", "to-220.svg", ("44", "mΩ"))
    fet("irf520", "IRF520", "N", 100, "9.2", "A", "TO-220", "to-220.svg", ("0.27", "Ω"), eq=["IRF520N (ähnlich, 9,7 A)"])
    fet("irf3205", "IRF3205", "N", 55, 110, "A", "TO-220", "to-220.svg", ("8", "mΩ"))
    fet("irf9540n", "IRF9540N", "P", 100, 23, "A", "TO-220", "to-220.svg", ("117", "mΩ"))
    fet("irf4905", "IRF4905", "P", 55, 74, "A", "TO-220", "to-220.svg", ("20", "mΩ"))


# =============================================================== Spannungsregler
def build_regulators():
    g = "spannungsregler"
    c = "Elektronik/ICs/Spannungsregler"
    for v, vin in ((5, 35), (6, 35), (8, 35), (9, 35), (12, 35), (15, 35), (24, 40)):
        add(g, f"78{v:02d}", f"78{v:02d}", f"Festspannungsregler +{v} V / 1 A, TO-220 (Pinbelegung E-M-A)",
            f"{c}/Linear fest", "TO-220", "to-220.svg",
            [("Ausgangsspannung", v, "V"), ("Ausgangsstrom", "1", "A"), ("Eingangsspannung max", vin, "V")],
            ["spannungsregler", "linearregler", "78xx", "to-220", f"{v}v"],
            equivalents=[f"L78{v:02d}CV", f"LM78{v:02d}", f"MC78{v:02d}"], datasheet_hint=f"L78{v:02d}")
    for v in (5, 12):
        add(g, f"79{v:02d}", f"79{v:02d}", f"Negativer Festspannungsregler −{v} V / 1 A, TO-220 (Pinbelegung M-E-A, abweichend von 78xx!)",
            f"{c}/Linear fest", "TO-220", "to-220.svg",
            [("Ausgangsspannung", -v, "V"), ("Ausgangsstrom", "1", "A"), ("Eingangsspannung max", "-35", "V")],
            ["spannungsregler", "linearregler", "79xx", "negativ", "to-220"],
            equivalents=[f"L79{v:02d}CV", f"MC79{v:02d}"], datasheet_hint=f"L79{v:02d}")
    add(g, "78l05", "78L05", "Festspannungsregler +5 V / 100 mA, TO-92",
        f"{c}/Linear fest", "TO-92", "to-92.svg",
        [("Ausgangsspannung", "5", "V"), ("Ausgangsstrom", "100", "mA"), ("Eingangsspannung max", "30", "V")],
        ["spannungsregler", "linearregler", "78lxx", "to-92", "5v"], equivalents=["L78L05", "MC78L05"], datasheet_hint="L78L05")
    add(g, "lm317t", "LM317T", "Einstellbarer Spannungsregler 1,25–37 V / 1,5 A, TO-220 (Pinbelegung ADJ-A-E)",
        f"{c}/Linear einstellbar", "TO-220", "to-220.svg",
        [("Ausgangsspannung", "1.25–37", "V"), ("Ausgangsstrom", "1.5", "A")],
        ["spannungsregler", "linearregler", "einstellbar", "lm317", "to-220"], equivalents=["B3170V (DDR)"], datasheet_hint="LM317")
    add(g, "lm317lz", "LM317LZ", "Einstellbarer Spannungsregler 1,25–37 V / 100 mA, TO-92",
        f"{c}/Linear einstellbar", "TO-92", "to-92.svg",
        [("Ausgangsspannung", "1.25–37", "V"), ("Ausgangsstrom", "100", "mA")],
        ["spannungsregler", "linearregler", "einstellbar", "lm317", "to-92"], datasheet_hint="LM317L")
    add(g, "lm337t", "LM337T", "Einstellbarer negativer Spannungsregler −1,25 bis −37 V / 1,5 A, TO-220",
        f"{c}/Linear einstellbar", "TO-220", "to-220.svg",
        [("Ausgangsspannung", "-37–-1.25", "V"), ("Ausgangsstrom", "1.5", "A")],
        ["spannungsregler", "linearregler", "einstellbar", "negativ", "to-220"], datasheet_hint="LM337")
    for v in ("3.3", "5.0"):
        add(g, f"ams1117-{v.replace('.', '')}", f"AMS1117-{v}", f"LDO-Festspannungsregler {de(v)} V / 1 A, SOT-223",
            f"{c}/LDO", "SOT-223", "sot-223.svg",
            [("Ausgangsspannung", v, "V"), ("Ausgangsstrom", "1", "A"), ("Eingangsspannung max", "15", "V")],
            ["spannungsregler", "ldo", "ams1117", "sot-223"], equivalents=[f"LM1117-{v} (800 mA)"], datasheet_hint="AMS1117")
    add(g, "ams1117-adj", "AMS1117-ADJ", "Einstellbarer LDO-Regler 1,25–13,8 V / 1 A, SOT-223",
        f"{c}/LDO", "SOT-223", "sot-223.svg",
        [("Ausgangsspannung", "1.25–13.8", "V"), ("Ausgangsstrom", "1", "A"), ("Eingangsspannung max", "15", "V")],
        ["spannungsregler", "ldo", "ams1117", "einstellbar", "sot-223"], datasheet_hint="AMS1117")
    add(g, "lm1117-33", "LM1117-3.3", "LDO-Festspannungsregler 3,3 V / 800 mA, SOT-223",
        f"{c}/LDO", "SOT-223", "sot-223.svg",
        [("Ausgangsspannung", "3.3", "V"), ("Ausgangsstrom", "800", "mA")],
        ["spannungsregler", "ldo", "lm1117", "sot-223"], equivalents=["AMS1117-3.3 (ähnlich)"], datasheet_hint="LM1117")
    add(g, "ld1117v33", "LD1117V33", "LDO-Festspannungsregler 3,3 V / 800 mA, TO-220",
        f"{c}/LDO", "TO-220", "to-220.svg",
        [("Ausgangsspannung", "3.3", "V"), ("Ausgangsstrom", "800", "mA"), ("Eingangsspannung max", "15", "V")],
        ["spannungsregler", "ldo", "ld1117", "to-220"], manufacturer="STMicroelectronics", mpn="LD1117V33", datasheet_hint="LD1117")
    add(g, "lm2940-50", "LM2940-5.0", "LDO-Festspannungsregler 5 V / 1 A, TO-220, verpolungs- und lastabwurffest (Kfz)",
        f"{c}/LDO", "TO-220", "to-220.svg",
        [("Ausgangsspannung", "5", "V"), ("Ausgangsstrom", "1", "A"), ("Eingangsspannung max", "26", "V")],
        ["spannungsregler", "ldo", "lm2940", "to-220", "kfz"], datasheet_hint="LM2940")
    add(g, "ht7333", "HT7333", "LDO-Festspannungsregler 3,3 V / 250 mA, sehr geringer Ruhestrom, SOT-89 (auch TO-92)",
        f"{c}/LDO", "SOT-89", "sot-89.svg",
        [("Ausgangsspannung", "3.3", "V"), ("Ausgangsstrom", "250", "mA"), ("Eingangsspannung max", "12", "V")],
        ["spannungsregler", "ldo", "ht7333", "low-power"], manufacturer="Holtek", datasheet_hint="HT73xx")
    add(g, "mcp1700-3302", "MCP1700-3302E", "LDO-Festspannungsregler 3,3 V / 250 mA, Ruhestrom 1,6 µA, TO-92 (Pinbelegung beachten)",
        f"{c}/LDO", "TO-92", "to-92.svg",
        [("Ausgangsspannung", "3.3", "V"), ("Ausgangsstrom", "250", "mA"), ("Eingangsspannung max", "6", "V")],
        ["spannungsregler", "ldo", "mcp1700", "low-power", "to-92"], manufacturer="Microchip", mpn="MCP1700-3302E/TO",
        datasheet_hint="MCP1700")
    add(g, "tl431", "TL431", "Einstellbare Präzisions-Referenz (Shunt-Regler) 2,5–36 V, TO-92",
        f"{c}/Referenzen", "TO-92", "to-92.svg",
        [("Ausgangsspannung", "2.495–36", "V"), ("Strom", "100", "mA")],
        ["referenz", "shunt", "tl431", "to-92"], datasheet_hint="TL431")
    add(g, "icl7660", "ICL7660", "Ladungspumpen-Spannungsinverter (erzeugt −U aus +U), 1,5–10 V, DIP-8",
        f"{c}/Ladungspumpen", "DIP-8", "dip-8.svg",
        [("Eingangsspannung max", "10", "V")], ["ladungspumpe", "inverter", "negativ", "dip-8"], datasheet_hint="ICL7660")

    m = "Elektronik/Module/Spannungswandler"
    add(g, "mod-lm2596", "LM2596 Step-Down-Modul", "Einstellbarer Abwärtswandler mit LM2596, Eingang bis 40 V, Ausgang 1,25–35 V, IC-Nennstrom 3 A (Modul praktisch ca. 2 A ohne Kühlung)",
        m, "Modul", "module-pcb.svg",
        [("Eingangsspannung max", "40", "V"), ("Ausgangsspannung", "1.25–35", "V"), ("Ausgangsstrom", "3", "A")],
        ["modul", "step-down", "buck", "lm2596", "dc-dc"], datasheet_hint="LM2596")
    add(g, "mod-mp1584", "MP1584 Step-Down-Modul", "Kompakter einstellbarer Abwärtswandler mit MP1584, Eingang 4,5–28 V, bis 3 A, 1,5 MHz",
        m, "Modul", "module-pcb.svg",
        [("Eingangsspannung max", "28", "V"), ("Ausgangsstrom", "3", "A")],
        ["modul", "step-down", "buck", "mp1584", "dc-dc"], datasheet_hint="MP1584")
    add(g, "mod-xl6009", "XL6009 Step-Up-Modul", "Einstellbarer Aufwärtswandler mit XL6009, Eingang bis 32 V, Ausgang bis ca. 35 V",
        m, "Modul", "module-pcb.svg",
        [("Eingangsspannung max", "32", "V")],
        ["modul", "step-up", "boost", "xl6009", "dc-dc"], datasheet_hint="XL6009")
    add(g, "mod-mt3608", "MT3608 Step-Up-Modul", "Kleiner einstellbarer Aufwärtswandler mit MT3608, Eingang 2–24 V, Ausgang bis 28 V",
        m, "Modul", "module-pcb.svg",
        [("Eingangsspannung max", "24", "V"), ("Ausgangsspannung", "28", "V")],
        ["modul", "step-up", "boost", "mt3608", "dc-dc"], datasheet_hint="MT3608")
    add(g, "mod-tp4056", "TP4056 Lademodul (mit Schutz)", "Li-Ion/LiPo-Lademodul 1 S, 4,2 V, bis 1 A, mit Schutzschaltung (DW01A + FS8205A), USB-C oder Micro-USB",
        m, "Modul", "module-pcb.svg",
        [("Ausgangsspannung", "4.2", "V"), ("Ausgangsstrom", "1", "A")],
        ["modul", "lader", "li-ion", "lipo", "tp4056", "akku"], datasheet_hint="TP4056")


# =============================================================== Logik
def build_logic():
    g = "ics_logik"
    c74 = "Elektronik/ICs/Logik/74HC"
    hc = [("00", "4× NAND mit 2 Eingängen", 14), ("02", "4× NOR mit 2 Eingängen", 14), ("04", "6× Inverter", 14),
          ("08", "4× AND mit 2 Eingängen", 14), ("14", "6× Inverter mit Schmitt-Trigger", 14),
          ("32", "4× OR mit 2 Eingängen", 14), ("74", "2× D-Flipflop mit Set/Reset", 14),
          ("86", "4× XOR mit 2 Eingängen", 14), ("125", "4× Bustreiber, Tristate", 14),
          ("138", "3-zu-8-Dekoder/Demultiplexer", 16), ("164", "8-Bit-Schieberegister, seriell-parallel", 14),
          ("165", "8-Bit-Schieberegister, parallel-seriell", 16), ("244", "8× Bustreiber, Tristate", 20),
          ("245", "8× bidirektionaler Bustransceiver, Tristate", 20),
          ("595", "8-Bit-Schieberegister mit Ausgangslatch, Tristate", 16),
          ("4051", "8-Kanal-Analogmultiplexer/-demultiplexer", 16)]
    for num, fn, pins in hc:
        add(g, f"74hc{num}", f"74HC{num}", f"{fn}, CMOS 2–6 V, DIP-{pins}", c74, f"DIP-{pins}", f"dip-{pins}.svg",
            [("Versorgungsspannung", "2–6", "V"), ("Typ", fn)],
            ["logik", "74hc", "cmos", f"dip-{pins}", f"74{num}"],
            equivalents=[f"74HCT{num} (TTL-Pegel-Eingänge)", f"SN74HC{num}N"] if num != "4051" else ["CD4051 (3–18 V)"],
            datasheet_hint=f"74HC{num}")
    for num, fn, pins in (("595", "8-Bit-Schieberegister mit Ausgangslatch, Tristate", 16), ("04", "6× Inverter", 14),
                          ("14", "6× Inverter mit Schmitt-Trigger", 14)):
        add(g, f"74hc{num}-so", f"74HC{num}D", f"{fn}, CMOS 2–6 V, SMD SO-{pins}", c74, f"SO-{pins}", f"so-{pins}.svg",
            [("Versorgungsspannung", "2–6", "V"), ("Typ", fn)],
            ["logik", "74hc", "cmos", "smd", f"so-{pins}", f"74{num}"], equivalents=[f"74HC{num} (DIP)"],
            datasheet_hint=f"74HC{num}")
    c40 = "Elektronik/ICs/Logik/CD4000"
    cd = [("4001", "4× NOR mit 2 Eingängen", 14), ("4011", "4× NAND mit 2 Eingängen", 14),
          ("4013", "2× D-Flipflop mit Set/Reset", 14), ("4017", "Dezimalzähler/Teiler mit 10 dekodierten Ausgängen", 16),
          ("4040", "12-stufiger Binärzähler", 16), ("4049", "6× invertierender Puffer", 16),
          ("4051", "8-Kanal-Analogmultiplexer/-demultiplexer", 16), ("4060", "14-stufiger Binärzähler mit Oszillator", 16),
          ("4066", "4× bilateraler Analogschalter", 14), ("4069", "6× Inverter", 14),
          ("4093", "4× NAND mit Schmitt-Trigger", 14), ("4511", "BCD-zu-7-Segment-Dekoder/Treiber mit Latch", 16)]
    for num, fn, pins in cd:
        add(g, f"cd{num}", f"CD{num}B", f"{fn}, CMOS 3–18 V, DIP-{pins}", c40, f"DIP-{pins}", f"dip-{pins}.svg",
            [("Versorgungsspannung", "3–18", "V"), ("Typ", fn)],
            ["logik", "cd4000", "cmos", f"dip-{pins}", num],
            equivalents=[f"HEF{num}B", f"MC14{num[1:]}B"], datasheet_hint=f"CD{num}B")
    ct = "Elektronik/ICs/Treiber"
    add(g, "uln2003a", "ULN2003A", "7-fach Darlington-Treiber, 50 V / 500 mA je Kanal, mit Freilaufdioden, DIP-16",
        ct, "DIP-16", "dip-16.svg",
        [("Kanäle", "7"), ("U_CE max", "50", "V"), ("I_C max", "500", "mA")],
        ["treiber", "darlington", "uln2003", "schrittmotor", "relais", "dip-16"], datasheet_hint="ULN2003A")
    add(g, "uln2803a", "ULN2803A", "8-fach Darlington-Treiber, 50 V / 500 mA je Kanal, mit Freilaufdioden, DIP-18",
        ct, "DIP-18", "dip-18.svg",
        [("Kanäle", "8"), ("U_CE max", "50", "V"), ("I_C max", "500", "mA")],
        ["treiber", "darlington", "uln2803", "dip-18"], datasheet_hint="ULN2803A")


# =============================================================== Analog / Schnittstellen
def build_analog():
    g = "ics_analog"
    c = "Elektronik/ICs"

    def ic(key, name, desc, sub, fp, params=(), tags=(), eq=None, hint=None, img=None, mfr="", mpn=""):
        img = img or {"DIP-4": "dip-4.svg", "DIP-6": "dip-6.svg", "DIP-8": "dip-8.svg", "DIP-14": "dip-14.svg",
                      "DIP-16": "dip-16.svg", "DIP-18": "dip-18.svg", "DIP-20": "dip-20.svg", "DIP-24": "dip-24.svg",
                      "DIP-28": "dip-28.svg", "DIP-40": "dip-40.svg", "SO-8": "so-8.svg", "SO-16": "so-16.svg",
                      "MSOP-10": "tssop.svg", "SSOP-24": "tssop.svg", "SSOP-20": "tssop.svg", "QFN-28": "qfn.svg",
                      "TO-220-5": "to-220-5.svg", "Multiwatt-15": "multiwatt.svg"}[fp]
        add(g, key, name, desc, f"{c}/{sub}", fp, img, params, list(tags) + [fp.lower()],
            equivalents=eq, datasheet_hint=hint or name, manufacturer=mfr, mpn=mpn)

    ic("ne555", "NE555", "Timer-IC (astabil/monostabil), 4,5–16 V, DIP-8", "Timer", "DIP-8",
       [("Versorgungsspannung", "4.5–16", "V")], ["timer", "555"], ["LM555", "B555D (DDR)", "TLC555/ICM7555 (CMOS)"])
    ic("ne556", "NE556", "Doppel-Timer-IC (2× 555), 4,5–16 V, DIP-14", "Timer", "DIP-14",
       [("Versorgungsspannung", "4.5–16", "V"), ("Kanäle", "2")], ["timer", "556"], ["LM556"])
    ic("tlc555", "TLC555", "CMOS-Timer (stromsparender 555), 2–15 V, DIP-8", "Timer", "DIP-8",
       [("Versorgungsspannung", "2–15", "V")], ["timer", "555", "cmos"], ["ICM7555 (ähnlich)", "NE555 (bipolar)"])
    opamps = [
        ("lm358", "LM358", "Doppel-Operationsverstärker, Single-Supply 3–32 V, Eingang bis GND", "DIP-8", "2", "3–32", ["LM2904", "LM324 (4-fach)"]),
        ("lm324", "LM324", "Vierfach-Operationsverstärker, Single-Supply 3–32 V, Eingang bis GND", "DIP-14", "4", "3–32", ["LM2902", "LM358 (2-fach)"]),
        ("tl071", "TL071", "JFET-Operationsverstärker, rauscharm, DIP-8", "DIP-8", "1", None, ["B081D (DDR, ähnlich TL081)", "TL081"]),
        ("tl072", "TL072", "Doppel-JFET-Operationsverstärker, rauscharm, DIP-8", "DIP-8", "2", None, ["TL082", "B082D (DDR, ≈ TL082)"]),
        ("tl074", "TL074", "Vierfach-JFET-Operationsverstärker, rauscharm, DIP-14", "DIP-14", "4", None, ["TL084", "B084D (DDR, ≈ TL084)"]),
        ("lm741", "LM741", "Klassischer Operationsverstärker (µA741), DIP-8", "DIP-8", "1", None, ["µA741", "MAA741 (Tesla)"]),
        ("ne5532", "NE5532", "Doppel-Operationsverstärker, rauscharm, Audio, DIP-8", "DIP-8", "2", None, None),
        ("opa2134", "OPA2134", "Doppel-Audio-Operationsverstärker (FET-Eingang), DIP-8", "DIP-8", "2", None, None),
        ("mcp6002", "MCP6002", "Doppel-Operationsverstärker Rail-to-Rail, 1,8–6 V, 1 MHz, DIP-8", "DIP-8", "2", "1.8–6", None),
    ]
    for key, name, desc, fp, ch, vs, eq in opamps:
        ps = [("Kanäle", ch)] + ([("Versorgungsspannung", vs, "V")] if vs else [])
        ic(key, name, desc, "Operationsverstärker", fp, ps, ["opamp", "operationsverstärker"], eq)
    ic("lm393", "LM393", "Doppel-Komparator, Open-Collector, 2–36 V, DIP-8", "Komparatoren", "DIP-8",
       [("Kanäle", "2"), ("Versorgungsspannung", "2–36", "V")], ["komparator"], ["LM2903", "LM339 (4-fach)"])
    ic("lm339", "LM339", "Vierfach-Komparator, Open-Collector, 2–36 V, DIP-14", "Komparatoren", "DIP-14",
       [("Kanäle", "4"), ("Versorgungsspannung", "2–36", "V")], ["komparator"], ["LM2901", "LM393 (2-fach)"])
    ic("lm311", "LM311", "Komparator mit Open-Collector-Ausgang, DIP-8", "Komparatoren", "DIP-8",
       [("Kanäle", "1")], ["komparator"])
    ic("lm386", "LM386", "NF-Leistungsverstärker (Audio, ca. 0,3–1 W), 4–12 V (LM386N-1), DIP-8", "Audio", "DIP-8",
       [("Versorgungsspannung", "4–12", "V")], ["audio", "verstärker", "nf"])
    ic("tda2030a", "TDA2030A", "NF-Leistungsverstärker (Audio, ca. 14–18 W), Pentawatt/TO-220-5", "Audio", "TO-220-5",
       [], ["audio", "verstärker", "nf", "endstufe"], ["A2030 (DDR, Gehäuse prüfen)"])
    ic("tl494", "TL494", "PWM-Controller für Schaltnetzteile, 7–40 V, DIP-16", "PWM-Controller", "DIP-16",
       [("Versorgungsspannung", "7–40", "V")], ["pwm", "schaltnetzteil", "smps"])
    ic("max232", "MAX232", "RS-232-Pegelwandler 5 V, 2 Sender/2 Empfänger, DIP-16", "Schnittstelle", "DIP-16",
       [("Versorgungsspannung", "5", "V"), ("Schnittstelle", "RS-232")], ["rs232", "pegelwandler", "seriell"], ["MAX3232 (3,3 V)"])
    ic("max3232", "MAX3232", "RS-232-Pegelwandler 3–5,5 V, 2 Sender/2 Empfänger, DIP-16", "Schnittstelle", "DIP-16",
       [("Versorgungsspannung", "3–5.5", "V"), ("Schnittstelle", "RS-232")], ["rs232", "pegelwandler", "seriell", "3v3"], ["MAX232 (nur 5 V)"])
    ic("max485", "MAX485", "RS-485/RS-422-Transceiver 5 V, Halbduplex, DIP-8", "Schnittstelle", "DIP-8",
       [("Versorgungsspannung", "5", "V"), ("Schnittstelle", "RS-485")], ["rs485", "modbus", "transceiver"], ["SN75176 (ähnlich)"])
    ic("ch340g", "CH340G", "USB-zu-UART-Wandler, benötigt 12-MHz-Quarz, SO-16", "Schnittstelle", "SO-16",
       [("Schnittstelle", "USB/UART")], ["usb", "uart", "seriell", "ch340"], ["CH340C (ohne Quarz)"], mfr="WCH")
    ic("cp2102", "CP2102", "USB-zu-UART-Wandler mit integriertem Oszillator, QFN-28 (5 × 5 mm)", "Schnittstelle", "QFN-28",
       [("Schnittstelle", "USB/UART")], ["usb", "uart", "seriell", "cp2102"], mfr="Silicon Labs")
    ic("ds3231", "DS3231SN", "Hochgenaue I²C-Echtzeituhr (RTC) mit temperaturkompensiertem Quarz, SO-16 breit", "RTC", "SO-16",
       [("Schnittstelle", "I²C"), ("Versorgungsspannung", "2.3–5.5", "V")], ["rtc", "uhr", "i2c"], ["DS3231M (MEMS, SO-8)"], "DS3231", mfr="Analog Devices (Maxim)")
    ic("ds1307", "DS1307", "I²C-Echtzeituhr (RTC), benötigt 32,768-kHz-Quarz, 5 V, DIP-8", "RTC", "DIP-8",
       [("Schnittstelle", "I²C"), ("Versorgungsspannung", "4.5–5.5", "V")], ["rtc", "uhr", "i2c"])
    ic("24c02", "24C02", "Serielles EEPROM 2 kbit (256 × 8), I²C, DIP-8", "Speicher", "DIP-8",
       [("Schnittstelle", "I²C"), ("Typ", "EEPROM 2 kbit")], ["eeprom", "speicher", "i2c"], ["AT24C02"])
    ic("24c256", "24C256", "Serielles EEPROM 256 kbit (32 k × 8), I²C, DIP-8", "Speicher", "DIP-8",
       [("Schnittstelle", "I²C"), ("Typ", "EEPROM 256 kbit")], ["eeprom", "speicher", "i2c"], ["AT24C256"])
    ic("93c46", "93C46", "Serielles EEPROM 1 kbit, Microwire, DIP-8", "Speicher", "DIP-8",
       [("Schnittstelle", "Microwire"), ("Typ", "EEPROM 1 kbit")], ["eeprom", "speicher", "microwire"])
    ic("pcf8574", "PCF8574", "8-Bit-I/O-Portexpander, I²C, 2,5–6 V, DIP-16", "Portexpander", "DIP-16",
       [("Schnittstelle", "I²C"), ("Versorgungsspannung", "2.5–6", "V"), ("Kanäle", "8")], ["portexpander", "gpio", "i2c"], ["PCF8574A (andere I²C-Adresse)"])
    ic("mcp23017", "MCP23017", "16-Bit-I/O-Portexpander, I²C, 1,8–5,5 V, DIP-28 (schmal)", "Portexpander", "DIP-28",
       [("Schnittstelle", "I²C"), ("Versorgungsspannung", "1.8–5.5", "V"), ("Kanäle", "16")], ["portexpander", "gpio", "i2c"], ["MCP23S17 (SPI)"], mfr="Microchip")
    ic("ads1115", "ADS1115", "16-Bit-A/D-Wandler, 4 Kanäle, I²C, PGA; meist als Breakout-Modul genutzt, MSOP-10", "ADC & DAC", "MSOP-10",
       [("Schnittstelle", "I²C"), ("Kanäle", "4"), ("Versorgungsspannung", "2–5.5", "V")], ["adc", "a/d", "i2c"], ["ADS1015 (12 Bit)"], mfr="Texas Instruments")
    ic("max7219", "MAX7219", "LED-Anzeigentreiber für 8 Stellen 7-Segment oder 8×8-Matrix, seriell (SPI-ähnlich), DIP-24", "Treiber", "DIP-24",
       [("Schnittstelle", "SPI")], ["led-treiber", "7-segment", "matrix"], mfr="Analog Devices (Maxim)")
    ic("l293d", "L293D", "Doppel-H-Brücke 600 mA je Kanal mit Freilaufdioden, Motorspannung 4,5–36 V, DIP-16", "Motortreiber", "DIP-16",
       [("Ausgangsstrom", "600", "mA"), ("Kanäle", "2")], ["motortreiber", "h-brücke", "dc-motor", "schrittmotor"])
    ic("l298n", "L298N", "Doppel-H-Brücke 2 A je Kanal, bis 46 V, Multiwatt-15 (externe Freilaufdioden nötig)", "Motortreiber", "Multiwatt-15",
       [("Ausgangsstrom", "2", "A"), ("Kanäle", "2")], ["motortreiber", "h-brücke", "dc-motor", "schrittmotor"])
    ic("tb6612fng", "TB6612FNG", "Doppel-MOSFET-H-Brücke 1,2 A (Spitze 3,2 A), Motorspannung bis 13,5 V, SSOP-24; meist als Breakout-Board", "Motortreiber", "SSOP-24",
       [("Ausgangsstrom", "1.2", "A"), ("Kanäle", "2")], ["motortreiber", "h-brücke", "dc-motor"], mfr="Toshiba")


# =============================================================== Mikrocontroller
def build_mcu():
    g = "mikrocontroller"
    c = "Elektronik/Mikrocontroller"

    def mcu(key, name, desc, sub, fp, img, flash=None, ram=None, clk=None, vs=None, extra=(), tags=(), eq=None, mfr=""):
        ps = []
        if flash:
            ps.append(("Flash", flash[0], flash[1]))
        if ram:
            ps.append(("RAM", ram[0], ram[1]))
        if clk:
            ps.append(("Takt", clk, "Hz"))
        if vs:
            ps.append(("Versorgungsspannung", vs, "V"))
        ps += list(extra)
        add(g, key, name, desc, f"{c}/{sub}", fp, img, ps, ["mikrocontroller", "mcu"] + list(tags),
            equivalents=eq, datasheet_hint=name, manufacturer=mfr)

    avr = "AVR"
    mcu("atmega328p-pu", "ATmega328P-PU", "8-Bit-AVR, 32 KB Flash, 2 KB SRAM, 1 KB EEPROM, bis 20 MHz, DIP-28 (Arduino-Uno-Chip)",
        avr, "DIP-28", "dip-28.svg", ("32", "KB"), ("2", "KB"), "20M", "1.8–5.5", [("EEPROM", "1", "KB")], ["avr", "atmega", "arduino"],
        ["ATmega328P-AU (TQFP-32)"], "Microchip (Atmel)")
    mcu("atmega328p-au", "ATmega328P-AU", "8-Bit-AVR, 32 KB Flash, 2 KB SRAM, 1 KB EEPROM, bis 20 MHz, TQFP-32",
        avr, "TQFP-32", "tqfp.svg", ("32", "KB"), ("2", "KB"), "20M", "1.8–5.5", [("EEPROM", "1", "KB")], ["avr", "atmega", "arduino", "smd"],
        ["ATmega328P-PU (DIP-28)"], "Microchip (Atmel)")
    mcu("atmega8a", "ATmega8A-PU", "8-Bit-AVR, 8 KB Flash, 1 KB SRAM, 512 B EEPROM, bis 16 MHz, DIP-28",
        avr, "DIP-28", "dip-28.svg", ("8", "KB"), ("1", "KB"), "16M", "2.7–5.5", [("EEPROM", "512", "B")], ["avr", "atmega"], mfr="Microchip (Atmel)")
    mcu("atmega32a", "ATmega32A-PU", "8-Bit-AVR, 32 KB Flash, 2 KB SRAM, 1 KB EEPROM, bis 16 MHz, DIP-40",
        avr, "DIP-40", "dip-40.svg", ("32", "KB"), ("2", "KB"), "16M", "2.7–5.5", [("EEPROM", "1", "KB")], ["avr", "atmega"], mfr="Microchip (Atmel)")
    mcu("atmega1284p", "ATmega1284P-PU", "8-Bit-AVR, 128 KB Flash, 16 KB SRAM, 4 KB EEPROM, bis 20 MHz, DIP-40",
        avr, "DIP-40", "dip-40.svg", ("128", "KB"), ("16", "KB"), "20M", "1.8–5.5", [("EEPROM", "4", "KB")], ["avr", "atmega"], mfr="Microchip (Atmel)")
    for key, name, fl, ram, ee, fp in (("attiny85", "ATtiny85-20PU", "8", "512", "512", "DIP-8"),
                                       ("attiny45", "ATtiny45-20PU", "4", "256", "256", "DIP-8"),
                                       ("attiny84a", "ATtiny84A-PU", "8", "512", "512", "DIP-14"),
                                       ("attiny44a", "ATtiny44A-PU", "4", "256", "256", "DIP-14")):
        vs = "2.7–5.5" if "-20" in name else "1.8–5.5"
        mcu(key, name, f"8-Bit-AVR, {fl} KB Flash, {ram} B SRAM, {ee} B EEPROM, bis 20 MHz, {fp}",
            avr, fp, f"{fp.lower()}.svg", (fl, "KB"), (ram, "B"), "20M", vs, [("EEPROM", ee, "B")], ["avr", "attiny"], mfr="Microchip (Atmel)")
    mcu("attiny13a", "ATtiny13A-PU", "8-Bit-AVR, 1 KB Flash, 64 B SRAM, 64 B EEPROM, bis 20 MHz, DIP-8",
        avr, "DIP-8", "dip-8.svg", ("1", "KB"), ("64", "B"), "20M", "1.8–5.5", [("EEPROM", "64", "B")], ["avr", "attiny"], mfr="Microchip (Atmel)")
    mcu("attiny2313a", "ATtiny2313A-PU", "8-Bit-AVR, 2 KB Flash, 128 B SRAM, 128 B EEPROM, UART, bis 20 MHz, DIP-20",
        avr, "DIP-20", "dip-20.svg", ("2", "KB"), ("128", "B"), "20M", "1.8–5.5", [("EEPROM", "128", "B")], ["avr", "attiny"], mfr="Microchip (Atmel)")
    pic = "PIC"
    mcu("pic12f675", "PIC12F675", "8-Bit-PIC, 1 k Worte Flash, 64 B RAM, 128 B EEPROM, 10-Bit-ADC, bis 20 MHz, DIP-8",
        pic, "DIP-8", "dip-8.svg", ("1.75", "KB"), ("64", "B"), "20M", "2–5.5", [("EEPROM", "128", "B")], ["pic"], mfr="Microchip")
    mcu("pic16f84a", "PIC16F84A", "8-Bit-PIC, 1 k Worte Flash, 68 B RAM, 64 B EEPROM, bis 20 MHz, DIP-18",
        pic, "DIP-18", "dip-18.svg", ("1.75", "KB"), ("68", "B"), "20M", None, [("EEPROM", "64", "B")], ["pic"], mfr="Microchip")
    mcu("pic16f628a", "PIC16F628A", "8-Bit-PIC, 2 k Worte Flash, 224 B RAM, 128 B EEPROM, interner Oszillator, bis 20 MHz, DIP-18",
        pic, "DIP-18", "dip-18.svg", ("3.5", "KB"), ("224", "B"), "20M", "2–5.5", [("EEPROM", "128", "B")], ["pic"], mfr="Microchip")
    mcu("pic16f877a", "PIC16F877A", "8-Bit-PIC, 8 k Worte Flash, 368 B RAM, 256 B EEPROM, bis 20 MHz, DIP-40",
        pic, "DIP-40", "dip-40.svg", ("14", "KB"), ("368", "B"), "20M", "2–5.5", [("EEPROM", "256", "B")], ["pic"], mfr="Microchip")
    mcu("pic18f2550", "PIC18F2550", "8-Bit-PIC18 mit Full-Speed-USB, 32 KB Flash, 2 KB RAM, 256 B EEPROM, bis 48 MHz, DIP-28",
        pic, "DIP-28", "dip-28.svg", ("32", "KB"), ("2", "KB"), "48M", None, [("EEPROM", "256", "B"), ("Schnittstelle", "USB 2.0 FS")], ["pic", "usb"], mfr="Microchip")
    arm = "ARM"
    mcu("stm32f103c8t6", "STM32F103C8T6", "32-Bit-ARM Cortex-M3, 64 KB Flash, 20 KB SRAM, 72 MHz, USB, LQFP-48 (\"Blue Pill\"-Chip)",
        arm, "LQFP-48", "tqfp.svg", ("64", "KB"), ("20", "KB"), "72M", "2–3.6", [], ["stm32", "arm", "cortex-m3"], mfr="STMicroelectronics")
    mcu("rp2040", "RP2040", "Dual-Core ARM Cortex-M0+, 264 KB SRAM, bis 133 MHz, externer QSPI-Flash nötig, PIO, QFN-56",
        arm, "QFN-56", "qfn.svg", None, ("264", "KB"), "133M", None, [], ["rp2040", "arm", "cortex-m0+", "raspberry"], mfr="Raspberry Pi")
    esp = "ESP"
    mcu("esp32-wroom-32", "ESP32-WROOM-32", "WLAN/Bluetooth-Modul mit ESP32 Dual-Core 240 MHz, 520 KB SRAM, 4 MB Flash, 3,3 V",
        esp, "Modul", "module-esp.svg", ("4", "MB"), ("520", "KB"), "240M", "3.0–3.6", [("Schnittstelle", "WLAN, Bluetooth")],
        ["esp32", "wlan", "wifi", "bluetooth"], ["ESP32-WROOM-32E (Nachfolger)"], "Espressif")
    mcu("esp-12f", "ESP-12F", "WLAN-Modul mit ESP8266, 4 MB Flash, 80/160 MHz, 3,3 V",
        esp, "Modul", "module-esp.svg", ("4", "MB"), None, "80M", "3.3", [("Schnittstelle", "WLAN")],
        ["esp8266", "wlan", "wifi"], ["ESP-12E (ähnlich)"], "Ai-Thinker")
    mcu("esp-01s", "ESP-01S", "Kleines WLAN-Modul mit ESP8266, 1 MB Flash, 2×4-Stiftleiste, nur 3,3 V (nicht 5-V-tolerant)",
        esp, "Modul", "module-esp.svg", ("1", "MB"), None, "80M", "3.3", [("Schnittstelle", "WLAN")],
        ["esp8266", "wlan", "wifi", "esp-01"], ["ESP-01 (512 KB oder 1 MB Flash)"], "Ai-Thinker")

    rf = "Elektronik/Module/Funk"
    for key, name, desc, ps, tags in (
        ("nrf24l01", "nRF24L01+", "2,4-GHz-Funkmodul mit nRF24L01+, SPI, bis 2 Mbit/s, 1,9–3,6 V (nicht an 5 V!)",
         [("Frequenz", "2.4G", "Hz"), ("Schnittstelle", "SPI"), ("Versorgungsspannung", "1.9–3.6", "V")], ["nrf24", "2.4ghz", "funk"]),
        ("nrf24l01-pa", "nRF24L01+ PA/LNA", "2,4-GHz-Funkmodul mit nRF24L01+, Leistungsverstärker und externer Antenne (größere Reichweite)",
         [("Frequenz", "2.4G", "Hz"), ("Schnittstelle", "SPI"), ("Versorgungsspannung", "1.9–3.6", "V")], ["nrf24", "2.4ghz", "funk", "antenne"]),
        ("hc-05", "HC-05", "Bluetooth-2.0-Modul (SPP), UART, Master/Slave, 3,3-V-Logik",
         [("Schnittstelle", "UART, Bluetooth")], ["bluetooth", "uart", "funk"]),
        ("hc-06", "HC-06", "Bluetooth-2.0-Modul (SPP), UART, nur Slave, 3,3-V-Logik",
         [("Schnittstelle", "UART, Bluetooth")], ["bluetooth", "uart", "funk"]),
        ("ra-02", "Ra-02 (SX1278)", "LoRa-Funkmodul 433 MHz mit Semtech SX1278, SPI, 3,3 V",
         [("Frequenz", "433M", "Hz"), ("Schnittstelle", "SPI"), ("Versorgungsspannung", "3.3", "V")], ["lora", "sx1278", "433mhz", "funk"]),
        ("rfm95w", "RFM95W", "LoRa-Funkmodul 868/915 MHz (SX1276-kompatibel), SPI, 3,3 V",
         [("Frequenz", "868M", "Hz"), ("Schnittstelle", "SPI"), ("Versorgungsspannung", "3.3", "V")], ["lora", "sx1276", "868mhz", "funk", "ttn"]),
        ("sim800l", "SIM800L", "GSM/GPRS-Modul (Quad-Band, 2G), UART, 3,4–4,4 V, Spitzenstrom bis 2 A",
         [("Schnittstelle", "UART"), ("Versorgungsspannung", "3.4–4.4", "V")], ["gsm", "gprs", "sms", "2g", "funk"]),
        ("fs1000a-set", "433-MHz-Sender/Empfänger-Set", "Einfaches 433-MHz-ASK-Set: Sender FS1000A + Empfänger XY-MK-5V",
         [("Frequenz", "433M", "Hz")], ["433mhz", "funk", "ask"]),
    ):
        add("module", f"mod-{key}", name, desc, rf, "Modul", "module-pcb.svg", ps, ["modul"] + tags, datasheet_hint=name)


# =============================================================== Module
def build_modules():
    g = "module"
    b = "Elektronik/Module/Entwicklungsboards"
    boards = [
        ("arduino-nano", "Arduino Nano", "Board mit ATmega328P, 16 MHz, 5 V, 32 KB Flash; Klone meist mit CH340 und Mini-USB/USB-C", "module-arduino.svg",
         [("Takt", "16M", "Hz"), ("Flash", "32", "KB"), ("RAM", "2", "KB"), ("Versorgungsspannung", "5", "V")], ["arduino", "atmega328p", "nano"]),
        ("arduino-uno-r3", "Arduino Uno R3", "Board mit ATmega328P (DIP-28), 16 MHz, 5 V, USB-B über ATmega16U2, Eingang 7–12 V empfohlen", "module-arduino.svg",
         [("Takt", "16M", "Hz"), ("Flash", "32", "KB"), ("RAM", "2", "KB"), ("Versorgungsspannung", "5", "V")], ["arduino", "atmega328p", "uno"]),
        ("arduino-mega-2560", "Arduino Mega 2560", "Board mit ATmega2560, 16 MHz, 256 KB Flash, 8 KB SRAM, 54 digitale I/O, 16 Analogeingänge", "module-arduino.svg",
         [("Takt", "16M", "Hz"), ("Flash", "256", "KB"), ("RAM", "8", "KB"), ("Versorgungsspannung", "5", "V")], ["arduino", "atmega2560", "mega"]),
        ("arduino-pro-mini-5v", "Arduino Pro Mini 5 V/16 MHz", "Kompaktes Board mit ATmega328P ohne USB (Programmierung über USB-Seriell-Adapter)", "module-arduino.svg",
         [("Takt", "16M", "Hz"), ("Versorgungsspannung", "5", "V")], ["arduino", "atmega328p", "pro-mini"]),
        ("arduino-pro-mini-3v3", "Arduino Pro Mini 3,3 V/8 MHz", "Kompaktes Board mit ATmega328P ohne USB, 3,3 V (Programmierung über USB-Seriell-Adapter)", "module-arduino.svg",
         [("Takt", "8M", "Hz"), ("Versorgungsspannung", "3.3", "V")], ["arduino", "atmega328p", "pro-mini", "3v3"]),
        ("digispark", "Digispark ATtiny85", "Mini-Board mit ATtiny85, USB-Stecker direkt auf Platine (V-USB), ca. 6 KB nutzbarer Flash", "module-arduino.svg",
         [("Takt", "16.5M", "Hz")], ["attiny85", "digispark", "usb"]),
        ("nodemcu-v2", "NodeMCU v2", "ESP8266-Board mit ESP-12E, 4 MB Flash, USB-Seriell (CP2102), Micro-USB, 3,3-V-Logik", "module-esp.svg",
         [("Flash", "4", "MB"), ("Takt", "80M", "Hz"), ("Schnittstelle", "WLAN")], ["esp8266", "nodemcu", "wlan"]),
        ("wemos-d1-mini", "Wemos D1 mini", "Kompaktes ESP8266-Board, 4 MB Flash, CH340, 3,3-V-Logik, viele Aufsteck-Shields", "module-esp.svg",
         [("Flash", "4", "MB"), ("Takt", "80M", "Hz"), ("Schnittstelle", "WLAN")], ["esp8266", "d1-mini", "wlan"]),
        ("esp32-devkitc", "ESP32 DevKitC", "Entwicklungsboard mit ESP32-WROOM-32, USB-Seriell, 3,3-V-Logik", "module-esp.svg",
         [("Flash", "4", "MB"), ("Takt", "240M", "Hz"), ("Schnittstelle", "WLAN, Bluetooth")], ["esp32", "devkit", "wlan", "bluetooth"]),
        ("esp32-cam", "ESP32-CAM", "ESP32-Board mit Kamera OV2640, 4 MB PSRAM, microSD-Slot; ohne USB (Adapter nötig)", "module-esp.svg",
         [("Takt", "240M", "Hz"), ("Schnittstelle", "WLAN, Bluetooth")], ["esp32", "kamera", "wlan"]),
        ("xiao-esp32c3", "Seeed XIAO ESP32C3", "Daumengroßes Board mit ESP32-C3 (RISC-V, 160 MHz), 4 MB Flash, 400 KB SRAM, WLAN + BLE 5, USB-C", "module-esp.svg",
         [("Takt", "160M", "Hz"), ("Flash", "4", "MB"), ("RAM", "400", "KB"), ("Schnittstelle", "WLAN, Bluetooth LE")], ["esp32-c3", "xiao", "risc-v", "wlan"]),
        ("rpi-pico", "Raspberry Pi Pico", "Board mit RP2040 (Dual-Core Cortex-M0+, 133 MHz), 2 MB Flash, 264 KB SRAM, Micro-USB", "module-pcb.svg",
         [("Takt", "133M", "Hz"), ("Flash", "2", "MB"), ("RAM", "264", "KB")], ["rp2040", "pico", "raspberry"]),
        ("rpi-pico-w", "Raspberry Pi Pico W", "Wie Pico, zusätzlich WLAN 2,4 GHz und Bluetooth (CYW43439)", "module-pcb.svg",
         [("Takt", "133M", "Hz"), ("Flash", "2", "MB"), ("RAM", "264", "KB"), ("Schnittstelle", "WLAN, Bluetooth")], ["rp2040", "pico", "raspberry", "wlan"]),
        ("stm32-bluepill", "STM32 \"Blue Pill\"", "Board mit STM32F103C8T6, 72 MHz, 64 KB Flash, 8-MHz-Quarz, Micro-USB (Programmierung per ST-Link)", "module-pcb.svg",
         [("Takt", "72M", "Hz"), ("Flash", "64", "KB"), ("RAM", "20", "KB")], ["stm32", "bluepill", "arm"]),
    ]
    for key, name, desc, img, ps, tags in boards:
        add(g, f"mod-{key}", name, desc, b, "Modul", img, ps, ["modul", "board"] + tags, datasheet_hint=name)

    d = "Elektronik/Module/Displays"
    for key, name, desc, img, ps, tags in (
        ("ssd1306-096", "OLED 0,96\" SSD1306 I²C", "OLED-Display 0,96\", 128×64 Pixel, Controller SSD1306, I²C (4 Pins), 3,3–5 V", "oled.svg",
         [("Schnittstelle", "I²C"), ("Versorgungsspannung", "3.3–5", "V")], ["oled", "ssd1306", "display", "i2c"]),
        ("sh1106-13", "OLED 1,3\" SH1106 I²C", "OLED-Display 1,3\", 128×64 Pixel, Controller SH1106 (nicht SSD1306!), I²C", "oled.svg",
         [("Schnittstelle", "I²C")], ["oled", "sh1106", "display", "i2c"]),
        ("lcd1602-i2c", "LCD 1602 mit I²C-Adapter", "Text-LCD 16×2 Zeichen (HD44780) mit I²C-Rucksack (PCF8574, Adresse meist 0x27 oder 0x3F), 5 V", "lcd1602.svg",
         [("Schnittstelle", "I²C"), ("Versorgungsspannung", "5", "V")], ["lcd", "1602", "hd44780", "display", "i2c"]),
        ("lcd2004-i2c", "LCD 2004 mit I²C-Adapter", "Text-LCD 20×4 Zeichen (HD44780) mit I²C-Rucksack (PCF8574), 5 V", "lcd1602.svg",
         [("Schnittstelle", "I²C"), ("Versorgungsspannung", "5", "V")], ["lcd", "2004", "hd44780", "display", "i2c"]),
        ("tm1637", "TM1637 4-stellige Anzeige", "4-stelliges 7-Segment-Modul mit TM1637, 2-Draht-Ansteuerung (CLK/DIO), Doppelpunkt", "seven-segment.svg",
         [("Schnittstelle", "2-Draht (CLK/DIO)")], ["7-segment", "tm1637", "display"]),
        ("max7219-matrix", "MAX7219 8×8-Matrix", "LED-Matrix 8×8 mit MAX7219, kaskadierbar (auch als 4er-Block FC-16)", "module-pcb.svg",
         [("Schnittstelle", "SPI")], ["matrix", "max7219", "led", "display"]),
        ("max7219-7seg", "MAX7219 8-stellige 7-Segment", "8-stelliges 7-Segment-Modul mit MAX7219, kaskadierbar", "seven-segment.svg",
         [("Schnittstelle", "SPI")], ["7-segment", "max7219", "display"]),
    ):
        add(g, f"mod-{key}", name, desc, d, "Modul", img, ps, ["modul"] + tags, datasheet_hint=name)

    for n in (1, 2, 4, 8):
        add(g, f"mod-relay-{n}ch", f"Relaismodul {n}-Kanal 5 V",
            f"Relaismodul mit {n}× SRD-05VDC-SL-C, 5-V-Spulen, Kontakte je 10 A / 250 V~, Ansteuerung über Transistor/Optokoppler",
            "Elektronik/Module/Relais", "Modul", "module-relay.svg",
            [("Kanäle", str(n)), ("Versorgungsspannung", "5", "V")], ["modul", "relais", "relaismodul", "srd-05vdc"])

    mt = "Elektronik/Module/Motortreiber"
    add(g, "mod-l298n", "L298N Motortreiber-Modul", "Doppel-H-Brücke mit L298N und Kühlkörper, bis 2 A je Kanal, Motorspannung bis 35 V, 5-V-Regler an Bord",
        mt, "Modul", "module-pcb.svg", [("Kanäle", "2"), ("Ausgangsstrom", "2", "A")], ["modul", "motortreiber", "l298n", "h-brücke"])
    add(g, "mod-a4988", "A4988 Schrittmotortreiber", "Schrittmotortreiber-Modul A4988 (StepStick), 8–35 V, bis 2 A mit Kühlung, bis 1/16-Schritt",
        mt, "Modul", "module-pcb.svg", [("Ausgangsstrom", "2", "A"), ("Eingangsspannung max", "35", "V")], ["modul", "schrittmotor", "a4988", "stepstick", "3d-drucker"])
    add(g, "mod-drv8825", "DRV8825 Schrittmotortreiber", "Schrittmotortreiber-Modul DRV8825 (StepStick), 8,2–45 V, bis 2,2 A mit Kühlung, bis 1/32-Schritt",
        mt, "Modul", "module-pcb.svg", [("Ausgangsstrom", "2.2", "A"), ("Eingangsspannung max", "45", "V")], ["modul", "schrittmotor", "drv8825", "stepstick", "3d-drucker"])

    u = "Elektronik/Module/Schnittstellen"
    for key, name, desc, tags in (
        ("ch340-usb-ttl", "USB-Seriell-Adapter CH340", "USB-zu-TTL-UART-Adapter mit CH340, umschaltbar 3,3/5 V", ["ch340", "usb", "uart", "ftdi"]),
        ("ft232rl-usb-ttl", "USB-Seriell-Adapter FT232RL", "USB-zu-TTL-UART-Adapter mit FTDI FT232RL, umschaltbar 3,3/5 V, mit DTR für Arduino-Reset", ["ft232", "ftdi", "usb", "uart"]),
        ("cp2102-usb-ttl", "USB-Seriell-Adapter CP2102", "USB-zu-TTL-UART-Adapter mit CP2102, 3,3-V-Logik (5-V-tolerant je nach Modul prüfen)", ["cp2102", "usb", "uart"]),
        ("microsd", "MicroSD-Kartenmodul", "MicroSD-Kartenleser, SPI, mit 3,3-V-Regler und Pegelwandler für 5-V-Systeme", ["sd", "microsd", "spi", "speicher"]),
        ("levelshifter-4ch", "Pegelwandler 4-Kanal", "Bidirektionaler Logikpegelwandler 3,3 V ↔ 5 V, 4 Kanäle (BSS138), für I²C/SPI geeignet", ["pegelwandler", "level-shifter", "bss138", "i2c"]),
    ):
        add(g, f"mod-{key}", name, desc, u, "Modul", "module-pcb.svg", [], ["modul"] + tags)


# =============================================================== LEDs
def build_leds():
    g = "leds"
    c = "Elektronik/Optoelektronik/LEDs"
    cols = [("rot", "2.0"), ("gruen", None), ("gelb", "2.0"), ("blau", "3.2"), ("weiss", "3.2"), ("orange", "2.0")]
    disp = {"rot": "rot", "gruen": "grün", "gelb": "gelb", "blau": "blau", "weiss": "weiß", "orange": "orange"}
    for size in ("3", "5"):
        for col, uf in cols:
            ps = [("Farbe", disp[col]), ("Durchmesser", size, "mm"), ("Strom", "20", "mA")]
            if uf:
                ps.append(("U_F typ", uf, "V"))
            add(g, f"led-{size}mm-{col}", f"LED {size} mm {disp[col]}",
                f"Standard-LED {size} mm {disp[col]}, bedrahtet, 20 mA (Vorwiderstand nötig; langes Bein = Anode)",
                f"{c}/THT", f"LED {size}mm", f"led-{size}mm-{col}.svg", ps,
                ["led", f"{size}mm", disp[col], "tht"])
    for size in ("0603", "0805"):
        for col, uf in cols[:5]:
            ps = [("Farbe", disp[col]), ("Strom", "20", "mA")]
            if uf:
                ps.append(("U_F typ", uf, "V"))
            add(g, f"led-{size}-{col}", f"LED {size} {disp[col]}",
                f"SMD-LED {disp[col]}, Bauform {size} (Kathodenmarkierung beachten)",
                f"{c}/SMD", size, "led-smd.svg", ps, ["led", "smd", size, disp[col]])
    for cc, ca in (("kathode", "gemeinsame Kathode"), ("anode", "gemeinsame Anode")):
        add(g, f"led-5mm-rgb-{cc}", f"RGB-LED 5 mm ({'CC' if cc == 'kathode' else 'CA'})",
            f"RGB-LED 5 mm, 4 Beine, {ca}, je Farbe eigener Vorwiderstand nötig",
            f"{c}/RGB", "LED 5mm", "led-5mm-rgb.svg",
            [("Farbe", "RGB"), ("Durchmesser", "5", "mm"), ("Typ", ca)], ["led", "rgb", "5mm", "tht", ca.split()[1].lower()])
    a = f"{c}/Adressierbar"
    add(g, "ws2812b", "WS2812B", "Adressierbare RGB-LED 5050 mit integriertem Controller, 5 V, Eindraht-Datenbus 800 kHz, bis ca. 60 mA bei Vollweiß",
        a, "LED 5050", "led-5050.svg", [("Versorgungsspannung", "5", "V"), ("Schnittstelle", "Eindraht (800 kHz)"), ("Farbe", "RGB")],
        ["led", "ws2812b", "neopixel", "adressierbar", "5050", "rgb"], manufacturer="Worldsemi", datasheet_hint="WS2812B",
        equivalents=["SK6812 (kompatibel)"])
    add(g, "sk6812-rgbw", "SK6812 RGBW", "Adressierbare RGBW-LED 5050 (zusätzlicher Weiß-Chip), 5 V, WS2812-ähnliches Protokoll (32 Bit je LED)",
        a, "LED 5050", "led-5050.svg", [("Versorgungsspannung", "5", "V"), ("Schnittstelle", "Eindraht (800 kHz)"), ("Farbe", "RGBW")],
        ["led", "sk6812", "neopixel", "adressierbar", "5050", "rgbw"], datasheet_hint="SK6812")
    add(g, "sk6812-rgb", "SK6812 RGB", "Adressierbare RGB-LED 5050, 5 V, WS2812B-kompatibles Protokoll",
        a, "LED 5050", "led-5050.svg", [("Versorgungsspannung", "5", "V"), ("Schnittstelle", "Eindraht (800 kHz)"), ("Farbe", "RGB")],
        ["led", "sk6812", "neopixel", "adressierbar", "5050", "rgb"], equivalents=["WS2812B"], datasheet_hint="SK6812")
    add(g, "apa102", "APA102", "Adressierbare RGB-LED 5050 mit Takt- und Datenleitung (SPI-artig), 5 V, hohe PWM-Frequenz",
        a, "LED 5050", "led-5050.svg", [("Versorgungsspannung", "5", "V"), ("Schnittstelle", "SPI (Daten + Takt)"), ("Farbe", "RGB")],
        ["led", "apa102", "dotstar", "adressierbar", "5050", "rgb"], equivalents=["SK9822 (ähnlich)"], datasheet_hint="APA102")
    for dens in (30, 60, 144):
        add(g, f"ws2812b-strip-{dens}", f"WS2812B-Streifen {dens} LED/m",
            f"Adressierbarer LED-Streifen mit WS2812B, {dens} LEDs pro Meter, 5 V (Einspeisung alle 1–2 m empfohlen)",
            a, "LED-Streifen", "led-5050.svg", [("Versorgungsspannung", "5", "V"), ("Farbe", "RGB")],
            ["led", "ws2812b", "streifen", "strip", "neopixel", "adressierbar"], unit="m")


# =============================================================== Optoelektronik
def build_opto():
    g = "optoelektronik"
    c = "Elektronik/Optoelektronik"
    add(g, "led-5mm-ir-940", "IR-LED 5 mm 940 nm", "Infrarot-Sendediode 5 mm, 940 nm (z. B. für Fernbedienungen), U_F ca. 1,2–1,5 V",
        f"{c}/Infrarot", "LED 5mm", "led-5mm-ir.svg", [("Durchmesser", "5", "mm"), ("Typ", "IR 940 nm"), ("U_F typ", "1.3", "V")],
        ["ir", "infrarot", "led", "5mm", "940nm"])
    add(g, "tsop4838", "TSOP4838", "IR-Empfänger 38 kHz mit Demodulator für Fernbedienungen (RC5/NEC), 2,5–5,5 V",
        f"{c}/Infrarot", "Sonstiges", "ir-receiver.svg",
        [("Frequenz", "38k", "Hz"), ("Versorgungsspannung", "2.5–5.5", "V")], ["ir", "infrarot", "empfänger", "fernbedienung"],
        manufacturer="Vishay", equivalents=["VS1838B (ähnlich, Pinbelegung prüfen)"])
    add(g, "vs1838b", "VS1838B", "Günstiger IR-Empfänger 38 kHz mit Demodulator für Fernbedienungen",
        f"{c}/Infrarot", "Sonstiges", "ir-receiver.svg", [("Frequenz", "38k", "Hz")], ["ir", "infrarot", "empfänger", "fernbedienung"],
        equivalents=["TSOP4838 (ähnlich, Pinbelegung prüfen)"])
    add(g, "bpw34", "BPW34", "Silizium-PIN-Fotodiode, 7,5 mm² Fläche, 400–1100 nm, schnell",
        f"{c}/Fotosensoren", "THT radial", "photodiode.svg", [("Typ", "PIN-Fotodiode"), ("U_R max", "60", "V")],
        ["fotodiode", "photodiode", "licht", "sensor"], manufacturer="Vishay", datasheet_hint="BPW34")
    add(g, "gl5528", "LDR GL5528", "Fotowiderstand 5 mm, ca. 10–20 kΩ bei 10 lx, Dunkelwiderstand ≥ 1 MΩ",
        f"{c}/Fotosensoren", "THT radial", "ldr.svg", [("Widerstand", "10k–20k", "Ω"), ("Durchmesser", "5", "mm")],
        ["ldr", "fotowiderstand", "licht", "sensor"], datasheet_hint="GL5528")
    add(g, "tcrt5000", "TCRT5000", "Reflexlichtschranke (IR-LED + Fototransistor), Arbeitsbereich ca. 0,2–15 mm",
        f"{c}/Fotosensoren", "Sonstiges", "reflex-sensor.svg", [("Typ", "Reflexkoppler")],
        ["reflex", "lichtschranke", "linienfolger", "ir"], manufacturer="Vishay", datasheet_hint="TCRT5000")
    s = f"{c}/Anzeigen"
    for key, name, desc, hint, typ in (
        ("7seg-056-cc", "7-Segment 0,56\" rot CK", "7-Segment-Anzeige 1-stellig 0,56\" (14,2 mm), rot, gemeinsame Kathode", "5161AS", "gemeinsame Kathode"),
        ("7seg-056-ca", "7-Segment 0,56\" rot CA", "7-Segment-Anzeige 1-stellig 0,56\" (14,2 mm), rot, gemeinsame Anode", "5161BS", "gemeinsame Anode"),
        ("7seg-056-4-cc", "7-Segment 4-stellig 0,56\" CK", "7-Segment-Anzeige 4-stellig 0,56\", rot, gemeinsame Kathode (gemultiplext)", "5641AS", "gemeinsame Kathode"),
        ("7seg-056-4-ca", "7-Segment 4-stellig 0,56\" CA", "7-Segment-Anzeige 4-stellig 0,56\", rot, gemeinsame Anode (gemultiplext)", "5641BS", "gemeinsame Anode"),
    ):
        add(g, key, name, desc, s, "7-Segment", "seven-segment.svg", [("Farbe", "rot"), ("Typ", typ)],
            ["7-segment", "anzeige", "led", "display"], datasheet_hint=hint)
    o = f"{c}/Optokoppler"
    add(g, "pc817", "PC817", "Optokoppler mit Fototransistor-Ausgang, 5 kV Isolation, CTR 50–600 %, DIP-4",
        o, "DIP-4", "dip-4.svg", [("Kanäle", "1"), ("U_CE max", "35", "V")], ["optokoppler", "isolation", "dip-4"],
        equivalents=["EL817", "LTV817"], datasheet_hint="PC817")
    for n in ("4n25", "4n35"):
        add(g, n, n.upper(), "Optokoppler mit Fototransistor-Ausgang und Basisanschluss, DIP-6",
            o, "DIP-6", "dip-6.svg", [("Kanäle", "1")], ["optokoppler", "isolation", "dip-6"],
            equivalents=["4N35" if n == "4n25" else "4N25"], datasheet_hint=n.upper())
    add(g, "6n137", "6N137", "Schneller Optokoppler 10 MBit/s mit Logikausgang, 5 V, DIP-8",
        o, "DIP-8", "dip-8.svg", [("Kanäle", "1"), ("Versorgungsspannung", "5", "V")], ["optokoppler", "highspeed", "midi", "dip-8"],
        datasheet_hint="6N137")
    for n, desc, v in (("moc3021", "Triac-Treiber-Optokoppler 400 V, ohne Nullspannungsschalter (für Phasenanschnitt), DIP-6", "400"),
                       ("moc3041", "Triac-Treiber-Optokoppler 400 V mit Nullspannungsschalter, DIP-6", "400"),
                       ("moc3063", "Triac-Treiber-Optokoppler 600 V mit Nullspannungsschalter, DIP-6", "600")):
        add(g, n, n.upper(), desc, o, "DIP-6", "dip-6.svg", [("Spannung", v, "V")],
            ["optokoppler", "triac", "netz", "230v", "dip-6"], datasheet_hint=n.upper())


# =============================================================== Steckverbinder
def build_connectors():
    g = "steckverbinder"
    c = "Elektronik/Elektromechanik/Steckverbinder"
    sl = f"{c}/Stiftleisten"
    for rows, ang in ((1, False), (1, True), (2, False), (2, True)):
        n = 40 * rows
        form = "gewinkelt" if ang else "gerade"
        add(g, f"pinheader-{rows}x40-{'angled' if ang else 'straight'}", f"Stiftleiste {rows}×40 {form}",
            f"Stiftleiste {rows}×40-polig, Raster 2,54 mm, {form}, abbrechbar",
            sl, "Stiftleiste 2.54", "pin-header.svg",
            [("Polzahl", str(n)), ("Rastermaß", "2.54", "mm"), ("Typ", form)], ["stiftleiste", "pinheader", "2.54mm", form])
    for rows in (1, 2):
        add(g, f"femaleheader-{rows}x40", f"Buchsenleiste {rows}×40 gerade",
            f"Buchsenleiste {rows}×40-polig, Raster 2,54 mm, gerade, kürzbar",
            f"{c}/Buchsenleisten", "Buchsenleiste 2.54", "female-header.svg",
            [("Polzahl", str(40 * rows)), ("Rastermaß", "2.54", "mm")], ["buchsenleiste", "femaleheader", "2.54mm"])
    add(g, "jumper-254", "Jumper 2,54 mm", "Kurzschlussbrücke (Jumper) für Stiftleisten 2,54 mm",
        sl, "Stiftleiste 2.54", "female-header.svg", [("Polzahl", "2"), ("Rastermaß", "2.54", "mm")], ["jumper", "brücke", "2.54mm"])

    xh = f"{c}/JST-XH"
    for n in range(2, 7):
        add(g, f"jst-xh-{n}-header", f"JST-XH {n}-pol Platinenstecker", f"JST-XH Stiftleiste {n}-polig, gerade, Raster 2,5 mm (Platinenseite)",
            xh, "JST-XH", "jst-xh.svg", [("Polzahl", str(n)), ("Rastermaß", "2.5", "mm")],
            ["jst", "jst-xh", "stecker", "platine", "2.5mm"], "JST", f"B{n}B-XH-A", datasheet_hint="JST XH")
        add(g, f"jst-xh-{n}-housing", f"JST-XH {n}-pol Crimpgehäuse", f"JST-XH Buchsengehäuse {n}-polig für Kabel (Crimpkontakte SXH-001T-P0.6 separat)",
            xh, "JST-XH", "jst-xh.svg", [("Polzahl", str(n)), ("Rastermaß", "2.5", "mm")],
            ["jst", "jst-xh", "buchse", "kabel", "crimp", "2.5mm"], "JST", f"XHP-{n}", datasheet_hint="JST XH")
    add(g, "jst-xh-contact", "JST-XH Crimpkontakt", "Crimpkontakt (Buchse) für JST-XH-Gehäuse, AWG 28–22",
        xh, "JST-XH", "dupont.svg", [("Rastermaß", "2.5", "mm")], ["jst", "jst-xh", "crimp", "kontakt"], "JST", "SXH-001T-P0.6")
    ph = f"{c}/JST-PH"
    add(g, "jst-ph-2-header", "JST-PH 2-pol Platinenstecker", "JST-PH Stiftleiste 2-polig, Raster 2,0 mm (typisch für LiPo-Akkus)",
        ph, "JST-PH", "jst-xh.svg", [("Polzahl", "2"), ("Rastermaß", "2", "mm")], ["jst", "jst-ph", "stecker", "lipo", "2mm"], "JST", "B2B-PH-K-S")
    add(g, "jst-ph-2-housing", "JST-PH 2-pol Crimpgehäuse", "JST-PH Buchsengehäuse 2-polig für Kabel, Raster 2,0 mm",
        ph, "JST-PH", "jst-xh.svg", [("Polzahl", "2"), ("Rastermaß", "2", "mm")], ["jst", "jst-ph", "buchse", "kabel", "crimp"], "JST", "PHR-2")
    add(g, "jst-ph-contact", "JST-PH Crimpkontakt", "Crimpkontakt (Buchse) für JST-PH-Gehäuse",
        ph, "JST-PH", "dupont.svg", [("Rastermaß", "2", "mm")], ["jst", "jst-ph", "crimp", "kontakt"], "JST", "SPH-002T-P0.5S")

    du = f"{c}/Dupont"
    add(g, "dupont-contact-f", "Dupont-Crimpkontakt Buchse", "Crimpkontakt weiblich für Dupont-Gehäuse, Raster 2,54 mm",
        du, "Dupont", "dupont.svg", [("Rastermaß", "2.54", "mm")], ["dupont", "crimp", "kontakt", "buchse"])
    add(g, "dupont-contact-m", "Dupont-Crimpkontakt Stecker", "Crimpkontakt männlich (Stift) für Dupont-Gehäuse, Raster 2,54 mm",
        du, "Dupont", "dupont.svg", [("Rastermaß", "2.54", "mm")], ["dupont", "crimp", "kontakt", "stecker"])
    for n in (1, 2, 3, 4, 6):
        add(g, f"dupont-housing-1x{n}", f"Dupont-Gehäuse 1×{n}", f"Dupont-Kunststoffgehäuse 1×{n}-polig, Raster 2,54 mm",
            du, "Dupont", "female-header.svg", [("Polzahl", str(n)), ("Rastermaß", "2.54", "mm")], ["dupont", "gehäuse"])
    for kind, txt in (("mm", "Stecker–Stecker"), ("mf", "Stecker–Buchse"), ("ff", "Buchse–Buchse")):
        add(g, f"jumperwire-{kind}-20cm", f"Jumper-Kabel {txt} 20 cm", f"Steckbrücken/Jumper-Kabel {txt}, 20 cm, Dupont 2,54 mm (Bündel)",
            du, "Dupont", "dupont.svg", [("Rastermaß", "2.54", "mm")], ["jumper", "kabel", "dupont", "steckbrett", "breadboard"])

    st = f"{c}/Schraubklemmen"
    for rm in ("5.08", "3.5"):
        for n in (2, 3):
            add(g, f"screwterm-{rm.replace('.', '')}-{n}", f"Schraubklemme {n}-pol RM {de(rm)}",
                f"Leiterplatten-Schraubklemme {n}-polig, Rastermaß {de(rm)} mm, anreihbar",
                st, "Schraubklemme", "screw-terminal.svg", [("Polzahl", str(n)), ("Rastermaß", rm, "mm")],
                ["schraubklemme", "klemme", "terminal", f"rm{rm}"])

    us = f"{c}/USB"
    add(g, "usb-c-16p", "USB-C-Buchse 16-pol (USB 2.0)", "USB-C-Buchse SMD, 16 Kontakte (USB 2.0 + CC), für Platinenmontage",
        us, "USB-C", "usb-c.svg", [("Polzahl", "16")], ["usb", "usb-c", "buchse", "smd"])
    add(g, "usb-c-6p", "USB-C-Buchse 6-pol (nur Strom)", "USB-C-Buchse, 6 Kontakte (VBUS, GND, CC1, CC2), nur zur Stromversorgung",
        us, "USB-C", "usb-c.svg", [("Polzahl", "6")], ["usb", "usb-c", "buchse", "strom"])
    add(g, "micro-usb-b", "Micro-USB-B-Buchse", "Micro-USB-B-Buchse SMD, 5-polig", us, "Micro-USB", "micro-usb.svg",
        [("Polzahl", "5")], ["usb", "micro-usb", "buchse", "smd"])
    add(g, "mini-usb-b", "Mini-USB-B-Buchse", "Mini-USB-B-Buchse SMD, 5-polig", us, "Mini-USB", "micro-usb.svg",
        [("Polzahl", "5")], ["usb", "mini-usb", "buchse", "smd"])
    add(g, "usb-a-f", "USB-A-Buchse", "USB-A-Buchse für Platinenmontage (THT), 4-polig", us, "USB-A", "usb-a.svg",
        [("Polzahl", "4")], ["usb", "usb-a", "buchse", "tht"])

    au = f"{c}/Strom & Audio"
    add(g, "dc-jack-55-21", "DC-Hohlbuchse 5,5×2,1 mm", "Hohlbuchse für Platinenmontage, 5,5 mm außen / 2,1 mm Stift (z. B. DC-005)",
        au, "DC-Buchse", "dc-jack.svg", [("Durchmesser", "5.5", "mm")], ["hohlbuchse", "dc", "netzteil", "5.5x2.1"])
    add(g, "dc-jack-55-25", "DC-Hohlbuchse 5,5×2,5 mm", "Hohlbuchse für Platinenmontage, 5,5 mm außen / 2,5 mm Stift",
        au, "DC-Buchse", "dc-jack.svg", [("Durchmesser", "5.5", "mm")], ["hohlbuchse", "dc", "netzteil", "5.5x2.5"])
    add(g, "jack-35-stereo", "Klinkenbuchse 3,5 mm stereo", "3,5-mm-Klinkenbuchse stereo (TRS) für Platinenmontage",
        au, "Klinke 3.5", "audio-jack.svg", [("Polzahl", "3")], ["klinke", "audio", "3.5mm", "buchse"])
    add(g, "xt60-m", "XT60 Stecker", "XT60-Hochstromstecker (männlich), 30 A Dauerstrom, verpolsicher (Akkus/RC)",
        au, "XT60", "xt60.svg", [("Strom", "30", "A"), ("Polzahl", "2")], ["xt60", "hochstrom", "akku", "lipo", "stecker"])
    add(g, "xt60-f", "XT60 Buchse", "XT60-Hochstrombuchse (weiblich), 30 A Dauerstrom, verpolsicher (Akkus/RC)",
        au, "XT60", "xt60.svg", [("Strom", "30", "A"), ("Polzahl", "2")], ["xt60", "hochstrom", "akku", "lipo", "buchse"])
    add(g, "banana-4mm-rot", "Bananenbuchse 4 mm rot", "Einbau-Bananenbuchse 4 mm, rot (Laborbuchse)",
        au, "Sonstiges", "generic.svg", [("Farbe", "rot"), ("Durchmesser", "4", "mm")], ["bananenbuchse", "labor", "4mm"])
    add(g, "banana-4mm-schwarz", "Bananenbuchse 4 mm schwarz", "Einbau-Bananenbuchse 4 mm, schwarz (Laborbuchse)",
        au, "Sonstiges", "generic.svg", [("Farbe", "schwarz"), ("Durchmesser", "4", "mm")], ["bananenbuchse", "labor", "4mm"])

    nw = f"{c}/Netzwerk"
    add(g, "rj45-8p8c", "RJ45-Buchse 8P8C", "RJ45-Netzwerkbuchse 8P8C für Platinenmontage, ohne Übertrager",
        nw, "RJ45", "rj45.svg", [("Polzahl", "8")], ["rj45", "ethernet", "netzwerk", "lan"])
    add(g, "rj45-hr911105a", "RJ45 MagJack HR911105A", "RJ45-Buchse mit integriertem Übertrager und LEDs (z. B. für ENC28J60)",
        nw, "RJ45", "rj45.svg", [("Polzahl", "8")], ["rj45", "ethernet", "magjack", "übertrager"], "HanRun", "HR911105A")

    so = f"{c}/IC-Sockel"
    for n in (8, 14, 16, 18, 20, 24, 28, 40):
        width = "breit (15,24 mm)" if n == 40 else "schmal (7,62 mm)"
        add(g, f"socket-dip{n}", f"IC-Sockel DIP-{n}", f"IC-Fassung DIP-{n}, {width}, Federkontakte",
            so, f"DIP-{n}", "ic-socket.svg", [("Polzahl", str(n)), ("Rastermaß", "2.54", "mm")], ["ic-sockel", "fassung", f"dip-{n}"])
    add(g, "socket-dip28-wide", "IC-Sockel DIP-28 breit", "IC-Fassung DIP-28, breit (15,24 mm), z. B. für EPROMs/RAMs",
        so, "DIP-28", "ic-socket.svg", [("Polzahl", "28"), ("Rastermaß", "2.54", "mm")], ["ic-sockel", "fassung", "dip-28", "breit"])
    add(g, "socket-dip24-wide", "IC-Sockel DIP-24 breit", "IC-Fassung DIP-24, breit (15,24 mm)",
        so, "DIP-24", "ic-socket.svg", [("Polzahl", "24"), ("Rastermaß", "2.54", "mm")], ["ic-sockel", "fassung", "dip-24", "breit"])

    wg = f"{c}/Verbindungsklemmen"
    for n in (2, 3, 5):
        add(g, f"wago-221-41{n}", f"WAGO 221-41{n}", f"Verbindungsklemme mit Hebeln, {n}-Leiter, 0,2–4 mm², 32 A / 450 V",
            wg, "Wago", "wago.svg", [("Polzahl", str(n)), ("Strom", "32", "A"), ("Spannung", "450", "V")],
            ["wago", "hebelklemme", "verbindungsklemme"], "WAGO", f"221-41{n}", datasheet_hint="WAGO 221")

    kk = f"{c}/Molex KK"
    for n in (2, 3, 4):
        add(g, f"molex-kk254-{n}", f"Molex KK 254 {n}-pol Stiftleiste", f"Molex-KK-254-Platinenstecker {n}-polig mit Verriegelung, Raster 2,54 mm",
            kk, "Molex KK", "jst-xh.svg", [("Polzahl", str(n)), ("Rastermaß", "2.54", "mm")], ["molex", "kk", "stecker", "2.54mm"], "Molex")

    idc = f"{c}/Wannen- & Pfostenstecker"
    for n in (10, 20):
        add(g, f"boxheader-2x{n // 2}", f"Wannenstecker {n}-pol", f"Wannenstecker 2×{n // 2}-polig, Raster 2,54 mm, gerade, mit Kodiernase",
            idc, "IDC", "idc.svg", [("Polzahl", str(n)), ("Rastermaß", "2.54", "mm")], ["wannenstecker", "idc", "flachband"])
        add(g, f"idc-socket-{n}", f"Pfostenbuchse {n}-pol (IDC)", f"Pfostenbuchse 2×{n // 2}-polig zum Aufquetschen auf Flachbandkabel (1,27 mm)",
            idc, "IDC", "idc.svg", [("Polzahl", str(n)), ("Rastermaß", "2.54", "mm")], ["pfostenbuchse", "idc", "flachband"])


# =============================================================== Schalter & Taster
def build_switches():
    g = "schalter_taster"
    t = "Elektronik/Elektromechanik/Schalter & Taster/Taster"
    for h in ("5", "9.5", "13"):
        add(g, f"tact-6x6x{h.replace('.', '')}", f"Taster 6×6×{de(h)} mm", f"Kurzhubtaster 6×6 mm, Höhe {de(h)} mm, 4 Pins THT, Schließer",
            t, "Taster", "button-6x6.svg", [("Polzahl", "4")], ["taster", "kurzhubtaster", "tact", "6x6"])
    add(g, "tact-12x12", "Taster 12×12 mm", "Kurzhubtaster 12×12 mm, 4 Pins THT, oft mit aufsteckbarer farbiger Kappe",
        t, "Taster", "button-12x12.svg", [("Polzahl", "4")], ["taster", "kurzhubtaster", "tact", "12x12"])
    add(g, "tact-smd-3x6", "SMD-Taster 3×6 mm", "Kurzhubtaster SMD 3×6×2,5 mm, 2 Anschlüsse",
        t, "Taster", "button-smd.svg", [("Polzahl", "2")], ["taster", "smd", "tact", "3x6"])
    s = "Elektronik/Elektromechanik/Schalter & Taster/Schalter"
    add(g, "mts-102", "Kippschalter MTS-102 (EIN-EIN)", "Mini-Kippschalter 1× Um (EIN-EIN), 3 Lötanschlüsse, Einbau 6 mm",
        s, "Schalter", "toggle-switch.svg", [("Typ", "EIN-EIN"), ("Polzahl", "3")], ["kippschalter", "toggle", "mts-102", "spdt"])
    add(g, "mts-103", "Kippschalter MTS-103 (EIN-AUS-EIN)", "Mini-Kippschalter 1× Um mit Mittelstellung (EIN-AUS-EIN), Einbau 6 mm",
        s, "Schalter", "toggle-switch.svg", [("Typ", "EIN-AUS-EIN"), ("Polzahl", "3")], ["kippschalter", "toggle", "mts-103", "spdt"])
    add(g, "mts-202", "Kippschalter MTS-202 (2× EIN-EIN)", "Mini-Kippschalter 2× Um (EIN-EIN), 6 Lötanschlüsse, Einbau 6 mm",
        s, "Schalter", "toggle-switch.svg", [("Typ", "EIN-EIN"), ("Polzahl", "6")], ["kippschalter", "toggle", "mts-202", "dpdt"])
    add(g, "ss12d00", "Schiebeschalter SS12D00", "Miniatur-Schiebeschalter 1× Um, 3 Pins, Raster 2,54 mm, stehend",
        s, "Schalter", "slide-switch.svg", [("Typ", "1× Um"), ("Polzahl", "3"), ("Rastermaß", "2.54", "mm")], ["schiebeschalter", "slide", "spdt"])
    add(g, "kcd1-101", "Wippschalter KCD1-101", "Einbau-Wippschalter EIN-AUS, 2 Flachstecker, 6 A / 250 V~, Ausschnitt ca. 21×15 mm",
        s, "Schalter", "rocker-switch.svg", [("Typ", "EIN-AUS"), ("Polzahl", "2")], ["wippschalter", "netzschalter", "rocker"])
    for n in (4, 8):
        add(g, f"dipswitch-{n}", f"DIP-Schalter {n}-fach", f"DIP-Schalter {n}-fach, Rastermaß 2,54 mm, passt in DIP-{2 * n}-Raster",
            s, "Schalter", "dip-switch.svg", [("Kanäle", str(n)), ("Rastermaß", "2.54", "mm")], ["dip-schalter", "codierschalter"])
    add(g, "kw11-3z", "Mikroschalter KW11-3Z mit Hebel", "Mikroschalter (Endschalter) mit Rollenhebel, 1× Um, 5 A / 250 V~",
        s, "Schalter", "microswitch.svg", [("Typ", "1× Um"), ("Polzahl", "3")], ["mikroschalter", "endschalter", "3d-drucker", "cnc"])
    e = "Elektronik/Elektromechanik/Schalter & Taster/Drehgeber"
    add(g, "ec11", "Drehencoder EC11", "Inkrementaler Drehgeber (Encoder) mit Rastung und Druckfunktion, 5 Pins, Achse 6 mm",
        e, "Drehgeber", "encoder.svg", [("Polzahl", "5")], ["encoder", "drehgeber", "inkrementalgeber", "ec11"], datasheet_hint="Alps EC11")
    add(g, "ky-040", "Drehencoder-Modul KY-040", "Drehgeber-Modul mit EC11-Encoder und Pull-up-Widerständen, Pins CLK/DT/SW/+/GND",
        e, "Modul", "encoder.svg", [("Polzahl", "5")], ["encoder", "drehgeber", "modul", "ky-040"])


# =============================================================== Quarze
def build_crystals():
    g = "quarze"
    c = "Elektronik/Passiv/Quarze & Resonatoren"
    for f in ("4", "8", "11.0592", "12", "16", "20", "25"):
        add(g, f"xtal-hc49s-{f.replace('.', '')}m", f"Quarz {de(f)} MHz HC-49/S", f"Quarz {de(f)} MHz, Bauform HC-49/S (flach), 2 Pins",
            f"{c}/Quarze", "HC-49/S", "crystal-hc49.svg", [("Frequenz", f"{f}M", "Hz")], ["quarz", "crystal", "hc-49", f"{f}mhz"])
    add(g, "xtal-32768", "Uhrenquarz 32,768 kHz", "Uhrenquarz 32,768 kHz, zylindrisch 2×6 mm, für RTC (z. B. DS1307)",
        f"{c}/Quarze", "Uhrenquarz", "crystal-watch.svg", [("Frequenz", "32.768k", "Hz")], ["quarz", "uhrenquarz", "rtc", "32khz"])
    for f in ("8", "16"):
        add(g, f"resonator-{f}m", f"Keramikresonator {f} MHz", f"Keramikresonator {f} MHz mit integrierten Lastkondensatoren, 3 Pins (Mitte = GND)",
            f"{c}/Resonatoren", "THT radial", "resonator.svg", [("Frequenz", f"{f}M", "Hz")], ["resonator", "keramik", f"{f}mhz"])


# =============================================================== Sicherungen
def build_fuses():
    g = "sicherungen"
    c = "Elektronik/Schutz/Sicherungen"
    for kind, word, letter in (("f", "flink", "F"), ("t", "träge", "T")):
        for a in ("0.5", "1", "2", "3.15", "5"):
            add(g, f"fuse-5x20-{kind}-{a.replace('.', '')}a", f"Feinsicherung 5×20 {letter} {de(a)} A",
                f"Glasrohr-Feinsicherung 5×20 mm, {word} ({letter}), {de(a)} A, 250 V",
                f"{c}/Feinsicherungen 5x20", "Sicherung 5x20", "fuse-5x20.svg",
                [("Strom", a, "A"), ("Spannung", "250", "V"), ("Typ", word)], ["sicherung", "feinsicherung", "5x20", word])
    add(g, "fuseholder-5x20-pcb", "Sicherungshalter 5×20 Platine", "Printsicherungshalter für 5×20-mm-Sicherungen mit Abdeckkappe",
        f"{c}/Sicherungshalter", "Sicherung 5x20", "fuse-holder.svg", [], ["sicherungshalter", "5x20", "platine"])
    add(g, "fuseholder-5x20-panel", "Sicherungshalter 5×20 Einbau", "Einbau-Sicherungshalter für 5×20-mm-Sicherungen mit Schraub-/Bajonettkappe",
        f"{c}/Sicherungshalter", "Sicherung 5x20", "fuse-holder.svg", [], ["sicherungshalter", "5x20", "einbau", "gehäuse"])
    for a in ("0.5", "1"):
        add(g, f"ptc-1206-{a.replace('.', '')}a", f"PTC-Sicherung 1206 {de(a)} A", f"Rückstellende SMD-Sicherung (PolySwitch), Haltestrom {de(a)} A, Bauform 1206",
            f"{c}/PTC", "1206", "smd-chip-generic.svg", [("Strom", a, "A"), ("Typ", "PTC rückstellend")], ["ptc", "polyfuse", "rückstellend", "smd", "1206"])
    for a in ("0.5", "1", "2"):
        add(g, f"ptc-tht-{a.replace('.', '')}a", f"PTC-Sicherung THT {de(a)} A", f"Rückstellende Sicherung (PolySwitch), bedrahtet, Haltestrom {de(a)} A",
            f"{c}/PTC", "THT radial", "ptc.svg", [("Strom", a, "A"), ("Typ", "PTC rückstellend")], ["ptc", "polyfuse", "rückstellend", "tht"])


# =============================================================== Relais
def build_relays():
    g = "relais"
    c = "Elektronik/Elektromechanik/Relais"
    for v in (5, 12):
        add(g, f"srd-{v:02d}vdc-sl-c", f"SRD-{v:02d}VDC-SL-C", f"Leistungsrelais {v} V DC, 1 Wechsler, 10 A / 250 V~ (Songle)",
            f"{c}/Leistungsrelais", "Relais", "relay.svg",
            [("Spulenspannung", v, "V"), ("Kontakte", "1 Wechsler"), ("Strom", "10", "A")],
            ["relais", "srd", "songle", f"{v}v"], "Songle", f"SRD-{v:02d}VDC-SL-C")
    add(g, "finder-40-52", "Finder 40.52", "Printrelais 2 Wechsler, 8 A / 250 V~, Spulenspannung je nach Ausführung (Aufdruck prüfen)",
        f"{c}/Leistungsrelais", "Relais", "relay.svg", [("Kontakte", "2 Wechsler"), ("Strom", "8", "A")],
        ["relais", "finder", "dpdt"], "Finder", datasheet_hint="Finder 40.52")
    add(g, "hk19f-dc5v", "HK19F-DC5V-SHG", "Signalrelais 5 V DC, 2 Wechsler, DIP-ähnliches Gehäuse (Huike)",
        f"{c}/Signalrelais", "Relais", "relay-sil.svg", [("Spulenspannung", "5", "V"), ("Kontakte", "2 Wechsler")],
        ["relais", "signalrelais", "dpdt", "5v"], "Huike", "HK19F-DC5V-SHG")
    add(g, "sil05-1a72", "SIL05-1A72", "Reed-Relais 5 V DC, 1 Schließer, SIL-4-Gehäuse (Varianten mit Freilaufdiode: -71D)",
        f"{c}/Reed-Relais", "SIL-4", "relay-sil.svg", [("Spulenspannung", "5", "V"), ("Kontakte", "1 Schließer")],
        ["relais", "reed", "sil", "5v"], datasheet_hint="SIL05-1A72")
    add(g, "g3mb-202p", "G3MB-202P", "Halbleiterrelais (SSR) 2 A, 100–240 V~, Steuerspannung 5 V, Nullspannungsschaltend, nur für Wechselstrom",
        f"{c}/Halbleiterrelais", "SIL-4", "relay-sil.svg", [("Spulenspannung", "5", "V"), ("Strom", "2", "A")],
        ["relais", "ssr", "halbleiterrelais", "230v"], "Omron", "G3MB-202P")


# =============================================================== Sensoren
def build_sensors():
    g = "sensoren"
    c = "Elektronik/Sensoren"
    T = f"{c}/Temperatur & Klima"
    add(g, "ntc-10k-3950", "NTC 10 kΩ B3950", "Heißleiter (NTC-Thermistor) 10 kΩ bei 25 °C, B-Wert 3950 K, bedrahtet",
        T, "THT radial", "ntc.svg", [("Widerstand", "10k", "Ω"), ("Typ", "NTC B3950")], ["ntc", "thermistor", "temperatur"])
    add(g, "ntc-10k-3950-probe", "NTC 10 kΩ B3950 Fühler", "NTC-Temperaturfühler 10 kΩ / B3950 in Edelstahlhülse mit Kabel (z. B. 3D-Drucker)",
        T, "Sonstiges", "ntc.svg", [("Widerstand", "10k", "Ω"), ("Typ", "NTC B3950")], ["ntc", "thermistor", "temperatur", "fühler"])
    add(g, "lm35dz", "LM35DZ", "Analoger Temperatursensor 10 mV/°C, 0–100 °C, 4–30 V, TO-92",
        T, "TO-92", "to-92.svg", [("Versorgungsspannung", "4–30", "V"), ("Typ", "analog 10 mV/°C")], ["temperatur", "lm35", "analog"], datasheet_hint="LM35")
    add(g, "tmp36", "TMP36", "Analoger Temperatursensor 10 mV/°C (750 mV bei 25 °C), −40 bis +125 °C, 2,7–5,5 V, TO-92",
        T, "TO-92", "to-92.svg", [("Versorgungsspannung", "2.7–5.5", "V"), ("Typ", "analog 10 mV/°C")], ["temperatur", "tmp36", "analog"],
        manufacturer="Analog Devices", datasheet_hint="TMP36")
    add(g, "ds18b20", "DS18B20", "Digitaler 1-Wire-Temperatursensor −55 bis +125 °C, ±0,5 °C, 3,0–5,5 V, TO-92",
        T, "TO-92", "to-92.svg", [("Versorgungsspannung", "3–5.5", "V"), ("Schnittstelle", "1-Wire")], ["temperatur", "ds18b20", "1-wire", "onewire"],
        manufacturer="Analog Devices (Maxim)", datasheet_hint="DS18B20")
    add(g, "ds18b20-probe", "DS18B20 wasserdicht", "DS18B20 in Edelstahlhülse mit Kabel (ca. 1 m), wasserdicht, 1-Wire",
        T, "Sonstiges", "ntc.svg", [("Schnittstelle", "1-Wire")], ["temperatur", "ds18b20", "1-wire", "fühler", "wasserdicht"])
    add(g, "dht11", "DHT11", "Temperatur-/Feuchtesensor 0–50 °C (±2 °C), 20–90 % rF (±5 %), Eindraht-Protokoll, 3,3–5,5 V",
        T, "Sonstiges", "dht.svg", [("Versorgungsspannung", "3.3–5.5", "V"), ("Schnittstelle", "Eindraht (proprietär)")],
        ["temperatur", "feuchte", "dht11", "klima"], datasheet_hint="DHT11")
    add(g, "dht22", "DHT22 / AM2302", "Temperatur-/Feuchtesensor −40 bis +80 °C (±0,5 °C), 0–100 % rF (±2 %), Eindraht-Protokoll, 3,3–6 V",
        T, "Sonstiges", "dht.svg", [("Versorgungsspannung", "3.3–6", "V"), ("Schnittstelle", "Eindraht (proprietär)")],
        ["temperatur", "feuchte", "dht22", "am2302", "klima"], datasheet_hint="AM2302")
    add(g, "bme280", "BME280-Modul", "Sensor für Temperatur, Luftfeuchte und Luftdruck (300–1100 hPa), I²C/SPI; Chip 1,71–3,6 V (Modul teils mit Regler)",
        T, "Modul", "sensor.svg", [("Schnittstelle", "I²C, SPI")], ["temperatur", "feuchte", "luftdruck", "bme280", "bosch", "klima"],
        manufacturer="Bosch Sensortec", datasheet_hint="BME280", equivalents=["BMP280 (ohne Feuchte)"])
    add(g, "bmp280", "BMP280-Modul", "Sensor für Luftdruck (300–1100 hPa) und Temperatur, I²C/SPI, 3,3 V",
        T, "Modul", "sensor.svg", [("Schnittstelle", "I²C, SPI")], ["luftdruck", "barometer", "bmp280", "bosch"],
        manufacturer="Bosch Sensortec", datasheet_hint="BMP280", equivalents=["BME280 (mit Feuchte)"])
    add(g, "max6675", "MAX6675-Modul", "Thermoelement-Wandler für Typ K, 0–1024 °C, 0,25 °C Auflösung, SPI, meist mit Fühler",
        T, "Modul", "sensor.svg", [("Schnittstelle", "SPI")], ["thermoelement", "typ-k", "max6675", "temperatur"], datasheet_hint="MAX6675")
    B = f"{c}/Bewegung & Abstand"
    add(g, "mpu6050", "MPU-6050-Modul (GY-521)", "6-Achsen-Bewegungssensor (3 Achsen Beschleunigung + 3 Achsen Gyroskop), I²C",
        B, "Modul", "sensor.svg", [("Schnittstelle", "I²C")], ["imu", "gyroskop", "beschleunigung", "mpu6050", "gy-521"], datasheet_hint="MPU-6050")
    add(g, "hc-sr04", "HC-SR04", "Ultraschall-Entfernungssensor 2–400 cm, 5 V, Trigger/Echo",
        B, "Modul", "ultrasonic.svg", [("Versorgungsspannung", "5", "V"), ("Frequenz", "40k", "Hz")], ["ultraschall", "entfernung", "abstand", "hc-sr04"])
    add(g, "vl53l0x", "VL53L0X-Modul", "Laser-Laufzeit-Entfernungssensor (ToF) bis ca. 2 m, I²C",
        B, "Modul", "sensor.svg", [("Schnittstelle", "I²C")], ["tof", "laser", "entfernung", "abstand", "vl53l0x"], manufacturer="STMicroelectronics")
    add(g, "hc-sr501", "PIR HC-SR501", "Passiv-Infrarot-Bewegungsmelder, 4,5–20 V, 3,3-V-Ausgang, Haltezeit und Empfindlichkeit einstellbar",
        B, "Modul", "pir.svg", [("Versorgungsspannung", "4.5–20", "V")], ["pir", "bewegungsmelder", "hc-sr501"])
    add(g, "a3144", "A3144", "Hall-Schalter (unipolar), Open-Collector-Ausgang, 4,5–24 V, flaches TO-92-Gehäuse",
        f"{c}/Magnetfeld", "TO-92", "to-92.svg", [("Versorgungsspannung", "4.5–24", "V"), ("Typ", "Hall-Schalter")], ["hall", "magnet", "a3144"])
    add(g, "reed-contact", "Reedkontakt Schließer", "Reedkontakt im Glasröhrchen, Schließer, schaltet bei Magnetannäherung (Glas bruchempfindlich)",
        f"{c}/Magnetfeld", "THT axial", "reed.svg", [("Typ", "Schließer")], ["reed", "magnet", "kontakt"])
    E = f"{c}/Strom & Kraft"
    add(g, "ina219", "INA219-Modul", "Strom-/Spannungs-/Leistungsmonitor, Busspannung bis 26 V, I²C; mit 0,1-Ω-Shunt bis ±3,2 A",
        E, "Modul", "sensor.svg", [("Schnittstelle", "I²C"), ("Eingangsspannung max", "26", "V")], ["strom", "leistung", "ina219", "messung"], datasheet_hint="INA219")
    for a, s in (("5", "185"), ("20", "100"), ("30", "66")):
        add(g, f"acs712-{a}a", f"ACS712-{a}A-Modul", f"Hall-Stromsensor ±{a} A, galvanisch getrennt, analog {s} mV/A, 5 V",
            E, "Modul", "sensor.svg", [("Strom", a, "A"), ("Versorgungsspannung", "5", "V")], ["strom", "hall", "acs712"],
            manufacturer="Allegro", datasheet_hint="ACS712")
    add(g, "hx711", "HX711-Modul", "24-Bit-A/D-Wandler für Wägezellen (Brückenschaltung), 2,6–5,5 V, 10/80 Messungen/s",
        E, "Modul", "sensor.svg", [("Versorgungsspannung", "2.6–5.5", "V")], ["waage", "wägezelle", "hx711", "adc"], datasheet_hint="HX711")
    L = f"{c}/Licht"
    add(g, "bh1750", "BH1750-Modul", "Digitaler Umgebungslichtsensor 1–65535 lx, I²C",
        L, "Modul", "sensor.svg", [("Schnittstelle", "I²C")], ["licht", "lux", "bh1750"], manufacturer="Rohm", datasheet_hint="BH1750")


# =============================================================== DDR
def build_ddr():
    g = "ddr"
    c = "Elektronik/DDR-Bauelemente"
    ics = [
        ("b555d", "B555D", "DDR-Timer-IC (HFO), Äquivalent zum NE555, DIP-8", "Timer", "DIP-8", ["NE555", "LM555"]),
        ("d100d", "D100D", "DDR-TTL: 4× NAND mit 2 Eingängen, Äquivalent zu SN7400, DIP-14", "TTL", "DIP-14", ["SN7400", "MH7400 (Tesla)"]),
        ("d103d", "D103D", "DDR-TTL: 4× NAND mit 2 Eingängen, Open-Collector, Äquivalent zu SN7403, DIP-14", "TTL", "DIP-14", ["SN7403"]),
        ("d104d", "D104D", "DDR-TTL: 6× Inverter, Äquivalent zu SN7404, DIP-14", "TTL", "DIP-14", ["SN7404"]),
        ("d110d", "D110D", "DDR-TTL: 3× NAND mit 3 Eingängen, Äquivalent zu SN7410, DIP-14", "TTL", "DIP-14", ["SN7410"]),
        ("d120d", "D120D", "DDR-TTL: 2× NAND mit 4 Eingängen, Äquivalent zu SN7420, DIP-14", "TTL", "DIP-14", ["SN7420"]),
        ("d130d", "D130D", "DDR-TTL: NAND mit 8 Eingängen, Äquivalent zu SN7430, DIP-14", "TTL", "DIP-14", ["SN7430"]),
        ("d140d", "D140D", "DDR-TTL: 2× NAND-Leistungsgatter mit 4 Eingängen, Äquivalent zu SN7440, DIP-14", "TTL", "DIP-14", ["SN7440"]),
        ("d174d", "D174D", "DDR-TTL: 2× D-Flipflop, Äquivalent zu SN7474, DIP-14", "TTL", "DIP-14", ["SN7474"]),
        ("d190d", "D190D", "DDR-TTL: Dezimalzähler, Äquivalent zu SN7490, DIP-14", "TTL", "DIP-14", ["SN7490"]),
        ("d192d", "D192D", "DDR-TTL: synchroner BCD-Vor-/Rückwärtszähler, Äquivalent zu SN74192, DIP-16", "TTL", "DIP-16", ["SN74192"]),
        ("d193d", "D193D", "DDR-TTL: synchroner 4-Bit-Binär-Vor-/Rückwärtszähler, Äquivalent zu SN74193, DIP-16", "TTL", "DIP-16", ["SN74193"]),
        ("d195d", "D195D", "DDR-TTL: 4-Bit-Schieberegister, Äquivalent zu SN7495, DIP-14", "TTL", "DIP-14", ["SN7495"]),
        ("dl000d", "DL000D", "DDR-Low-Power-Schottky-TTL: 4× NAND, Äquivalent zu 74LS00, DIP-14", "TTL", "DIP-14", ["74LS00"]),
        ("dl004d", "DL004D", "DDR-Low-Power-Schottky-TTL: 6× Inverter, Äquivalent zu 74LS04, DIP-14", "TTL", "DIP-14", ["74LS04"]),
        ("dl074d", "DL074D", "DDR-Low-Power-Schottky-TTL: 2× D-Flipflop, Äquivalent zu 74LS74, DIP-14", "TTL", "DIP-14", ["74LS74"]),
        ("a109d", "A109D", "DDR-Operationsverstärker, Äquivalent zum µA709 (externe Frequenzkompensation nötig); Gehäuse prüfen", "Analog", "DIP-14", ["µA709", "LM709"]),
        ("b081d", "B081D", "DDR-JFET-Operationsverstärker, Äquivalent zum TL081, DIP-8", "Analog", "DIP-8", ["TL081", "TL071 (rauschärmer)"]),
        ("b082d", "B082D", "DDR-Doppel-JFET-Operationsverstärker, Äquivalent zum TL082, DIP-8", "Analog", "DIP-8", ["TL082", "TL072 (rauschärmer)"]),
        ("b084d", "B084D", "DDR-Vierfach-JFET-Operationsverstärker, Äquivalent zum TL084, DIP-14", "Analog", "DIP-14", ["TL084", "TL074 (rauschärmer)"]),
        ("a277d", "A277D", "DDR-Treiber für LED-Punkt-/Bandanzeigen (12 LEDs); funktional ähnlich LM3914, NICHT pinkompatibel; DIP-18", "Analog", "DIP-18",
         ["LM3914 (ähnlich, NICHT pinkompatibel!)"]),
        ("a2030", "A2030", "DDR-NF-Leistungsverstärker, Äquivalent zum TDA2030; Gehäuse (H/V-Ausführung) prüfen", "Analog", "TO-220-5", ["TDA2030"]),
        ("b3170v", "B3170V", "DDR-Einstellregler 1,2–37 V, Äquivalent zum LM317, TO-220", "Spannungsregler", "TO-220", ["LM317T"]),
        ("maa723", "MAA723", "Tesla (ČSSR) Präzisions-Spannungsregler, Äquivalent zum µA723; Gehäuse prüfen", "Analog", "DIP-14", ["µA723", "LM723"]),
        ("maa741", "MAA741", "Tesla (ČSSR) Operationsverstärker, Äquivalent zum µA741; Gehäuse prüfen", "Analog", "DIP-8", ["µA741", "LM741"]),
        ("ma7805", "MA7805", "Tesla (ČSSR) Festspannungsregler +5 V, Äquivalent zum 7805, TO-220", "Spannungsregler", "TO-220", ["7805", "L7805CV"]),
        ("mh7400", "MH7400", "Tesla (ČSSR) TTL: 4× NAND mit 2 Eingängen, Äquivalent zu SN7400, DIP-14", "TTL", "DIP-14", ["SN7400", "D100D (DDR)"]),
        ("u880d", "U880D", "DDR-8-Bit-CPU, Äquivalent zum Zilog Z80 (Z80-CPU), DIP-40", "Mikroprozessoren", "DIP-40", ["Z80 CPU (Z8400)"]),
        ("u855d", "U855D", "DDR-Parallel-I/O-Baustein, Äquivalent zum Z80 PIO, DIP-40", "Mikroprozessoren", "DIP-40", ["Z80 PIO (Z8420)"]),
        ("u856d", "U856D", "DDR-Serieller I/O-Baustein, Äquivalent zum Z80 SIO, DIP-40", "Mikroprozessoren", "DIP-40", ["Z80 SIO (Z8440)"]),
        ("u857d", "U857D", "DDR-Zähler/Zeitgeber-Baustein, Äquivalent zum Z80 CTC, DIP-28", "Mikroprozessoren", "DIP-28", ["Z80 CTC (Z8430)"]),
        ("u8272d", "U8272D", "DDR-Diskettencontroller, Äquivalent zum Intel 8272 (µPD765), DIP-40", "Mikroprozessoren", "DIP-40", ["i8272", "µPD765"]),
        ("u2164d", "U2164D", "DDR-DRAM 64 k × 1 Bit, entspricht dem 4164, DIP-16", "Speicher", "DIP-16", ["4164 (ähnlich)"]),
    ]
    for key, name, desc, sub, fp, eq in ics:
        img = {"TO-220": "to-220.svg", "TO-220-5": "to-220-5.svg"}.get(fp, f"{fp.lower()}.svg")
        add(g, f"ddr-{key}", name, desc, f"{c}/ICs/{sub}", fp, img, [], ["ddr", "rft", "ostblock", sub.lower()],
            equivalents=eq, datasheet_hint=name)
    tr = [
        ("sc237", "SC237", "NPN", "DDR-Si-NPN-Kleinsignaltransistor der BC237-Klasse; Gehäuse und Pinbelegung prüfen", "TO-92", ["BC237 (ähnlich)", "BC547 (ähnlich, Pinbelegung prüfen)"]),
        ("sc238", "SC238", "NPN", "DDR-Si-NPN-Kleinsignaltransistor der BC238-Klasse; Gehäuse und Pinbelegung prüfen", "TO-92", ["BC238 (ähnlich)", "BC548 (ähnlich, Pinbelegung prüfen)"]),
        ("sc239", "SC239", "NPN", "DDR-Si-NPN-Kleinsignaltransistor der BC239-Klasse (rauscharm); Gehäuse und Pinbelegung prüfen", "TO-92", ["BC239 (ähnlich)", "BC549 (ähnlich, Pinbelegung prüfen)"]),
        ("sc307", "SC307", "PNP", "DDR-Si-PNP-Kleinsignaltransistor der BC307-Klasse; Gehäuse und Pinbelegung prüfen", "TO-92", ["BC307 (ähnlich)", "BC557 (ähnlich, Pinbelegung prüfen)"]),
        ("sc308", "SC308", "PNP", "DDR-Si-PNP-Kleinsignaltransistor der BC308-Klasse; Gehäuse und Pinbelegung prüfen", "TO-92", ["BC308 (ähnlich)", "BC558 (ähnlich, Pinbelegung prüfen)"]),
        ("kc507", "KC507", "NPN", "Tesla (ČSSR) Si-NPN-Kleinsignaltransistor, Äquivalent zum BC107, Metallgehäuse TO-18", "TO-18", ["BC107"]),
        ("kc508", "KC508", "NPN", "Tesla (ČSSR) Si-NPN-Kleinsignaltransistor, Äquivalent zum BC108, Metallgehäuse TO-18", "TO-18", ["BC108"]),
        ("kc509", "KC509", "NPN", "Tesla (ČSSR) Si-NPN-Kleinsignaltransistor, Äquivalent zum BC109 (rauscharm), Metallgehäuse TO-18", "TO-18", ["BC109"]),
        ("kd503", "KD503", "NPN", "Tesla (ČSSR) Si-NPN-Leistungstransistor der 2N3055-Klasse, TO-3", "TO-3", ["2N3055 (ähnlich)"]),
    ]
    for key, name, pol, desc, fp, eq in tr:
        add(g, f"ddr-{key}", name, desc, f"{c}/Transistoren/{pol}", fp, f"{fp.lower()}.svg", [("Typ", pol)],
            ["ddr", "tesla" if name.startswith("K") else "rft", "ostblock", "transistor", pol.lower()], equivalents=eq, datasheet_hint=name)
    for v in ("5.1", "6.2", "9.1", "12"):
        add(g, f"ddr-szx21-{v.replace('.', 'v')}", f"SZX21/{de(v)}", f"DDR-Z-Diode {de(v)} V (Reihe SZX 21); Belastbarkeit/Gehäuse prüfen",
            f"{c}/Dioden", "DO-35", "do-35.svg", [("Typ", "Z-Diode"), ("U_Z", v, "V")], ["ddr", "rft", "z-diode", "zener"],
            equivalents=[f"BZX55C{v.replace('.', 'V')} (ähnlich, Leistung prüfen)"])
    add(g, "ddr-vqa13", "VQA13", "DDR-Leuchtdiode rot (RFT); Bauform/Durchmesser prüfen", f"{c}/Optoelektronik", "LED 5mm", "led-5mm-rot.svg",
        [("Farbe", "rot")], ["ddr", "rft", "led", "rot"], equivalents=["Standard-LED rot (ähnlich)"])
    add(g, "ddr-vqa23", "VQA23", "DDR-Leuchtdiode grün (RFT); Bauform/Durchmesser prüfen", f"{c}/Optoelektronik", "LED 5mm", "led-5mm-gruen.svg",
        [("Farbe", "grün")], ["ddr", "rft", "led", "grün"], equivalents=["Standard-LED grün (ähnlich)"])
    add(g, "ddr-vqe24", "VQE24", "DDR-7-Segment-LED-Anzeige (RFT); Polarität und Pinbelegung vor Einsatz prüfen", f"{c}/Optoelektronik", "7-Segment",
        "seven-segment.svg", [], ["ddr", "rft", "7-segment", "anzeige"])
    for val, volt in (("10u", "40"), ("47u", "40"), ("100u", "25"), ("470u", "25")):
        add(g, f"ddr-frolyt-{val}-{volt}v", f"Frolyt-Elko {cap_de(val)} {volt} V",
            f"DDR-Aluminium-Elektrolytkondensator (RFT/Frolyt) {cap_de(val)} {volt} V; alte Elkos vor Einsatz prüfen/formieren, Bauform (axial/radial) prüfen",
            f"{c}/Passiv", "Elko radial", "elko.svg", [("Kapazität", val, "F"), ("Spannung", volt, "V")],
            ["ddr", "rft", "frolyt", "elko", "kondensator"])
    add(g, "ddr-rft-relais-tgl", "RFT-Relais (TGL)", "DDR-Kleinrelais nach TGL-Norm (RFT), generischer Eintrag; Spulenspannung und Kontaktbestückung am Typenschild prüfen",
        f"{c}/Relais", "Relais", "relay.svg", [], ["ddr", "rft", "relais", "tgl"])


# =============================================================== Build & Validierung
def main():
    for fn in (build_resistors, build_caps, build_inductors, build_diodes, build_transistors, build_regulators,
               build_logic, build_analog, build_mcu, build_modules, build_leds, build_opto, build_connectors,
               build_switches, build_crystals, build_fuses, build_relays, build_sensors, build_ddr):
        fn()

    errors = []
    seen = {}
    key_re = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
    for grp, parts in GROUPS.items():
        for p in parts:
            k = p["key"]
            if k in seen:
                errors.append(f"doppelter Key '{k}' in {grp} und {seen[k]}")
            seen[k] = grp
            if not key_re.match(k):
                errors.append(f"ungültiger Key '{k}'")
            if p["footprint"] not in ALLOWED_FOOTPRINTS:
                errors.append(f"{k}: Footprint '{p['footprint']}' nicht erlaubt")
            if not os.path.isfile(os.path.join(IMG_DIR, p["image"])):
                errors.append(f"{k}: Bild '{p['image']}' fehlt")
            for prm in p["params"]:
                if not prm.get("name") or prm.get("value") in (None, ""):
                    errors.append(f"{k}: unvollständiger Parameter {prm}")
            for f in ("name", "description", "category", "unit"):
                if not p[f]:
                    errors.append(f"{k}: Feld '{f}' leer")
            if not p["category"].startswith("Elektronik/"):
                errors.append(f"{k}: Kategorie außerhalb 'Elektronik/'")

    os.makedirs(DATA_DIR, exist_ok=True)
    total = 0
    for grp in sorted(GROUPS):
        parts = sorted(GROUPS[grp], key=lambda p: (p["category"], p["key"]))
        doc = {"group": GROUP_TITLES[grp], "parts": parts}
        with open(os.path.join(DATA_DIR, f"{grp}.json"), "w", encoding="utf-8", newline="\n") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write("\n")
        print(f"{grp + '.json':24s} {len(parts):5d}")
        total += len(parts)
    print(f"{'GESAMT':24s} {total:5d}")

    if errors:
        print(f"\nVALIDIERUNG FEHLGESCHLAGEN ({len(errors)} Fehler):", file=sys.stderr)
        for e in errors:
            print("  - " + e, file=sys.stderr)
        return 1
    print("Validierung OK: Keys eindeutig, Bilder vorhanden, Parameter vollständig, Footprints erlaubt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
