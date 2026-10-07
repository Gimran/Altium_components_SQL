"""Keep GH_DB_SQLITE.DbLib in step with the database and with where this folder lives.

    python app/make_dblib.py

The file holds Altium's own settings — column layouts, the last open table and so on — so it
is only patched: the connection string (an absolute path to the database, see README),
[TableN] + field maps for database tables it does not list yet, and field maps for
Footprint Ref/Path 3-4 when those columns exist. Nothing else is touched, and an up-to-date
file is not rewritten. start.cmd runs this on every start.

Close the .DbLib in Altium first: if Altium saves it afterwards, it writes its own copy back.

The absolute path differs from machine to machine, so git keeps the file with a placeholder
instead (.gitattributes, filter "dblib"): `--clean` puts the placeholder in on commit,
`--smudge` puts this folder's path back on checkout. Every run registers that filter in the
clone's .git/config, which git does not carry over on clone.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from check_altium_connection import DOWNLOAD, connection_string, installed_drivers, pick_driver

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "GH_DB_SQLITE.DbLib"
SQLITE_FILE = ROOT / "GH_DB_LIB.sqlite"
GIT_PLACEHOLDER = SQLITE_FILE.name

# Whatever name sqliteodbc actually registered; falls back to the usual one so the file can
# still be generated before the driver is installed.
INSTALLED_DRIVER = pick_driver(installed_drivers())
ODBC_DRIVER = INSTALLED_DRIVER or "SQLite3 ODBC Driver"

SECTION_RE = re.compile(r"^\[(?P<name>[^\]]+)\]\s*$")
CS_DATABASE_RE = re.compile(r'^(ConnectionString=[^\r\n]*?\bDatabase=)[^;"\r\n]*', re.M)


def log(msg: str = "") -> None:
    sys.stdout.buffer.write((msg + "\n").encode("utf-8"))


def set_database(text: str, path: str) -> str:
    # A function, not a replacement string: Windows paths are full of backslashes.
    return CS_DATABASE_RE.sub(lambda m: m.group(1) + path, text)


def database_of(cs: str) -> str:
    m = re.search(r'\bDatabase=([^;"]*)', cs)
    return m.group(1) if m else ""


def git_filter(flag: str) -> int:
    """stdin → stdout for git: placeholder on --clean, this folder's database on --smudge."""
    text = sys.stdin.buffer.read().decode("utf-8", "surrogateescape")
    path = GIT_PLACEHOLDER if flag == "--clean" else str(SQLITE_FILE)
    sys.stdout.buffer.write(set_database(text, path).encode("utf-8", "surrogateescape"))
    return 0


def register_git_filter() -> None:
    git = shutil.which("git")
    if not git or not (ROOT / ".git").exists():
        return
    script = Path(__file__).resolve().relative_to(ROOT).as_posix()
    for kind in ("clean", "smudge"):
        key = f"filter.dblib.{kind}"
        want = f'"{Path(sys.executable).as_posix()}" {script} --{kind}'
        have = subprocess.run([git, "-C", str(ROOT), "config", "--get", key],
                              capture_output=True, text=True).stdout.strip()
        if have != want:
            subprocess.run([git, "-C", str(ROOT), "config", key, want], capture_output=True)
            log(f"git: настроен фильтр {key}")


def connection_keys() -> dict[str, str]:
    # Altium itself blanks the database type and path when "Use Connection String" is chosen.
    return {
        "ConnectionString": connection_string(ODBC_DRIVER, SQLITE_FILE),
        "LibraryDatabaseType": "",
        "LibraryDatabasePath": "",
        "DatabasePathRelative": "0",
    }


def sections(text: str) -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    name, lines = "", []
    for line in text.splitlines():
        m = SECTION_RE.match(line)
        if m:
            if name or lines:
                out.append((name, lines))
            name, lines = m.group("name"), [line]
        else:
            lines.append(line)
    out.append((name, lines))
    return out


def sqlite_columns(table: str) -> set[str]:
    import sqlite3
    cn = sqlite3.connect(f"file:{SQLITE_FILE}?mode=ro", uri=True)
    try:
        return {r[1] for r in cn.execute(f'PRAGMA table_info("{table}")')}
    finally:
        cn.close()


def missing_field_maps(lines: list[str]) -> tuple[list[str], list[str]]:
    """FieldMap sections for footprint columns the database has but the .DbLib does not map.

    Each is cloned from the table's "Footprint Ref 2"/"Footprint Path 2" entry, so it carries
    whatever flags Altium put there.
    """
    options = [l for l in lines if l.startswith("Options=FieldName=")]
    mapped = {l.split("|", 1)[0].split("=", 2)[2] for l in options}
    tables = [l.split("=", 1)[1] for l in lines if l.startswith("TableName=")]
    next_no = 1 + max((int(m.group(1)) for l in lines
                       if (m := re.match(r"^\[FieldMap(\d+)\]", l))), default=0)
    added, notes = [], []
    for table in tables:
        have = sqlite_columns(table)
        for n in (3, 4):
            for kind in ("Footprint Ref", "Footprint Path"):
                col, model = f"{kind} {n}", f"{kind} 2"
                if col not in have or f"{table}.{col}" in mapped:
                    continue
                template = next((l for l in options if l.startswith(f"Options=FieldName={table}.{model}|")), None)
                if template is None:
                    continue
                added += [f"[FieldMap{next_no}]", template.replace(model, col)]
                notes.append(f"{table}: добавлен FieldMap для {col}")
                next_no += 1
    return added, notes


def sqlite_tables() -> list[str]:
    import sqlite3
    cn = sqlite3.connect(f"file:{SQLITE_FILE}?mode=ro", uri=True)
    try:
        return [r[0] for r in cn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND name NOT LIKE '~%' AND name NOT LIKE '\\_\\_%' ESCAPE '\\' ORDER BY name")]
    finally:
        cn.close()


def missing_tables(lines: list[str]) -> tuple[list[str], list[str], list[str]]:
    """[TableN] sections and FieldMaps for database tables the .DbLib does not list yet.

    The field maps are copied from the first listed table, for the columns the new table has.
    Returns (table sections, field map sections, notes).
    """
    listed = [l.split("=", 1)[1] for l in lines if l.startswith("TableName=")]
    if not listed:
        return [], [], []
    model = listed[0]
    options = [l for l in lines if l.startswith(f"Options=FieldName={model}.")]
    next_table = 1 + len(listed)
    next_map = 1 + max((int(m.group(1)) for l in lines
                        if (m := re.match(r"^\[FieldMap(\d+)\]", l))), default=0)
    tables, maps, notes = [], [], []
    for table in sqlite_tables():
        if table in listed:
            continue
        tables += [f"[Table{next_table}]", "SchemaName=", f"TableName={table}", "Enabled=True",
                   "UserWhere=0", "UserWhereText="]
        next_table += 1
        have = sqlite_columns(table)
        for opt in options:
            column = opt.split("|", 1)[0].split(".", 1)[1]
            if column not in have:
                continue
            maps += [f"[FieldMap{next_map}]",
                     opt.replace(f"FieldName={model}.", f"FieldName={table}.")
                        .replace(f"TableNameOnly={model}|", f"TableNameOnly={table}|")]
            next_map += 1
        notes.append(f"добавлена таблица {table} (сопоставление полей — как у {model})")
    return tables, maps, notes


def patch(text: str, drop_tmp_tables: bool) -> tuple[str, list[str]]:
    keys = connection_keys()
    changes: list[str] = []
    out: list[str] = []
    table_index = 0
    for name, lines in sections(text):
        table = next((l.split("=", 1)[1] for l in lines if l.startswith("TableName=")), "")
        if name.startswith("Table"):
            if drop_tmp_tables and table.startswith("~TMP"):
                changes.append(f"убрана несуществующая таблица {table}")
                continue
            table_index += 1
            lines = [f"[Table{table_index}]" if SECTION_RE.match(l) else l for l in lines]
        for line in lines:
            key, sep, value = line.partition("=")
            if sep and key in keys and value != keys[key]:
                if key == "ConnectionString" and database_of(value) != database_of(keys[key]):
                    changes.append(f"путь к базе: {database_of(value) or '(пусто)'}"
                                   f" → {database_of(keys[key])}")
                else:
                    changes.append(f"{key}: {value or '(пусто)'} → {keys[key] or '(пусто)'}")
                line = f"{key}={keys[key]}"
            out.append(line)
    added, notes = missing_field_maps(out)
    out = "\n".join(out).rstrip().splitlines() + added
    tables, maps, table_notes = missing_tables(out)
    if tables:
        # New [TableN] go right after the last existing one, field maps at the end.
        last = max(i for i, l in enumerate(out) if l.startswith("TableName="))
        end = next((i for i in range(last + 1, len(out)) if SECTION_RE.match(out[i])), len(out))
        out = out[:end] + tables + out[end:] + maps
    return "\n".join(out).rstrip() + "\n", changes + notes + table_notes


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in ("--clean", "--smudge"):
        return git_filter(sys.argv[1])
    for path in (SQLITE_FILE, TARGET):
        if not path.is_file():
            log(f"не найден {path}")
            return 1
    register_git_filter()
    if INSTALLED_DRIVER is None:
        log("ВНИМАНИЕ: 64-битный ODBC-драйвер SQLite не установлен — Altium не подключится")
        log(f"  к базе. Поставьте sqliteodbc_w64.exe: {DOWNLOAD}")
    before = TARGET.read_text(encoding="utf-8-sig", errors="replace")
    text, changes = patch(before, drop_tmp_tables=True)
    if text == before.rstrip() + "\n" or not changes:
        log(f"{TARGET.name}: уже в актуальном состоянии")
        return 0
    TARGET.write_text(text, encoding="utf-8")
    log(f"обновлён на месте: {TARGET}")

    for change in changes:
        log(f"  {change}")
    log(f"драйвер: {ODBC_DRIVER}")
    log(f"секций FieldMap: {sum(1 for l in text.splitlines() if l.startswith('[FieldMap'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
