"""Create the IND table (ferrite beads and inductors) and fill it with whole manufacturer series.

    python app/import_inductors.py            # show what would be added
    python app/import_inductors.py --apply    # add it

Series and where every number comes from:

    BLM03PX   Murata  spec JENF243A_0020AK-01 (BLM03_SN)       4 parts
    BLM15EG   Murata  spec JENF243A-0023N-01 (BLM15E_SN)        2 parts
    BLM15GG   Murata  spec JENF243A-0029E-01 (BLM15G_SN)        2 parts
    LQH2MCN   Murata  data sheet LQH2MC_02 series (0806)       20 parts
    MMZ0603S  TDK     TDK Product Center, all "Production"     28 parts
    CB2012T   Taiyo Yuden  TY-COMPAS; now sold as LSQNA201212T…,
                      the CB2012T number is kept in Description 10 parts
    CBMF1608T Taiyo Yuden  catalog DS/IND/DOC012844074.pdf; now LSQNB160808T…
                      (CBMF1608T number kept in Description)    10 parts

Rated current of Murata beads is the 85 °C value when the spec gives one, else the 125 °C one;
DC resistance is the initial maximum. Distributor numbers and prices are left empty — the
DigiKey site could not be read.

Rows are matched on PartNumber: existing ones are left alone, so re-running only adds what is
missing. The table gets the same Altium columns as CAP/RES; footprints are then filled by
assign_footprints.py (IND: 0201 / HD / UHD and so on). Needs SQLite.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import backends
from backends import q

ROOT = Path(__file__).resolve().parent.parent
TABLE = "IND"

COLUMNS = (
    ("ID", "INTEGER PRIMARY KEY"),
    ("PartNumber", "TEXT"),
    ("Manufacturer", "TEXT"),
    ("Series", "TEXT"),
    ("Description", "TEXT"),
    ("Unit Price (USD)", "REAL"),
    ("Value", "TEXT"),
    ("Tolerance", "TEXT"),
    ("Impedance @ 1GHz", "TEXT"),
    ("Current Rating (Amps)", "TEXT"),
    ("Current - Saturation (Isat)", "TEXT"),
    ("DC Resistance (DCR)", "TEXT"),
    ("Frequency - Self Resonant", "TEXT"),
    ("Grade", "TEXT"),
    ("Operating Temperature", "TEXT"),
    ("Package", "TEXT"),
    ("Size / Dimension", "TEXT"),
    ("Height - Seated (Max)", "TEXT"),
    ("Library Ref", "TEXT"),
    ("Footprint Ref", "TEXT"),
    ("Library Path", "TEXT"),
    ("Footprint Path", "TEXT"),
    ("Supplier 1", "TEXT"),
    ("Supplier Part Number 1", "TEXT"),
    ("ComponentLink1URL", "TEXT"),
    ("ComponentLink1Description", "TEXT"),
    ("ImageURL", "TEXT"),
    ("Category", "TEXT"),
    ("CategorySub", "TEXT"),
    ("Zone", "TEXT"),
    ("Footprint Ref 2", "TEXT"),
    ("Footprint Path 2", "TEXT"),
    ("Footprint Ref 3", "TEXT"),
    ("Footprint Path 3", "TEXT"),
)

ZONE = "=Copy(DocumentName,1,length(DocumentName)-7)"

# package, size, seated height — DigiKey wording, like the CAP/RES tables
BODY = {
    "0201": ("0201 (0603 Metric)", '0.024" L x 0.012" W (0.60mm x 0.30mm)', '0.013" (0.33mm)'),
    "0402": ("0402 (1005 Metric)", '0.039" L x 0.020" W (1.00mm x 0.50mm)', '0.022" (0.55mm)'),
    "0806": ("0806 (2016 Metric)", '0.079" L x 0.063" W (2.00mm x 1.60mm)', '0.037" (0.95mm)'),
    "0805": ("0805 (2012 Metric)", '0.079" L x 0.049" W (2.00mm x 1.25mm)', '0.057" (1.45mm)'),
    "0603": ("0603 (1608 Metric)", '0.063" L x 0.031" W (1.60mm x 0.80mm)', '0.039" (1.00mm)'),
}

# --------------------------------------------------------------------- source data

# Murata beads: part, Z@100MHz, tolerance, Z@1GHz, rated mA, DCR max (Ω)
MURATA_BEADS = {
    "BLM03PX": ("0201", [
        ("BLM03PX220SN1D", 22, "±25%", None, 1800, 0.040),
        ("BLM03PX330SN1D", 33, "±25%", None, 1500, 0.055),
        ("BLM03PX800SN1D", 80, "±25%", None, 1000, 0.130),
        ("BLM03PX121SN1D", 120, "±25%", None, 900, 0.160),
    ]),
    "BLM15EG": ("0402", [
        ("BLM15EG121SN1D", 120, "±25%", "145Ω", 1500, 0.095),
        ("BLM15EG221SN1D", 220, "±25%", "270Ω", 700, 0.28),
    ]),
    "BLM15GG": ("0402", [
        ("BLM15GG221SN1D", 220, "±25%", "600Ω ±40%", 300, 0.7),
        ("BLM15GG471SN1D", 470, "±25%", "1200Ω ±40%", 200, 1.3),
    ]),
}

# TDK MMZ0603S: part, Z@100MHz, Z@1GHz, rated A, DCR max (Ω) — all ±25%
TDK_MMZ = [
    ("MMZ0603S100CT000", 10, None, 1.0, 0.05),
    ("MMZ0603S100CTD25", 10, None, 1.0, 0.05),
    ("MMZ0603S102ET000", 1000, 1800, 0.125, 2.6),
    ("MMZ0603S102HT000", 1000, None, 0.2, 1.25),
    ("MMZ0603S102HTD25", 1000, None, 0.2, 1.25),
    ("MMZ0603S121CT000", 120, None, 0.2, 0.45),
    ("MMZ0603S121CTD25", 120, None, 0.2, 0.45),
    ("MMZ0603S121ET000", 120, 200, 0.25, 0.37),
    ("MMZ0603S121HT000", 120, None, 0.48, 0.22),
    ("MMZ0603S121HTD25", 120, None, 0.48, 0.22),
    ("MMZ0603S241CT000", 240, None, 0.2, 0.57),
    ("MMZ0603S241CTD25", 240, None, 0.2, 0.57),
    ("MMZ0603S241ET000", 240, 400, 0.2, 0.71),
    ("MMZ0603S241HT000", 240, None, 0.42, 0.32),
    ("MMZ0603S241HTD25", 240, None, 0.42, 0.32),
    ("MMZ0603S471CT000", 470, None, 0.1, 1.3),
    ("MMZ0603S471CTD25", 470, None, 0.1, 1.3),
    ("MMZ0603S471HT000", 470, None, 0.31, 0.65),
    ("MMZ0603S471HTD25", 470, None, 0.31, 0.65),
    ("MMZ0603S601CT000", 600, None, 0.1, 1.45),
    ("MMZ0603S601CTD25", 600, None, 0.1, 1.45),
    ("MMZ0603S601ET000", 600, 1000, 0.15, 1.6),
    ("MMZ0603S601HT000", 600, None, 0.28, 0.75),
    ("MMZ0603S601HTD25", 600, None, 0.28, 0.75),
    ("MMZ0603S800CT000", 80, None, 0.2, 0.3),
    ("MMZ0603S800CTD25", 80, None, 0.2, 0.3),
    ("MMZ0603S800HT000", 80, None, 0.52, 0.18),
    ("MMZ0603S800HTD25", 80, None, 0.52, 0.18),
]

# Murata LQH2MCN_02: code, µH, tolerance, rated mA, DCR (Ω, ±30%), SRF min MHz
LQH2MCN = [
    ("1R0M", 1.0, 20, 485, 0.30, 100), ("1R5M", 1.5, 20, 445, 0.40, 95),
    ("2R2M", 2.2, 20, 425, 0.48, 70), ("3R3M", 3.3, 20, 375, 0.60, 65),
    ("4R7M", 4.7, 20, 300, 0.8, 60), ("5R6M", 5.6, 20, 280, 0.9, 60),
    ("6R8M", 6.8, 20, 255, 1.0, 55), ("8R2M", 8.2, 20, 235, 1.1, 50),
    ("100K", 10, 10, 225, 1.2, 48), ("120K", 12, 10, 210, 1.4, 44),
    ("150K", 15, 10, 200, 1.6, 40), ("180K", 18, 10, 190, 1.8, 35),
    ("220K", 22, 10, 185, 2.1, 30), ("270K", 27, 10, 180, 2.5, 30),
    ("330K", 33, 10, 160, 2.8, 28), ("390K", 39, 10, 125, 4.4, 24),
    ("470K", 47, 10, 120, 5.1, 18), ("560K", 56, 10, 110, 5.7, 17),
    ("680K", 68, 10, 100, 6.6, 14), ("820K", 82, 10, 90, 7.5, 14),
]

# Taiyo Yuden LSQNA201212T (was CB2012T): code, µH, tol %, rated A (= Isat, ΔL 30 %),
# temperature-rise A (ΔT 40 °C), DCR max Ω, SRF min MHz
CB2012T = [
    ("1R0M", 1.0, 20, 0.5, 0.9, 0.195, 100), ("2R2M", 2.2, 20, 0.41, 0.77, 0.299, 80),
    ("3R3M", 3.3, 20, 0.33, 0.65, 0.39, 55), ("4R7M", 4.7, 20, 0.3, 0.58, 0.52, 45),
    ("6R8M", 6.8, 20, 0.25, 0.54, 0.611, 38), ("100K", 10, 10, 0.19, 0.44, 0.91, 32),
    ("100M", 10, 20, 0.19, 0.44, 0.91, 32), ("150K", 15, 10, 0.17, 0.32, 1.69, 28),
    ("150M", 15, 20, 0.17, 0.32, 1.69, 28), ("220M", 22, 20, 0.135, 0.28, 2.21, 16),
]

# Taiyo Yuden LSQNB160808T (was CBMF1608T), same columns. Source: catalog "Wire-wound chip
# power inductors (CB series)" (DS/IND/DOC012844074.pdf); its DCR is nominal ±30 %, here the
# maximum (×1.3), which is how TY-COMPAS lists it — checked against 1R0M, 3R3M, 4R7M, 100M.
CBMF1608T = [
    ("1R0M", 1.0, 20, 0.29, 0.77, 0.117, 100), ("2R2M", 2.2, 20, 0.19, 0.56, 0.221, 80),
    ("3R3M", 3.3, 20, 0.17, 0.5, 0.286, 60), ("4R7M", 4.7, 20, 0.145, 0.47, 0.312, 45),
    ("100K", 10, 10, 0.115, 0.38, 0.468, 32), ("100M", 10, 20, 0.115, 0.38, 0.468, 32),
    ("220K", 22, 10, 0.07, 0.23, 1.3, 16), ("220M", 22, 20, 0.07, 0.23, 1.3, 16),
    ("470K", 47, 10, 0.05, 0.14, 3.25, 11), ("470M", 47, 20, 0.05, 0.14, 3.25, 11),
]

# series -> (current prefix, former prefix, size, data)
TAIYO = {
    "CB2012T": ("LSQNA201212T", "CB2012T", "0805", CB2012T),
    "CBMF1608T": ("LSQNB160808T", "CBMF1608T", "0603", CBMF1608T),
}

# --------------------------------------------------------------------- formatting


def amps(a: float) -> str:
    return f"{a:g}A" if a >= 1 else f"{round(a * 1000):g}mA"


def ohms(r: float) -> str:
    return f"{r * 1000:g}mOhm" if r < 1 else f"{r:g}Ohm"


def uh(v: float) -> str:
    return f"{v:g}µH"


def base(size: str) -> dict:
    package, dim, height = BODY[size]
    return {"Package": package, "Size / Dimension": dim, "Height - Seated (Max)": height,
            "Library Path": "IND_DB.SchLib", "Footprint Path": "IND.PcbLib",
            "ComponentLink1Description": "Datasheet", "Category": "IND", "Zone": ZONE}


def bead(pn, maker, series, size, z, tol, z1g, amps_, dcr, url, grade="Commercial",
         temp="-55°C ~ 125°C") -> dict:
    return {**base(size), "PartNumber": pn, "Manufacturer": maker, "Series": series,
            "Description": f"FERRITE BEAD {z} OHM {size}",
            "Value": f"{z}Ω", "Tolerance": tol, "Impedance @ 1GHz": z1g,
            "Current Rating (Amps)": amps(amps_), "DC Resistance (DCR)": ohms(dcr),
            "Grade": grade, "Operating Temperature": temp,
            "Library Ref": "IND: Ferrite Bead", "CategorySub": "Ferrite Beads",
            "ComponentLink1URL": url}


def rows() -> list[dict]:
    out = []
    for series, (size, parts) in MURATA_BEADS.items():
        for pn, z, tol, z1g, ma, dcr in parts:
            url = f"https://www.murata.com/en-us/products/productdetail?partno={pn[:-1]}%23"
            out.append(bead(pn, "Murata", series, size, z, tol, z1g, ma / 1000, dcr, url))

    for pn, z, z1g, a, dcr in TDK_MMZ:
        url = f"https://product.tdk.com/en/search/emc/emc/beads/info?part_no={pn}"
        out.append(bead(pn, "TDK", "MMZ0603S", "0201", z, "±25%",
                        f"{z1g}Ω" if z1g else None, a, dcr, url,
                        grade="Automotive AEC-Q200" if pn.endswith("D25") else "Commercial"))

    for code, l, tol, ma, dcr, srf in LQH2MCN:
        pn = f"LQH2MCN{code}02L"
        out.append({**base("0806"), "PartNumber": pn, "Manufacturer": "Murata",
                    "Series": "LQH2MCN",
                    "Description": f"FIXED IND {l:g}UH {ma}MA {dcr:g} OHM SMD",
                    "Value": uh(l), "Tolerance": f"±{tol}%",
                    "Current Rating (Amps)": amps(ma / 1000),
                    "DC Resistance (DCR)": f"{ohms(dcr)} ±30%",
                    "Frequency - Self Resonant": f"{srf}MHz", "Grade": "Commercial",
                    "Operating Temperature": "-40°C ~ 85°C",
                    "Library Ref": "IND: FIX", "CategorySub": "Fixed Inductors",
                    "Footprint Ref": "LQH2MCN100K02L",
                    "ComponentLink1URL":
                        f"https://www.murata.com/en-us/products/productdetail?partno={pn}"})

    for series, (prefix, former, size, parts) in TAIYO.items():
      for code, l, tol, a, a_temp, dcr, srf in parts:
        pn, old = f"{prefix}{code}", f"{former}{code}"
        out.append({**base(size), "PartNumber": pn, "Manufacturer": "Taiyo Yuden",
                    "Series": series,
                    "Description": f"FIXED IND {l:g}UH {round(a * 1000)}MA {dcr:g} OHM SMD ({old})",
                    "Value": uh(l), "Tolerance": f"±{tol}%",
                    "Current Rating (Amps)": amps(a),
                    "Current - Saturation (Isat)": amps(a),
                    "DC Resistance (DCR)": ohms(dcr),
                    "Frequency - Self Resonant": f"{srf}MHz", "Grade": "Commercial",
                    "Operating Temperature": "-40°C ~ 105°C",
                    "Library Ref": "IND: FIX", "CategorySub": "Fixed Inductors",
                    "ComponentLink1URL":
                        f"https://ds.yuden.co.jp/TYCOMPAS/or/detail?pn={pn}&u=M"})
    return out


# --------------------------------------------------------------------- database


def log(msg: str = "") -> None:
    sys.stdout.buffer.write((msg + "\n").encode("utf-8"))


def create_table(cn: sqlite3.Connection) -> None:
    cols = ",\n  ".join(f"{q(name)} {decl}" for name, decl in COLUMNS)
    cn.execute(f"CREATE TABLE {q(TABLE)} (\n  {cols}\n)")
    cn.execute(f'CREATE UNIQUE INDEX "ux_{TABLE}_pn" ON {q(TABLE)}("PartNumber")')


def main() -> int:
    do_apply = "--apply" in sys.argv[1:]
    paths = [a for a in sys.argv[1:] if not a.startswith("--")]
    db = Path(paths[0]).resolve() if paths else ROOT / "GH_DB_LIB.sqlite"

    data = rows()
    pns = [r["PartNumber"] for r in data]
    assert len(pns) == len(set(pns)), "duplicate PartNumber in the source data"
    known = {name for name, _ in COLUMNS}
    assert all(set(r) <= known for r in data), "row has a column the table lacks"

    cn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    exists = cn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (TABLE,)).fetchone() is not None
    have = ({r[0] for r in cn.execute(f"SELECT PartNumber FROM {q(TABLE)}")}
            if exists else set())
    cn.close()
    new = [r for r in data if r["PartNumber"] not in have]

    log(f"база: {db}")
    log(f"таблица {TABLE}: " + ("есть" if exists else "будет создана")
        + f", добавится {len(new)} из {len(data)}")
    by_series: dict[str, int] = {}
    for r in new:
        by_series[r["Series"]] = by_series.get(r["Series"], 0) + 1
    for s, n in by_series.items():
        log(f"  {s:<9} {n}")
    if not new:
        return 0
    if not do_apply:
        log("\nэто предварительный просмотр; записать: python app/import_inductors.py --apply")
        return 0

    snapshot = db.parent / "backups" / f"{db.stem}_before-ind_{datetime.now():%Y%m%d_%H%M%S}{db.suffix}"
    snapshot.parent.mkdir(exist_ok=True)
    backends.copy_database(db, snapshot)

    cn = sqlite3.connect(str(db), timeout=15, isolation_level=None)
    try:
        cn.execute("BEGIN IMMEDIATE")
        if not exists:
            create_table(cn)
        for r in new:
            cn.execute(f"INSERT INTO {q(TABLE)} ({', '.join(q(c) for c in r)}) "
                       f"VALUES ({', '.join('?' for _ in r)})", list(r.values()))
        cn.execute("COMMIT")
    except Exception:
        cn.execute("ROLLBACK")
        raise
    finally:
        cn.close()

    cn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    got = cn.execute(f"SELECT COUNT(*) FROM {q(TABLE)}").fetchone()[0]
    cn.close()
    log(f"\nзаписано; в {TABLE} теперь {got} строк")
    log(f"копия до изменений: {snapshot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
