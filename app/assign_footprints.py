"""Fill the density variants of every part's footprint from the .PcbLib.

    python app/assign_footprints.py            # show what would change
    python app/assign_footprints.py --apply    # change it

Template, by the size in the first word of Package and the prefix in Category:

    Footprint Ref     CAP: 0201       medium — the default footprint
    Footprint Ref 2   CAP: 0201 HD    high density
    Footprint Ref 3   CAP: 0201 UHD   minimal pad, for RF lines

A slot is filled only when the .PcbLib actually has that footprint. The medium one must match
exactly — otherwise "CAP: 0201" would also catch "CAP: 0201 HD ...". HD and UHD also accept a
library name that matches ignoring spaces and carries a tail, such as "CAP: 0201HD CAPC0603X03L"
or "CAP: 0402 HD C1005X04L", as long as exactly one name does. When a variant is missing the
current value is left alone and the report lists it, so the library can be brought in line
and this re-run: every run starts again from the library.

SQLite only. Adds Footprint Ref 3 / Footprint Path 3 when a table lacks them.
"""
from __future__ import annotations

import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import backends
import footprints
from backends import locked_by_someone, q

ROOT = Path(__file__).resolve().parent.parent
TABLES = ("CAP_YAGEO", "RES_YAGEO_RC_L", "IND")

# slot column, its path column, template suffix, whether a tail after the name is allowed
SLOTS = (
    ("Footprint Ref", "Footprint Path", "", False),
    ("Footprint Ref 2", "Footprint Path 2", " HD", True),
    ("Footprint Ref 3", "Footprint Path 3", " UHD", True),
)


def log(msg: str = "") -> None:
    sys.stdout.buffer.write((msg + "\n").encode("utf-8"))


def squash(name: str) -> str:
    return "".join(name.split()).casefold()


def find_variant(template: str, library: list[str], tail_ok: bool) -> tuple[str | None, str]:
    """(footprint name, how it matched) — or (None, reason)."""
    if template in library:
        return template, "точно"
    if not tail_ok:
        return None, "нет в библиотеке"
    key = squash(template)
    hits = [n for n in library if squash(n).startswith(key)]
    if len(hits) == 1:
        return hits[0], "без учёта пробелов"
    if hits:
        return None, "неоднозначно: " + ", ".join(hits)
    return None, "нет в библиотеке"


def size_of(package: str | None) -> str | None:
    return package.split()[0] if package and package.split() else None


def plan_table(cn: sqlite3.Connection, table: str):
    cols = {r[1] for r in cn.execute(f"PRAGMA table_info({q(table)})")}
    rows = cn.execute(
        f'SELECT "ID", "Category", "Package", "Footprint Path", '
        + ", ".join(f"{q(ref)}" if ref in cols else "NULL" for ref, *_ in SLOTS)
        + f" FROM {q(table)}").fetchall()

    library_cache: dict[str, list[str]] = {}
    updates: dict[int, dict[str, str]] = {}          # ID -> {column: value}
    summary: dict[tuple, Counter] = defaultdict(Counter)
    missing: dict[str, set[str]] = defaultdict(set)   # library -> templates it lacks
    skipped: Counter = Counter()

    for row_id, category, package, lib_file, *current in rows:
        size = size_of(package)
        if not (category and size and lib_file):
            skipped["нет Category, Package или Footprint Path"] += 1
            continue
        if lib_file not in library_cache:
            path = footprints.find_library(lib_file)
            library_cache[lib_file] = footprints.read_pcblib(path) if path else []
        library = library_cache[lib_file]
        if not library:
            skipped[f"не прочитана библиотека {lib_file}"] += 1
            continue

        for (ref_col, path_col, suffix, tail_ok), now in zip(SLOTS, current):
            template = f"{category}: {size}{suffix}"
            found, how = find_variant(template, library, tail_ok)
            if found is None:
                missing[lib_file].add(f"{template}  ({how})" if how != "нет в библиотеке" else template)
                summary[(size, ref_col, now or "—", now or "—", "нет варианта — не трогаю")][table] += 1
                continue
            if found != now:
                updates.setdefault(row_id, {})[ref_col] = found
                updates[row_id][path_col] = lib_file
            summary[(size, ref_col, now or "—", found, how)][table] += 1

    return cols, rows, updates, summary, missing, skipped


def by_size(table: str, summary) -> list[tuple[str, list[tuple]]]:
    """[(size, [(slot column, before, after, how, count), ...]), ...], smallest size first."""
    groups = defaultdict(list)
    for (size, ref_col, before, after, how), n in summary.items():
        groups[size].append((ref_col, before, after, how, n[table]))
    return [(size, sorted(groups[size])) for size in sorted(groups, key=lambda s: (len(s), s))]


def missing_sorted(names: set[str]) -> list[str]:
    return sorted(names, key=lambda s: (len(s.split()[1]), s))


def report(table: str, rows, updates, summary, missing, skipped) -> None:
    log(f"\n===== {table}: {len(rows)} записей, меняется {len(updates)}")
    for size, slots in by_size(table, summary):
        log(f"  {size}")
        for ref_col, before, after, how, n in slots:
            arrow = "без изменений" if before == after else f"{before} → {after}"
            log(f"    {ref_col:<16} {arrow:<48} [{how}] × {n}")
    for lib, names in missing.items():
        log(f"  не хватает в {lib}: " + "; ".join(missing_sorted(names)))
    for why, n in skipped.items():
        log(f"  пропущено {n}: {why}")


def plan(db: Path) -> dict:
    """{table: plan_table(...)} for every table this script handles that exists in db."""
    cn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {t: plan_table(cn, t) for t in TABLES
                if cn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone()}
    finally:
        cn.close()


def pending(plans: dict) -> tuple[int, bool]:
    """(rows to change, whether a footprint column has to be added)."""
    rows = sum(len(p[2]) for p in plans.values())
    new_cols = any(ref not in p[0] or path not in p[0]
                   for p in plans.values() for ref, path, *_ in SLOTS)
    return rows, new_cols


def apply(db: Path, plans: dict, out=log) -> tuple[bool, Path]:
    """Write the plans; (every row verified, snapshot taken before the change)."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot = db.parent / "backups" / f"{db.stem}_before-footprints_{stamp}{db.suffix}"
    snapshot.parent.mkdir(exist_ok=True)
    backends.copy_database(db, snapshot)

    cn = sqlite3.connect(str(db), timeout=15, isolation_level=None)
    try:
        cn.execute("BEGIN IMMEDIATE")
        for table, (cols, _rows, updates, *_rest) in plans.items():
            for ref_col, path_col, *_ in SLOTS:
                for col in (ref_col, path_col):
                    if col not in cols:
                        cn.execute(f"ALTER TABLE {q(table)} ADD COLUMN {q(col)} TEXT")
                        out(f"  {table}: добавлена колонка {col}")
            for row_id, values in updates.items():
                sets = ", ".join(f"{q(c)} = ?" for c in values)
                cn.execute(f'UPDATE {q(table)} SET {sets} WHERE "ID" = ?', [*values.values(), row_id])
        cn.execute("COMMIT")
    except Exception:
        cn.execute("ROLLBACK")
        raise
    finally:
        cn.close()

    # Every row: planned values landed, and nothing else moved.
    old = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    new = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    ok = True
    def load(conn: sqlite3.Connection, table: str) -> dict[int, dict]:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({q(table)})")]
        return {row[cols.index("ID")]: dict(zip(cols, row))
                for row in conn.execute(f"SELECT {', '.join(q(c) for c in cols)} FROM {q(table)}")}

    for table, (_cols, _rows, updates, *_rest) in plans.items():
        before, after = load(old, table), load(new, table)
        bad = 0 if set(before) == set(after) else 1
        for row_id, got in after.items():
            # New columns start empty; then the planned values; everything else as it was.
            expected = {c: None for c in got}
            expected.update(before.get(row_id, {}))
            expected.update(updates.get(row_id, {}))
            bad += sum(1 for c in got if got[c] != expected[c])
        ok &= not bad
        out(f"  сверка {table}: " + ("OK" if not bad else f"ОШИБКА, несовпадений {bad}"))
    old.close()
    new.close()
    out(f"\nкопия до изменений: {snapshot}")
    return ok, snapshot


def main() -> int:
    do_apply = "--apply" in sys.argv[1:]
    paths = [a for a in sys.argv[1:] if not a.startswith("--")]
    db = Path(paths[0]).resolve() if paths else ROOT / "GH_DB_LIB.sqlite"
    log(f"база: {db}")

    plans = plan(db)
    for table, (_cols, rows, updates, summary, missing, skipped) in plans.items():
        report(table, rows, updates, summary, missing, skipped)

    total, new_cols = pending(plans)
    if not total and not new_cols:
        log("\nменять нечего")
        return 0
    if not do_apply:
        log(f"\nэто предварительный просмотр; записать: python app/assign_footprints.py --apply")
        return 0
    if locked_by_someone(db):
        log("\nбаза занята другим процессом — ничего не изменено")
        return 1
    log("\nзапись:")
    ok, _snapshot = apply(db, plans)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
