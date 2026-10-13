"""Lizenzschlüssel erstellen und prüfen (nur für den Verkäufer – braucht den privaten Signierschlüssel).

  python -m app.license_tool erstellen --schluessel ~/daliegts-lizenz-privat.key --name "Max Muster" \\
         --email max@example.org --id DL-0001 --jahre 1
  python -m app.license_tool pruefen "DLS1.…"

--jahre 0 = Updates ohne Ablauf. Der private Schlüssel gehört NICHT ins Repository und nicht auf fremde Rechner.
"""
from __future__ import annotations

import argparse
import base64
import sys
from datetime import date
from pathlib import Path

from .services import ed25519
from .services import license as lic


def _read_secret(path: str) -> bytes:
    raw = base64.b64decode(Path(path).expanduser().read_text().strip())
    if len(raw) != 32:
        raise SystemExit("Signierschlüssel hat nicht 32 Bytes – falsche Datei?")
    pub = base64.b64encode(ed25519.public_key(raw)).decode()
    if pub not in lic.PUBLIC_KEYS:
        raise SystemExit("Dieser Signierschlüssel passt nicht zu den öffentlichen Schlüsseln im Programm.")
    return raw


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.license_tool", description="DaLiegt’s-Lizenzen erstellen/prüfen")
    sub = ap.add_subparsers(dest="cmd", required=True)
    mk = sub.add_parser("erstellen")
    mk.add_argument("--schluessel", required=True, help="Datei mit dem privaten Signierschlüssel (base64)")
    mk.add_argument("--name", required=True)
    mk.add_argument("--email", default="")
    mk.add_argument("--id", default="", help="Lizenznummer, z. B. Bestellnummer")
    mk.add_argument("--jahre", type=int, default=1, help="Jahre mit Updates (0 = ohne Ablauf)")
    ck = sub.add_parser("pruefen")
    ck.add_argument("key")
    a = ap.parse_args(argv)
    if a.cmd == "erstellen":
        secret = _read_secret(a.schluessel)
        today = date.today()
        upd = None if a.jahre <= 0 else today.replace(year=today.year + a.jahre).isoformat()
        key = lic.make_key(secret, a.name, a.email, a.id, today.isoformat(), upd)
        print(key)
        return 0
    r = lic.parse(lic.normalize(a.key))
    if not r.ok:
        print("UNGÜLTIG:", r.error)
        return 1
    print(f"gültig: {r.name} <{r.email}> · Lizenz {r.lic_id} · ausgestellt {r.issued_de} · "
          f"Updates {'bis ' + r.updates_de if r.updates_until else 'ohne Ablauf'}" + (f" · {r.error}" if r.error else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
