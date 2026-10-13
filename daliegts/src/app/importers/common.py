"""Gemeinsame Helfer für Importer: Bericht, externe IDs (Idempotenz), HTML-Bereinigung."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ExternalId


@dataclass
class Report:
    title: str
    dry_run: bool = False
    counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)

    def inc(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def as_text(self) -> str:
        lines = [f"{self.title}{' – PROBELAUF (nichts gespeichert)' if self.dry_run else ''}", ""]
        lines += [f"  {k}: {v}" for k, v in self.counts.items()]
        if self.notes:
            lines += ["", "Hinweise:"] + [f"  • {n}" for n in self.notes]
        if self.duplicates:
            lines += ["", f"Mögliche Dubletten ({len(self.duplicates)}):"] + [f"  • {d}" for d in self.duplicates[:100]]
        if self.warnings:
            lines += ["", f"Warnungen ({len(self.warnings)}):"] + [f"  • {w}" for w in self.warnings[:200]]
        return "\n".join(lines)


def ext_get(db: Session, source: str, ext_type: str, ext_id) -> int | None:
    e = db.scalar(select(ExternalId).where(ExternalId.source == source, ExternalId.ext_type == ext_type, ExternalId.ext_id == str(ext_id)))
    return e.entity_id if e else None


def ext_set(db: Session, source: str, ext_type: str, ext_id, entity: str, entity_id: int) -> None:
    e = db.scalar(select(ExternalId).where(ExternalId.source == source, ExternalId.ext_type == ext_type, ExternalId.ext_id == str(ext_id)))
    if e is None:
        db.add(ExternalId(source=source, ext_type=ext_type, ext_id=str(ext_id), entity=entity, entity_id=entity_id))
    else:
        e.entity, e.entity_id = entity, entity_id


def clean_html(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


FOOTPRINT_IMAGE_HINTS = {
    "0402": "smd-chip-generic.svg", "0603": "smd-chip-resistor.svg", "0805": "smd-chip-resistor.svg", "1206": "smd-chip-resistor.svg",
    "sot-23": "sot-23.svg", "sot23": "sot-23.svg", "sot-223": "sot-223.svg", "sot223": "sot-223.svg", "sot-89": "sot-89.svg",
    "sma": "sma.svg", "do-214": "sma.svg", "smb": "sma.svg", "minimelf": "minimelf.svg", "sod-80": "minimelf.svg", "sod-123": "sod-123.svg",
    "so-8": "so-8.svg", "so8": "so-8.svg", "soic-8": "so-8.svg", "so-14": "so-14.svg", "so-16": "so-16.svg", "sop": "so-8.svg",
    "to-92": "to-92.svg", "to92": "to-92.svg", "to-220": "to-220.svg", "to220": "to-220.svg", "to-126": "to-126.svg", "to-3": "to-3.svg",
    "to-247": "to-247.svg", "do-35": "do-35.svg", "do35": "do-35.svg", "do-41": "do-41.svg", "do41": "do-41.svg", "do-201": "do-201.svg",
    "dip-8": "dip-8.svg", "dip8": "dip-8.svg", "dip-14": "dip-14.svg", "dip-16": "dip-16.svg", "dip-18": "dip-18.svg", "dip-20": "dip-20.svg",
    "dip-28": "dip-28.svg", "dip28": "dip-28.svg", "pdip-28": "dip-28.svg", "dip-40": "dip-40.svg", "hc49": "crystal-hc49.svg",
    "elko": "elko.svg", "radial": "elko.svg", "led 3": "led-3mm-rot.svg", "led3": "led-3mm-rot.svg", "3mm": "led-3mm-rot.svg",
    "led 5": "led-5mm-rot.svg", "5mm": "led-5mm-rot.svg", "axial": "resistor-tht.svg", "bedrahtet": "resistor-tht.svg",
    "so08": "so-8.svg", "so14": "so-14.svg", "so18": "so-16.svg", "dip40": "dip-40.svg", "dip14": "dip-14.svg",
    "dip08": "dip-8.svg", "sot-23-5": "sot-23.svg", "do214": "sma.svg", "micro-sma": "sma.svg", "kerko": "smd-chip-capacitor.svg",
    "tantal": "tantal-smd.svg", "led-rot_3mm": "led-3mm-rot.svg", "led-gruen_3mm": "led-3mm-gruen.svg", "led-gelb_3mm": "led-3mm-gelb.svg",
    "led-rot_5mm": "led-5mm-rot.svg", "led-gruen_5mm": "led-5mm-gruen.svg", "led-gelb_5mm": "led-5mm-gelb.svg", "led-blau_0603": "led-smd.svg",
    "led-rot_0603": "led-smd.svg", "led grün 603": "led-smd.svg", "quarz": "crystal-hc49.svg", "poti": "potentiometer.svg",
    "widerstand-kohle": "resistor-tht.svg", "kohle": "resistor-tht.svg", "folienkondensator": "film-cap.svg", "mks": "film-cap.svg",
    "scheiben": "mlcc-tht.svg", "schiebeschalter": "slide-switch.svg", "wippschalter": "rocker-switch.svg", "taster": "button-6x6.svg",
    "gleichrichter_mbs": "bridge-smd.svg", "gleichrichter": "bridge-rectifier.svg", "ar-klemm": "screw-terminal.svg", "rj11": "rj45.svg",
    "jumper": "pin-header.svg", "sockel_dip": "ic-socket.svg", "sockel": "ic-socket.svg", "drossel": "inductor-smd.svg", "sdr1105": "inductor-smd.svg",
    "mimelf": "minimelf.svg", "sm8": "so-8.svg", "pqfp": "tqfp.svg",
    "stiftleiste": "pin-header.svg", "buchsenleiste": "female-header.svg", "tqfp": "tqfp.svg", "qfn": "qfn.svg", "tssop": "tssop.svg",
}


def guess_footprint_image(name: str) -> str | None:
    n = (name or "").lower().replace(" ", "")
    for key, img in sorted(FOOTPRINT_IMAGE_HINTS.items(), key=lambda kv: -len(kv[0])):
        if key.replace(" ", "") in n:
            return f"catalog:{img}"
    return None
