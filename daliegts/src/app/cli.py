"""Kommandozeile: python -m app.cli <befehl>

Befehle:
  migrate            Sichere Migration (Backup → Probelauf auf Kopie → Migration → Prüfung)
  check              Schema-Status anzeigen (Exit 0 = ok)
  backup             SQLite-Backup (+ --export für offenes Exportformat, --media für Medien)
  export [DATEI]     Export im Format "Lagerwerk-Export v1"
  restore DATEI      Export in LEERE Datenbank importieren
  create-admin       Admin-Konto anlegen
  import-partdb DUMP [--neuer DUMP] [--media ARCHIV] [--probelauf]
  import-inventree [--sqlite DB] [--tracking CSV] [--csv-dir ORDNER] [--media ARCHIV] [--probelauf]
  reindex            Suchindex neu aufbauen
  serve              Server starten (Uvicorn)
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from .config import settings


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lagerwerk", description="DaLiegt’s (lagerwerk) – Verwaltung")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sub.add_parser("check")
    b = sub.add_parser("backup")
    b.add_argument("--export", action="store_true")
    b.add_argument("--media", action="store_true")
    b.add_argument("--label", default="")
    e = sub.add_parser("export")
    e.add_argument("file", nargs="?")
    r = sub.add_parser("restore")
    r.add_argument("file")
    a = sub.add_parser("create-admin")
    a.add_argument("--username", default="admin")
    a.add_argument("--password")
    a.add_argument("--name", default="")
    ip = sub.add_parser("import-partdb")
    ip.add_argument("dump")
    ip.add_argument("--neuer")
    ip.add_argument("--media")
    ip.add_argument("--probelauf", action="store_true")
    ii = sub.add_parser("import-inventree")
    ii.add_argument("--sqlite")
    ii.add_argument("--tracking")
    ii.add_argument("--csv-dir")
    ii.add_argument("--media")
    ii.add_argument("--probelauf", action="store_true")
    sub.add_parser("reindex")
    pt = sub.add_parser("provider-test", help="Info-Provider testen, z. B. provider-test reichelt NE555")
    pt.add_argument("provider")
    pt.add_argument("query")
    pt.add_argument("--sku", action="store_true", help="als Bestellnummer abrufen statt suchen")
    sub.add_parser("provider-nightly", help="Nachtlauf der Info-Provider sofort ausführen")
    s = sub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    args = p.parse_args(argv)

    from .services import schema

    if args.cmd in ("provider-test", "provider-nightly"):
        import asyncio
        import json as _json

        from . import db as dbmod
        from .services import infosync
        from .services.providers import load_config, registry

        if args.cmd == "provider-nightly":
            print(_json.dumps(asyncio.run(infosync.nightly()), ensure_ascii=False, indent=1))
            return 0
        cls = registry().get(args.provider)
        if cls is None:
            print("Unbekannter Provider. Möglich: " + ", ".join(registry()), file=sys.stderr)
            return 2

        async def _run():
            with dbmod.session_scope() as db:
                conf, _ = load_config(db)
            async with infosync.new_client() as client:
                prov = cls(conf.get(args.provider, {}), client)
                if not prov.configured():
                    return "Zugangsdaten fehlen (Verwaltung › Info-Provider)."
                if args.sku:
                    h = await prov.get(args.query)
                    return [h.to_dict()] if h else []
                return [h.to_dict() for h in await prov.search(args.query, 5)]

        try:
            print(_json.dumps(asyncio.run(_run()), ensure_ascii=False, indent=1))
        except Exception as exc:  # noqa: BLE001 – Diagnosewerkzeug: Fehler vollständig zeigen
            import traceback

            traceback.print_exc()
            print(f"FEHLER: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.cmd == "check":
        status, cur, head = schema.check_schema(settings.db_path)
        print(f"Status: {status}  aktuell: {cur}  Programm: {head}  DB: {settings.db_path}")
        return 0 if status == "ok" else 1

    if args.cmd == "migrate":
        from .services.backup import backup_database

        def _bk():
            return backup_database(label=f"vor-migration-{schema.current_revision(settings.db_path)}")

        try:
            rep = schema.safe_migrate(settings.db_path, _bk if settings.db_path.exists() else None)
        except schema.SchemaError as exc:
            print(f"FEHLER: {exc}", file=sys.stderr)
            return 2
        print(rep)
        _seed()
        return 0

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("app.main:app", host=args.host, port=args.port, proxy_headers=settings.proxy_headers,
                    forwarded_allow_ips="*" if settings.proxy_headers else None, workers=1)
        return 0

    status, cur, head = schema.check_schema(settings.db_path)
    if status == "empty":
        schema.upgrade(settings.db_path)
    elif status != "ok":
        print(f"Schema nicht aktuell ({status}). Zuerst `python -m app.cli migrate` ausführen.", file=sys.stderr)
        return 2
    _seed()
    from .db import session_scope

    with session_scope() as db:
        if args.cmd == "backup":
            from .services.backup import backup_database, backup_media, export_v1, rotate

            print(backup_database(label=args.label))
            if args.export:
                print(export_v1(db, include_media=False))
            if args.media:
                print(backup_media())
            rotate()
        elif args.cmd == "export":
            from .services.backup import export_v1

            print(export_v1(db, Path(args.file) if args.file else None))
        elif args.cmd == "restore":
            from .services.backup import import_v1

            print(import_v1(db, Path(args.file)))
        elif args.cmd == "create-admin":
            from sqlalchemy import select

            from .models import Group, User
            from .security import hash_password

            pw = args.password or getpass.getpass("Passwort: ")
            g = db.scalar(select(Group).where(Group.name == "Admin"))
            u = db.scalar(select(User).where(User.username == args.username))
            if u is None:
                u = User(username=args.username, display_name=args.name, group_id=g.id)
                db.add(u)
            u.password_hash = hash_password(pw)
            print(f"Admin „{args.username}“ gespeichert.")
        elif args.cmd == "import-partdb":
            from .importers.partdb import import_partdb

            rep = import_partdb(db, Path(args.dump), Path(args.neuer) if args.neuer else None, Path(args.media) if args.media else None,
                                dry_run=args.probelauf)
            print(rep.as_text())
        elif args.cmd == "import-inventree":
            from .importers.inventree import import_inventree

            rep = import_inventree(db, Path(args.sqlite) if args.sqlite else None, Path(args.tracking) if args.tracking else None,
                                   Path(args.csv_dir) if args.csv_dir else None, Path(args.media) if args.media else None, dry_run=args.probelauf)
            print(rep.as_text())
        elif args.cmd == "reindex":
            from .services.search import rebuild_index

            print(f"{rebuild_index(db)} Artikel indiziert.")
    return 0


def _seed() -> None:
    from .db import session_scope
    from .services.seed import seed

    with session_scope() as db:
        seed(db)


if __name__ == "__main__":
    sys.exit(main())
