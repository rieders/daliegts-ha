"""Liest MySQL-/MariaDB-Dumps (phpMyAdmin, mysqldump) ohne MySQL-Server in eine SQLite-Datenbank ein."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

CREATE_RE = re.compile(r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?`(\w+)`\s*\((.*?)\n\)\s*[^;]*;", re.S | re.I)
INSERT_RE = re.compile(r"INSERT INTO\s+`(\w+)`\s*(\(([^)]*)\))?\s*VALUES\s*", re.I)
ESC = {"0": "\0", "b": "\b", "n": "\n", "r": "\r", "t": "\t", "Z": "\x1a", "\\": "\\", "'": "'", '"': '"'}


def _parse_values(s: str, pos: int) -> tuple[list[list], int]:
    """Parst "(1,'a',NULL),(2,'b',3.5);" ab pos. Gibt Zeilen und Position nach ';' zurück."""
    rows: list[list] = []
    n = len(s)
    while pos < n:
        while pos < n and s[pos] in " \t\r\n,":
            pos += 1
        if pos >= n or s[pos] == ";":
            return rows, pos + 1
        if s[pos] != "(":
            raise ValueError(f"Unerwartetes Zeichen {s[pos]!r} an Position {pos}")
        pos += 1
        row: list = []
        while True:
            while s[pos] in " \t\r\n":
                pos += 1
            c = s[pos]
            if c == "'":
                pos += 1
                buf = []
                while True:
                    ch = s[pos]
                    if ch == "\\":
                        nxt = s[pos + 1]
                        buf.append(ESC.get(nxt, nxt))
                        pos += 2
                    elif ch == "'":
                        if pos + 1 < n and s[pos + 1] == "'":
                            buf.append("'")
                            pos += 2
                        else:
                            pos += 1
                            break
                    else:
                        buf.append(ch)
                        pos += 1
                row.append("".join(buf))
            else:
                m = re.compile(r"[^,)]+").match(s, pos)
                tok = m.group(0).strip()
                pos = m.end()
                if tok.upper() == "NULL":
                    row.append(None)
                else:
                    try:
                        row.append(int(tok))
                    except ValueError:
                        try:
                            row.append(float(tok))
                        except ValueError:
                            row.append(tok)
            while s[pos] in " \t\r\n":
                pos += 1
            if s[pos] == ",":
                pos += 1
                continue
            if s[pos] == ")":
                pos += 1
                break
            raise ValueError(f"Syntaxfehler im Dump an Position {pos}")
        rows.append(row)
    return rows, pos


def load_dump(path: Path, target: sqlite3.Connection | None = None) -> sqlite3.Connection:
    """Lädt einen Dump in eine (In-Memory-)SQLite-DB. Spaltennamen werden aus CREATE TABLE übernommen."""
    raw = Path(path).read_bytes()
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        s = raw.decode("latin-1")
    con = target or sqlite3.connect(":memory:")
    columns: dict[str, list[str]] = {}
    for m in CREATE_RE.finditer(s):
        table, body = m.group(1), m.group(2)
        cols = re.findall(r"^\s*`(\w+)`", body, re.M)
        columns[table] = cols
        con.execute(f'DROP TABLE IF EXISTS "{table}"')
        con.execute(f'CREATE TABLE "{table}" ({", ".join(chr(34) + c + chr(34) for c in cols)})')
    pos = 0
    while True:
        m = INSERT_RE.search(s, pos)
        if not m:
            break
        table = m.group(1)
        cols = [c.strip().strip("`") for c in m.group(3).split(",")] if m.group(3) else columns.get(table, [])
        rows, pos = _parse_values(s, m.end())
        if table not in columns:
            con.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({", ".join(chr(34) + c + chr(34) for c in cols)})')
            columns[table] = cols
        ph = ", ".join("?" for _ in cols)
        con.executemany(f'INSERT INTO "{table}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) VALUES ({ph})', rows)
    con.commit()
    con.row_factory = sqlite3.Row
    return con


def tables(con: sqlite3.Connection) -> dict[str, int]:
    return {r[0]: con.execute(f'SELECT COUNT(*) FROM "{r[0]}"').fetchone()[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
