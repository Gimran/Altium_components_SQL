"""SQLite specifics: connections, snapshots, WAL state and column metadata.

The rest of the app writes SQL with square-bracket quoting and `?` placeholders, which SQLite
accepts as well as Altium's ODBC path does.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

HIDDEN_TABLE_RE = re.compile(r"^(sqlite_|~TMP|__)", re.IGNORECASE)

# Errors raised for a rejected statement.
Error = sqlite3.Error

NUMERIC = {"INTEGER", "REAL", "NUMERIC"}


def q(name: str) -> str:
    """Double-quote an identifier for SQL built by hand."""
    return '"' + name.replace('"', '""') + '"'


def open_connection(path: Path) -> sqlite3.Connection:
    cn = sqlite3.connect(str(path))
    cn.execute("PRAGMA foreign_keys = ON")
    return cn


def scalar(cur):
    """First column of the first row."""
    row = cur.fetchone()
    return None if row is None else row[0]


def copy_database(src: Path, dst: Path) -> None:
    """A consistent snapshot through SQLite's backup API, even while someone writes."""
    source = sqlite3.connect(str(src))
    target = sqlite3.connect(str(dst))
    try:
        with target:
            source.backup(target)
    finally:
        target.close()
        source.close()


def locked_by_someone(db: Path) -> bool:
    """True if another process holds a lock that would stop a write from committing."""
    cn = sqlite3.connect(str(db), timeout=2, isolation_level=None)
    try:
        cn.execute("BEGIN EXCLUSIVE")
        cn.execute("ROLLBACK")
        return False
    except sqlite3.OperationalError:
        return True
    finally:
        cn.close()


def wal_status(path: Path) -> dict | None:
    """How much of a WAL database still lives only in the -wal file. None if not in WAL.

    Read from the -shm index without touching the database, so it never waits on a lock:
    mxFrame (offset 16) is the last committed frame, nBackfill (offset 96) the last one
    copied into the main file. Both are native-endian u32 — little-endian on Windows.
    """
    try:
        cn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
        try:
            mode = cn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            cn.close()
    except sqlite3.Error:
        return None
    if mode != "wal":
        return None
    wal, shm = Path(f"{path}-wal"), Path(f"{path}-shm")
    pending = 0
    try:
        head = shm.read_bytes()[:136]
        if len(head) >= 100 and head[12] == 1:
            max_frame = int.from_bytes(head[16:20], "little")
            backfilled = int.from_bytes(head[96:100], "little")
            pending = max(0, max_frame - backfilled)
    except OSError:
        pass                    # no -shm: nobody has the database open, nothing is pending
    return {"pending": pending, "wal_bytes": wal.stat().st_size if wal.exists() else 0}


def checkpoint(path: Path) -> dict:
    """Copy what readers allow from -wal into the main file; empty -wal if everything went.

    PASSIVE never waits: frames a reader still needs (Altium's open read) stay in -wal.
    """
    cn = sqlite3.connect(str(path), timeout=1, isolation_level=None)
    try:
        busy, frames, moved = cn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        if frames >= 0 and moved == frames:
            cn.execute("PRAGMA busy_timeout = 0")
            cn.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # shrink -wal; fine if it cannot
    finally:
        cn.close()
    return {"frames": max(frames, 0), "moved": max(moved, 0)}


def list_tables(cn) -> list[str]:
    names = [r[0] for r in cn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    return sorted(n for n in names if not HIDDEN_TABLE_RE.match(n))


# --------------------------------------------------------------------- column metadata

# The column name must not swallow quotes or newlines, or it runs past earlier columns.
GENERATED_RE = re.compile(
    r'"(?P<col>[^"\n]+)"\s+(?:\w+\s+)?GENERATED\s+ALWAYS\s+AS\s*\(', re.IGNORECASE)


def _generated_expressions(cn, table: str) -> dict[str, str]:
    """Pull `GENERATED ALWAYS AS (...)` bodies out of the stored CREATE TABLE statement."""
    row = cn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                     (table,)).fetchone()
    if not row or not row[0]:
        return {}
    ddl = row[0]
    out: dict[str, str] = {}
    for m in GENERATED_RE.finditer(ddl):
        depth, i = 1, m.end()
        while i < len(ddl) and depth:
            depth += {"(": 1, ")": -1}.get(ddl[i], 0)
            i += 1
        out[m.group("col")] = ddl[m.end():i - 1].strip()
    return out


def describe(cn, table: str) -> list[dict]:
    """Column metadata in the shape db.Column expects."""
    exprs = _generated_expressions(cn, table)
    cols = []
    for cid, name, decl, notnull, _default, pk, hidden in cn.execute(
            f"PRAGMA table_xinfo({q(table)})"):
        tn = (decl or "").upper()
        # An INTEGER PRIMARY KEY is a rowid alias — SQLite assigns it on insert.
        is_auto = bool(pk) and tn == "INTEGER"
        cols.append({
            "name": name,
            "type_name": tn or "TEXT",
            "size": None,            # SQLite does not enforce declared text lengths
            "nullable": not notnull,
            "ordinal": cid + 1,
            "is_autonumber": is_auto,
            "is_numeric": tn in NUMERIC and not is_auto,
            "expression": exprs.get(name, "") or ("вычисляемое поле" if hidden in (2, 3) else ""),
        })
    return cols
