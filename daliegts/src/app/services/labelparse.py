"""Etiketten auf Lieferantentüten auslesen (v0.4).

Unterstützt
- ECIA/ANSI MH10.8.2 („[)>␞06␝…“): DigiKey, Mouser, Farnell u. a. als DataMatrix bzw. QR
- LCSC-QR („{pbn:…,pc:C12345,pm:NE555DR,qty:50,…}“)
- TME-QR („QTY:10 PN:… MFR:… MPN:… …“)
Ergebnis ist ein neutrales Dict; was fehlt, bleibt leer.
"""
from __future__ import annotations

import re

GS, RS, EOT = "\x1d", "\x1e", "\x04"

# Datenkennungen (Data Identifier) nach ANSI MH10.8.2, soweit für Bauteile nützlich
ECIA_FIELDS = {
    "1P": "mpn", "30P": "sku", "P": "sku", "Q": "qty", "1V": "manufacturer", "1T": "lot", "9D": "date_code", "10D": "date_code",
    "4L": "origin", "K": "order", "1K": "order", "14K": "order_line", "11K": "invoice", "11Z": "carrier", "12Z": "part_id",
}


def _empty(raw: str, source: str) -> dict:
    return {"source": source, "supplier": "", "sku": "", "mpn": "", "manufacturer": "", "qty": None, "lot": "", "date_code": "",
            "origin": "", "order": "", "raw": raw}


def parse_ecia(raw: str) -> dict | None:
    s = raw.replace("{GS}", GS).replace("{RS}", RS).replace("␝", GS).replace("␞", RS)
    if not s.startswith("[)>"):
        return None
    body = s[3:].lstrip(RS)
    if body.startswith("06"):
        body = body[2:]
    parts = [p for p in re.split(f"[{GS}{RS}{EOT}]", body) if p]
    if len(parts) < 2:
        return None
    out = _empty(raw, "ecia")
    for p in parts:
        # längste passende Kennung zuerst (z. B. 30P vor P, 1P vor P)
        for di in sorted(ECIA_FIELDS, key=len, reverse=True):
            if p.startswith(di) and (len(di) > 1 or not p[:2].isdigit()):
                key, val = ECIA_FIELDS[di], p[len(di):].strip()
                if key == "qty":
                    out["qty"] = int(val) if val.isdigit() else out["qty"]
                elif val and not out.get(key):
                    out[key] = val
                break
    sku = out["sku"]
    if sku.endswith("-ND") or out.get("part_id"):
        out["supplier"] = "DigiKey"
    elif re.match(r"^\d{2,3}-", sku):
        out["supplier"] = "Mouser"
    return out if (out["mpn"] or out["sku"]) else None


def parse_lcsc(raw: str) -> dict | None:
    s = raw.strip()
    if not (s.startswith("{") and "pc:" in s and "pm:" in s):
        return None
    fields = dict(m.groups() for m in re.finditer(r"(\w+):([^,}]*)", s))
    out = _empty(raw, "lcsc")
    out.update(supplier="LCSC", sku=fields.get("pc", "").strip(), mpn=fields.get("pm", "").strip(), order=fields.get("on", "").strip())
    q = fields.get("qty", "").strip()
    out["qty"] = int(q) if q.isdigit() else None
    return out if out["sku"] or out["mpn"] else None


def parse_tme(raw: str) -> dict | None:
    s = raw.strip()
    if not re.search(r"\bQTY:\s*\d", s) or not re.search(r"\b(PN|MPN):", s):
        return None
    keys = r"(?:QTY|PN|MPN|MFR|PO|CPO|RoHS)"
    fields = {k: v.strip() for k, v in re.findall(rf"\b({keys[3:-1]}):\s*(.*?)(?=\s+{keys}:|\s+https?://|$)", s)}
    out = _empty(raw, "tme")
    out.update(supplier="TME", sku=fields.get("PN", ""), mpn=fields.get("MPN", ""), manufacturer=fields.get("MFR", ""),
               order=fields.get("PO", ""))
    out["qty"] = int(fields["QTY"]) if fields.get("QTY", "").isdigit() else None
    return out


def parse_label(raw: str | None) -> dict | None:
    """Erkennt Lieferanten-Etiketten; None wenn es keins ist."""
    if not raw or len(raw) < 6:
        return None
    for fn in (parse_ecia, parse_lcsc, parse_tme):
        try:
            r = fn(raw)
        except (ValueError, TypeError):
            r = None
        if r:
            return r
    return None


def looks_like_label(raw: str | None) -> bool:
    return parse_label(raw) is not None
