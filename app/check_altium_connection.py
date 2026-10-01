"""Test the SQLite database the same way Altium does — OLE DB (MSDASQL) over ODBC.

    python app/check_altium_connection.py

Altium is a Windows application talking ADO/OLE DB, so a plain sqlite3 check proves nothing
about whether Altium can connect. This walks the exact same path and says which link failed:
the driver, the connection string, or the query.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "GH_DB_LIB.sqlite"

# sqliteodbc registers several names; prefer the version 3 UTF-8 one.
PREFERRED_DRIVERS = (
    "SQLite3 ODBC Driver",
    "SQLite ODBC (UTF-8) Driver",
    "SQLite ODBC Driver",
)
DOWNLOAD = "http://www.ch-werner.de/sqliteodbc/"


def w(text: str = "") -> None:
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))


def installed_drivers() -> list[str]:
    """ODBC drivers registered for the 64-bit subsystem, where Altium lives."""
    import winreg
    names: list[str] = []
    for view in (winreg.KEY_WOW64_64KEY,):
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"SOFTWARE\ODBC\ODBCINST.INI\ODBC Drivers",
                                 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                if str(value).lower() == "installed":
                    names.append(name)
                i += 1
    return names


def pick_driver(drivers: list[str]) -> str | None:
    for candidate in PREFERRED_DRIVERS:
        for name in drivers:
            if name.lower() == candidate.lower():
                return name
    return next((n for n in drivers if "sqlite" in n.lower()), None)


def connection_string(driver: str, db: Path) -> str:
    # LongNames=0: with it on, sqliteodbc reports every column as "CAP_YAGEO.PartNumber",
    #   and Altium shows the table name in each parameter.
    # NoTXN=1: harmless for Altium, which only reads. It does NOT stop Altium from holding a
    #   read lock for as long as the library is loaded — WAL journal mode is what lets writers
    #   commit past that (see README, "Блокировка при открытом Altium").
    return (
        "Provider=MSDASQL.1;Persist Security Info=False;"
        f'Extended Properties="DRIVER={driver};Database={db};'
        'LongNames=0;NoTXN=1;Timeout=1000;SyncPragma=NORMAL;StepAPI=0;"'
    )


def writable(db: Path) -> bool:
    """Could the web UI commit right now? Takes the lock a commit needs and gives it back."""
    import sqlite3
    cn = sqlite3.connect(str(db), timeout=1.0, isolation_level=None)
    try:
        cn.execute("BEGIN EXCLUSIVE")
        cn.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        cn.close()


def main() -> int:
    db = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_DB
    w(f"база: {db}")
    if not db.is_file():
        w("  НЕТ ФАЙЛА — база должна лежать рядом с проектом")
        return 1
    w(f"  размер: {db.stat().st_size / 1e6:.2f} МБ\n")

    drivers = installed_drivers()
    sqlite_drivers = [d for d in drivers if "sqlite" in d.lower()]
    w("ODBC-драйверы SQLite (64-бит):")
    if not sqlite_drivers:
        w("  НЕ НАЙДЕНЫ — это и есть причина «Connection Failed» в Altium.")
        w(f"  Поставьте 64-битный sqliteodbc_w64.exe: {DOWNLOAD}")
        w("\n  Установленные драйверы, для справки:")
        for name in sorted(drivers):
            w(f"    {name}")
        return 1
    for name in sqlite_drivers:
        w(f"  {name}")

    driver = pick_driver(drivers)
    cs = connection_string(driver, db)
    w(f"\nвыбран драйвер: {driver}")

    try:
        import win32com.client
    except ImportError:
        w("нет pywin32 — не могу проверить через OLE DB (pip install pywin32)")
        return 1

    import sqlite3
    probe = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    journal = probe.execute("PRAGMA journal_mode").fetchone()[0]
    probe.close()
    w(f"  режим журнала: {journal}" + ("" if journal == "wal" else
      " — пока библиотека открыта в Altium, записать не получится; нужен WAL"))
    if not writable(db):
        w("\nСейчас записать в базу нельзя: другой процесс (обычно загруженная в Altium")
        w("библиотека) держит чтение открытым." + ("" if journal == "wal" else
          " В режиме WAL это бы не мешало."))

    w("\nподключение через MSDASQL (так же, как Altium):")
    try:
        cn = win32com.client.Dispatch("ADODB.Connection")
        cn.Open(cs)
    except Exception as exc:
        w(f"  ОШИБКА: {exc}")
        w("  Строка подключения, которую пробовал:")
        w(f"    {cs}")
        return 1
    w("  подключение установлено")

    try:
        for table in ("CAP_YAGEO", "RES_YAGEO_RC_L"):
            rs = cn.Execute(f"SELECT COUNT(*) FROM [{table}]")[0]
            w(f"  {table}: {rs.Fields.Item(0).Value} строк")
        rs = cn.Execute("SELECT * FROM [CAP_YAGEO] WHERE [ID] = 1")[0]
        names = [rs.Fields.Item(i).Name for i in range(rs.Fields.Count)]
        prefixed = [n for n in names if "." in n]
        w(f"  колонок CAP_YAGEO: {len(names)}"
          + (f" — ВНИМАНИЕ, с префиксом таблицы: {prefixed[:3]}" if prefixed else ", имена чистые"))
        w(f"  пробная строка: PartNumber = {rs.Fields.Item('PartNumber').Value!r}")
    except Exception as exc:
        w(f"  запрос не прошёл: {exc}")
        return 1
    finally:
        try:
            cn.Close()
        except Exception:
            pass

    w("\nВсё работает. Строка для Altium (Use Connection String):")
    w(f"\n{cs}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
