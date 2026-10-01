"""Data layer for the component database UI (SQLite).

SQLite specifics live in backends.py; the SQL here uses square-bracket quoting and `?`
placeholders.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import backends

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"

# Columns with at most this many distinct values get a pick list in the form.
DROPDOWN_MAX_DISTINCT = 60
# Keep at most this many backup copies of the database file.
BACKUP_KEEP = 10
VALUES_TTL = 300.0

_lock = threading.Lock()
_schema_cache: dict[str, "Table"] = {}
_values_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}


class DbError(RuntimeError):
    pass


def load_config() -> dict:
    cfg = {
        "db_path": "",
        "library_path": "",      # where .PcbLib/.SchLib live; defaults to the database's folder
        "host": "127.0.0.1",
        "port": 8777,
        "backup_on_write": True,
        "open_browser": True,
    }
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    for key, env_name in (("db_path", "COMPONENT_DB_PATH"),
                          ("library_path", "COMPONENT_DB_LIBRARIES")):
        env = os.environ.get(env_name)
        if env:
            cfg[key] = env
    return cfg


CONFIG = load_config()


def db_path() -> Path:
    p = Path(CONFIG["db_path"]).expanduser()
    if not p.is_absolute():
        p = (APP_DIR.parent / p).resolve()
    return p


def library_root() -> Path:
    """Folder searched for .PcbLib/.SchLib — the database's own folder unless configured."""
    configured = (CONFIG.get("library_path") or "").strip()
    if not configured:
        return db_path().parent
    p = Path(configured).expanduser()
    return p if p.is_absolute() else (APP_DIR.parent / p).resolve()


@contextmanager
def connect():
    """Short-lived connection to the database at db_path()."""
    p = db_path()
    if not p.exists():
        raise DbError(f"Файл базы не найден: {p}")
    cn = backends.open_connection(p)
    try:
        yield cn
    finally:
        cn.close()


# --------------------------------------------------------------------------- schema

GROUPS = (
    ("altium", (
        "Library Ref", "Footprint Ref", "Library Path", "Footprint Path",
        "Footprint Ref 2", "Footprint Path 2", "Footprint Ref 3",
        "Footprint Path 3", "Zone", "PE3",
    )),
    ("supply", (
        "Supplier 1", "Supplier Part Number 1",
        "Supplier 2", "Supplier Part Number 2",
        "Unit Price (USD)", "Price", "DatasheetURL", "ImageURL", "Image",
        "ComponentLink1URL", "ComponentLink1Description",
        "Category", "CategorySub",
    )),
)
# PartNumber + Manufacturer identify a part, so they lead the form side by side.
MAIN_FIELDS = ("PartNumber", "Manufacturer", "Description", "Value")
GROUP_TITLES = {
    "main": "Основное",
    "params": "Параметры",
    "altium": "Altium",
    "supply": "Поставщик и документация",
}
TEXT_TYPES = ("VARCHAR", "CHAR", "WVARCHAR", "LONGCHAR", "TEXT")


@dataclass
class Column:
    name: str
    type_name: str
    size: int | None
    nullable: bool
    ordinal: int
    is_autonumber: bool = False
    is_numeric: bool = False
    expression: str = ""      # calculated / generated column — cannot be written to

    @property
    def read_only(self) -> bool:
        return bool(self.expression) or self.is_autonumber

    @property
    def maxlength(self) -> int | None:
        # SQLite does not enforce declared text lengths, so size comes back as None there.
        return self.size if self.type_name in ("VARCHAR", "CHAR", "WVARCHAR") else None

    @property
    def is_long_text(self) -> bool:
        return self.type_name == "LONGCHAR"

    @property
    def is_url(self) -> bool:
        n = self.name.lower()
        return "url" in n or n == "image" or "datasheet" in n


@dataclass
class Table:
    name: str
    columns: list[Column]
    id_col: str
    id_is_autonumber: bool
    label_col: str = "PartNumber"
    loaded_at: float = field(default_factory=time.time)

    def col(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)

    @property
    def editable(self) -> list[Column]:
        return [c for c in self.columns
                if not c.read_only and c.name != self.id_col]

    @property
    def calculated(self) -> list[Column]:
        return [c for c in self.columns if c.expression]

    @property
    def form_columns(self) -> list[Column]:
        """Everything the form shows — calculated fields included, but rendered read-only."""
        return [c for c in self.columns if not c.is_autonumber and c.name != self.id_col]

    def grouped(self) -> list[tuple[str, str, list[Column]]]:
        assigned = {n.lower(): key for key, names in GROUPS for n in names}
        buckets: dict[str, list[Column]] = {"main": [], "params": [], "altium": [], "supply": []}
        for c in self.form_columns:
            if c.name in MAIN_FIELDS:
                buckets["main"].append(c)
            else:
                buckets[assigned.get(c.name.lower(), "params")].append(c)
        buckets["main"].sort(key=lambda c: MAIN_FIELDS.index(c.name))
        return [(k, GROUP_TITLES[k], buckets[k])
                for k in ("main", "params", "altium", "supply") if buckets[k]]


def list_tables() -> list[str]:
    with connect() as cn:
        return backends.list_tables(cn)


def get_table(name: str, refresh: bool = False) -> Table:
    with _lock:
        cached = _schema_cache.get(name)
    if cached and not refresh:
        return cached
    if name not in list_tables():
        raise DbError(f"Таблица {name!r} не найдена")
    with connect() as cn:
        cols = [Column(**info) for info in backends.describe(cn, name)]
    cols.sort(key=lambda c: c.ordinal)
    auto = next((c for c in cols if c.is_autonumber), None)
    if auto:
        id_col, id_auto = auto.name, True
    elif any(c.name.upper() == "ID" for c in cols):
        id_col = next(c.name for c in cols if c.name.upper() == "ID")
        id_auto = False
    else:
        id_col, id_auto = cols[0].name, False
    label = "PartNumber" if any(c.name == "PartNumber" for c in cols) else cols[0].name
    t = Table(name=name, columns=cols, id_col=id_col,
              id_is_autonumber=id_auto, label_col=label)
    with _lock:
        _schema_cache[name] = t
    return t


def refresh_schema() -> None:
    with _lock:
        _schema_cache.clear()
        _values_cache.clear()


# --------------------------------------------------------------------------- reads

def distinct_values(table: str, column: str, limit: int = DROPDOWN_MAX_DISTINCT) -> list[str]:
    key = (table, column)
    now = time.time()
    with _lock:
        hit = _values_cache.get(key)
    if hit and now - hit[0] < VALUES_TTL:
        return hit[1]
    sql = (f"SELECT DISTINCT [{column}] FROM [{table}] "
           f"WHERE [{column}] IS NOT NULL AND [{column}] <> '' "
           f"ORDER BY [{column}] LIMIT {limit + 1}")
    with connect() as cn:
        rows = [r[0] for r in cn.cursor().execute(sql)]
    out: list[str] = [] if len(rows) > limit else [str(v) for v in rows]
    with _lock:
        _values_cache[key] = (now, out)
    return out


def picklists(table: str) -> dict[str, list[str]]:
    t = get_table(table)
    out = {}
    for c in t.editable:
        if c.type_name not in TEXT_TYPES or c.name in ("PartNumber",) or "Part Number" in c.name:
            continue
        vals = distinct_values(table, c.name)
        if vals:
            out[c.name] = vals
    return out


def invalidate_values(table: str) -> None:
    with _lock:
        for k in [k for k in _values_cache if k[0] == table]:
            _values_cache.pop(k, None)


def grid_columns(t: Table) -> list[str]:
    preferred = ["PartNumber", "Manufacturer", "Value", "Description", "Package",
                 "Footprint Ref", "Tolerance", "Voltage", "Power (Watts)", "Library Ref"]
    names = [c.name for c in t.columns]
    picked = [p for p in preferred if p in names]
    for n in names:
        if len(picked) >= 7:
            break
        if n not in picked and n != t.id_col:
            picked.append(n)
    return picked[:7]


EMPTY_FILTER = "__empty__"     # "value is blank" choice in a column filter
MANUAL_ENTRY = "__manual__"    # "type it myself" choice in a footprint dropdown


def filterable_columns(t: Table) -> list[Column]:
    """Text columns worth a filter dropdown: between 2 and DROPDOWN_MAX_DISTINCT values."""
    out = []
    for c in t.columns:
        if c.name == t.id_col or c.expression or c.type_name not in TEXT_TYPES:
            continue
        if c.is_url or c.name in ("PartNumber", "Description", "PE3", "Zone"):
            continue
        if len(distinct_values(t.name, c.name)) >= 2:
            out.append(c)
    return out


def _build_where(t: Table, query: str, filters: dict[str, str]) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    if query.strip():
        searchable = [c.name for c in t.columns if c.type_name in TEXT_TYPES]
        for term in query.split():
            clauses.append("(" + " OR ".join(f"[{c}] LIKE ?" for c in searchable) + ")")
            params.extend([f"%{term}%"] * len(searchable))
    for col, val in filters.items():
        c = t.col(col)
        if c is None or val == "":
            continue
        if val == EMPTY_FILTER:
            clauses.append(f"([{col}] IS NULL OR [{col}] = '')")
        else:
            clauses.append(f"[{col}] = ?")
            params.append(val)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def filter_options(table: str, query: str = "",
                   filters: dict[str, str] | None = None) -> dict[str, list[str]]:
    """Values still available in each filter column, given the other active filters.

    Narrowing one column narrows the rest, so the bar never offers a choice that would
    return nothing.
    """
    t = get_table(table)
    filters = {k: v for k, v in (filters or {}).items() if v}
    cols = filterable_columns(t)
    if not query.strip() and not filters:          # nothing to narrow — use the cached lists
        return {c.name: distinct_values(table, c.name) for c in cols}

    # One pass over the candidate columns, filtered in Python, instead of a filtered
    # DISTINCT per column.
    names = [c.name for c in cols]
    names += [k for k in filters if k not in names and t.col(k) is not None]
    where, params = _build_where(t, query, {})      # search only; filters applied below
    sql = "SELECT " + ", ".join(f"[{n}]" for n in names) + f" FROM [{table}]{where}"
    with connect() as cn:
        rows = cn.cursor().execute(sql, params).fetchall()

    index = {n: i for i, n in enumerate(names)}

    def matches(row, col: str, wanted: str) -> bool:
        cell = row[index[col]]
        cell = "" if cell is None else str(cell)
        return cell == "" if wanted == EMPTY_FILTER else cell == wanted

    out: dict[str, list[str]] = {}
    for c in cols:
        others = [(k, v) for k, v in filters.items() if k != c.name]
        i = index[c.name]
        values = {("" if r[i] is None else str(r[i]))
                  for r in rows if all(matches(r, k, v) for k, v in others)}
        out[c.name] = sorted(values, key=str.casefold)
    return out


def search_rows(table: str, query: str = "", limit: int = 100, offset: int = 0,
                sort: str = "", desc: bool = False,
                filters: dict[str, str] | None = None) -> tuple[list[dict], int]:
    t = get_table(table)
    grid = grid_columns(t)
    select = ", ".join(f"[{c}]" for c in [t.id_col] + grid)
    where, params = _build_where(t, query, filters or {})
    sort_col = sort if sort in [c.name for c in t.columns] else t.id_col
    order = f" ORDER BY [{sort_col}]" + (" DESC" if desc else "")
    with connect() as cn:
        cur = cn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM [{table}]{where}", params)
        total = backends.scalar(cur)
        cur.execute(f"SELECT {select} FROM [{table}]{where}{order}", params)
        names = [d[0] for d in cur.description]
        rows = cur.fetchall()[offset:offset + limit]
    return [dict(zip(names, r)) for r in rows], total


def get_row(table: str, row_id) -> dict | None:
    t = get_table(table)
    with connect() as cn:
        cur = cn.cursor()
        cur.execute(f"SELECT * FROM [{table}] WHERE [{t.id_col}] = ?", (_cast_id(t, row_id),))
        row = cur.fetchone()
        if row is None:
            return None
        names = [d[0] for d in cur.description]
    return dict(zip(names, row))


def defaults_for(table: str) -> dict:
    """Prefill columns that hold exactly one value across the whole table."""
    t = get_table(table)
    out: dict[str, str] = {}
    for c in t.editable:
        # Manufacturer stays: a single-maker table (all Yageo) should still prefill it.
        if c.is_numeric or c.name in ("PartNumber", "Description", "Value") or "Part Number" in c.name:
            continue
        vals = distinct_values(table, c.name, limit=1)
        if len(vals) == 1:
            out[c.name] = vals[0]
    return out


def part_number_exists(table: str, part_number: str, exclude_id=None) -> bool:
    t = get_table(table)
    if not t.col("PartNumber") or not part_number:
        return False
    sql = f"SELECT COUNT(*) FROM [{table}] WHERE [PartNumber] = ?"
    params: list = [part_number]
    if exclude_id is not None:
        sql += f" AND [{t.id_col}] <> ?"
        params.append(_cast_id(t, exclude_id))
    with connect() as cn:
        cur = cn.cursor()
        cur.execute(sql, params)
        return (backends.scalar(cur) or 0) > 0


def table_stats(table: str) -> dict:
    with connect() as cn:
        cur = cn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM [{table}]")
        return {"rows": backends.scalar(cur)}


# --------------------------------------------------------------------------- writes

def backup() -> Path | None:
    """Snapshot the database once per day, before the first write."""
    if not CONFIG.get("backup_on_write", True):
        return None
    src = db_path()
    bdir = src.parent / "backups"
    bdir.mkdir(exist_ok=True)
    pattern = f"{src.stem}_*{src.suffix}"
    today = datetime.now().strftime("%Y%m%d")
    if any(p.name.startswith(f"{src.stem}_{today}") for p in bdir.glob(pattern)):
        return None
    dst = bdir / f"{src.stem}_{datetime.now():%Y%m%d_%H%M%S}{src.suffix}"
    backends.copy_database(src, dst)
    for p in sorted(bdir.glob(pattern))[:-BACKUP_KEEP]:
        p.unlink(missing_ok=True)
    return dst


def _coerce(col: Column, raw):
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "":
        return None
    if col.is_numeric:
        try:
            return float(s.replace(",", ".").replace(" ", ""))
        except ValueError as e:
            raise DbError(f"Поле «{col.name}»: «{raw}» — не число") from e
    if col.maxlength and len(s) > col.maxlength:
        raise DbError(f"Поле «{col.name}»: {len(s)} символов, максимум {col.maxlength}")
    return s


def _cast_id(t: Table, row_id):
    c = t.col(t.id_col)
    if c and (c.is_numeric or c.is_autonumber):
        if c.type_name in ("DOUBLE", "SINGLE", "REAL"):
            return float(row_id)
        return int(float(row_id))
    return row_id


def _payload(t: Table, data: dict) -> dict:
    # MANUAL_ENTRY only reaches here if the dropdown was submitted without JavaScript.
    return {c.name: _coerce(c, data[c.name]) for c in t.editable
            if c.name in data and data[c.name] != MANUAL_ENTRY}


def _friendly(exc: Exception) -> DbError:
    """Engine complaints, translated for the form."""
    text = str(exc)
    if "UNIQUE constraint failed" in text:
        column = text.rsplit(".", 1)[-1].strip()
        return DbError(f"В базе включена проверка уникальности: «{column}» уже занят. "
                       f"Измените значение или снимите уникальный индекс.")
    if "NOT NULL constraint failed" in text:
        return DbError(f"Поле «{text.rsplit('.', 1)[-1].strip()}» обязательно для заполнения")
    return DbError(text)


def insert_row(table: str, data: dict):
    t = get_table(table)
    payload = _payload(t, data)
    if not any(v is not None for v in payload.values()):
        raise DbError("Нечего сохранять — все поля пустые")
    backup()
    with connect() as cn:
        cur = cn.cursor()
        if not t.id_is_autonumber:
            cur.execute(f"SELECT MAX([{t.id_col}]) FROM [{table}]")
            mx = backends.scalar(cur)
            payload[t.id_col] = (int(mx) if mx is not None else 0) + 1
        cols = ", ".join(f"[{k}]" for k in payload)
        marks = ", ".join("?" for _ in payload)
        try:
            cur.execute(f"INSERT INTO [{table}] ({cols}) VALUES ({marks})", list(payload.values()))
        except backends.Error as exc:
            raise _friendly(exc) from exc
        new_id = cur.lastrowid if t.id_is_autonumber else payload[t.id_col]
        cn.commit()
    invalidate_values(table)
    return new_id


def update_row(table: str, row_id, data: dict) -> None:
    t = get_table(table)
    payload = _payload(t, data)
    if not payload:
        return
    backup()

    sets = ", ".join(f"[{k}] = ?" for k in payload)
    with connect() as cn:
        cur = cn.cursor()
        try:
            cur.execute(f"UPDATE [{table}] SET {sets} WHERE [{t.id_col}] = ?",
                        [*payload.values(), _cast_id(t, row_id)])
        except backends.Error as exc:
            raise _friendly(exc) from exc
        cn.commit()
    invalidate_values(table)


# --------------------------------------------------------------------------- find & replace

REPLACE_MODES = {
    "part": "часть значения",
    "whole": "значение целиком",
    "regex": "регулярное выражение",
}


def replace_plan(table: str, column: str, find: str, repl: str, mode: str,
                 case: bool, query: str = "", filters: dict[str, str] | None = None) -> list[dict]:
    """Rows of the current search/filter whose `column` would change: id, label, before, after.

    "part" replaces every occurrence of the text, "whole" only a cell equal to it (an empty
    find matches blank cells, to fill them in), "regex" is Python re.sub with \\1 groups.
    A row whose new value the column cannot take gets "error" instead of failing the lot.
    """
    t = get_table(table)
    col = t.col(column)
    if col is None or col not in t.editable:
        raise DbError(f"В колонку «{column}» писать нельзя")
    if mode not in REPLACE_MODES:
        raise DbError(f"Неизвестный режим {mode!r}")
    if mode != "whole" and find == "":
        raise DbError("Укажите, что искать")
    flags = 0 if case else re.IGNORECASE
    try:
        pattern = re.compile(find if mode == "regex" else re.escape(find), flags)
    except re.error as e:
        raise DbError(f"Ошибка в регулярном выражении: {e}") from e

    def new_value(old: str) -> str | None:
        if mode == "whole":
            same = old == find if case else old.casefold() == find.casefold()
            return repl if same else None
        if not pattern.search(old):
            return None
        try:
            return pattern.sub(repl if mode == "regex" else (lambda _m: repl), old)
        except (re.error, IndexError) as e:
            raise DbError(f"Ошибка в замене: {e}") from e

    where, params = _build_where(t, query, filters or {})
    label = t.label_col if t.label_col != column else t.id_col
    sql = f"SELECT [{t.id_col}], [{label}], [{column}] FROM [{table}]{where} ORDER BY [{t.id_col}]"
    with connect() as cn:
        rows = cn.cursor().execute(sql, params).fetchall()

    out = []
    for row_id, row_label, raw in rows:
        before = "" if raw is None else (f"{raw:.15g}" if isinstance(raw, float) else str(raw))
        after = new_value(before)
        if after is None:
            continue
        item = {"id": row_id, "label": row_label, "before": before, "after": after,
                "value": None, "error": ""}
        try:
            item["value"] = _coerce(col, after)
        except DbError as e:
            item["error"] = str(e)
        if item["error"] or item["value"] != _coerce_quiet(col, before):
            out.append(item)
    return out


def _coerce_quiet(col: Column, raw):
    try:
        return _coerce(col, raw)
    except DbError:
        return raw


def plan_token(changes: list[dict]) -> str:
    """Fingerprint of a preview, so Apply writes exactly what was shown."""
    import hashlib
    blob = "\n".join(f"{c['id']}\t{c['before']}\t{c['value']}" for c in changes)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def replace_apply(table: str, column: str, changes: list[dict]) -> Path:
    """Write a replace_plan in one transaction, after a full snapshot. Returns the snapshot."""
    t = get_table(table)
    if any(c["error"] for c in changes):
        raise DbError("Есть строки с ошибками — исправьте замену")
    src = db_path()
    snapshot = src.parent / "backups" / f"{src.stem}_before-replace_{datetime.now():%Y%m%d_%H%M%S}{src.suffix}"
    snapshot.parent.mkdir(exist_ok=True)
    backends.copy_database(src, snapshot)
    with connect() as cn:
        cur = cn.cursor()
        try:
            for c in changes:
                cur.execute(f"UPDATE [{table}] SET [{column}] = ? WHERE [{t.id_col}] = ?",
                            (c["value"], _cast_id(t, c["id"])))
        except backends.Error as exc:
            cn.rollback()
            raise _friendly(exc) from exc
        cn.commit()
    invalidate_values(table)
    return snapshot


def delete_row(table: str, row_id) -> None:
    t = get_table(table)
    backup()
    with connect() as cn:
        cur = cn.cursor()
        cur.execute(f"DELETE FROM [{table}] WHERE [{t.id_col}] = ?", (_cast_id(t, row_id),))
        cn.commit()
    invalidate_values(table)
