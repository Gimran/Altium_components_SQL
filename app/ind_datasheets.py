"""Datasheet links for the IND table — Compel first, then Promelec, then LCSC.

    python app/ind_datasheets.py            # show what would change
    python app/ind_datasheets.py --apply    # write ComponentLink1URL

Collected on 2026-09-28 through the sds.compel.ru API (Search2/Load2 → Infosheet/Data → pdfs),
office.promelec.ru (ajax-search-apcu → ajax-goods-id → DATASHEET) and LCSC product detail.
Every link was checked: either the PDF is named after the part itself, or the part number
was found in the PDF's text (series catalogs). All of them open without a login.
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

C = "https://sds.compel.ru/item-pdf/"
BLM03_SPEC = C + "fc62d5bdc0cd19a8c8c1e92047ac90e9/pn/mur~blm03ax800sn1d.pdf"
MMZ_E = C + "b5c4927c159edf9da7f61f4ad6ab18ed/pn/tdk~mmz0603a121et000.pdf"
MMZ_H = C + "356fbd9a17289737aa3f496323b9bfc3/pn/tdk~mmz0603s102ht000.pdf"
MMZ_HTD25 = C + "84c7bedcdb8d852d5062c165ca024f92/pn/tdk~mmz0603s102htd25.pdf"
MMZ_CTD25 = C + "934b93048fbcb2f7f7413443ff3ded62/pn/tdk~mmz0603d121ctd25.pdf"
MMZ_C = ("https://cdn.promelec.ru/upload/grab/product.tdk.com/info/en/catalog/datasheets/"
         "beads_commercial_signal_mmz0603_en.pdf")
CB_100K = C + "50ba28ff41363c63c5b16509b83960df/pn/taiyo~cb2012t100k.pdf"
CB_100M = C + "0d9504602845c7f4709f4148602bc737/pn/taiyo~cb2012t100m.pdf"
CB_CATALOG_LCSC = ("https://datasheet.lcsc.com/datasheet/pdf/c91387d7655bc26824623d8b8c050379.pdf"
                   "?productCode=C223161")

# part -> (url, source); "own" = PDF of this very part, "series" = catalog that lists it
LINKS = {
    "BLM03PX220SN1D": (BLM03_SPEC, "Compel, series"),
    "BLM03PX330SN1D": (C + "5892f1abd025547c2ca9d8852fbeb00c/pn/mur~blm03ag102sn1d.pdf", "Compel, series"),
    "BLM03PX800SN1D": (BLM03_SPEC, "Compel, series"),
    "BLM03PX121SN1D": (BLM03_SPEC, "Compel, series"),
    "BLM15EG121SN1D": (C + "398a1f3839b4050eebf75bdca406cbae/pn/mur~blm15eg121sn1d.pdf", "Compel, own"),
    "BLM15EG221SN1D": (C + "de6b601369ccc76aebcc1e10acde243c/pn/mur~blm15eg221sn1d.pdf", "Compel, own"),
    "BLM15GG221SN1D": (C + "bd05b9ffa442c4cb5641c74edb9571ba/pn/mur~blm15gg221sn1d.pdf", "Compel, own"),
    "BLM15GG471SN1D": (C + "df8251173aeb682c1c6dc6479a31138b/pn/mur~blm15gg471sn1d.pdf", "Compel, own"),
    "MMZ0603S100CT000": (MMZ_C, "Promelec, series"),
    "MMZ0603S100CTD25": (MMZ_CTD25, "Compel, series"),
    "MMZ0603S102ET000": (MMZ_E, "Compel, series"),
    "MMZ0603S102HT000": (C + "356fbd9a17289737aa3f496323b9bfc3/pn/tdk~mmz0603s102ht000.pdf", "Compel, own"),
    "MMZ0603S102HTD25": (C + "84c7bedcdb8d852d5062c165ca024f92/pn/tdk~mmz0603s102htd25.pdf", "Compel, own"),
    "MMZ0603S121CT000": (MMZ_C, "Promelec, series"),
    "MMZ0603S121CTD25": (MMZ_CTD25, "Compel, series"),
    "MMZ0603S121ET000": (MMZ_E, "Compel, series"),
    "MMZ0603S121HT000": (MMZ_H, "Compel, series"),
    "MMZ0603S121HTD25": (MMZ_HTD25, "Compel, series"),
    "MMZ0603S241CT000": (MMZ_C, "Promelec, series"),
    "MMZ0603S241CTD25": (MMZ_CTD25, "Compel, series"),
    "MMZ0603S241ET000": (MMZ_E, "Compel, series"),
    "MMZ0603S241HT000": (MMZ_H, "Compel, series"),
    "MMZ0603S241HTD25": (MMZ_HTD25, "Compel, series"),
    "MMZ0603S471CT000": (MMZ_C, "Promelec, series"),
    "MMZ0603S471CTD25": (MMZ_CTD25, "Compel, series"),
    "MMZ0603S471HT000": (MMZ_H, "Compel, series"),
    "MMZ0603S471HTD25": (MMZ_HTD25, "Compel, series"),
    "MMZ0603S601CT000": (MMZ_C, "Promelec, series"),
    "MMZ0603S601CTD25": (MMZ_CTD25, "Compel, series"),
    "MMZ0603S601ET000": (MMZ_E, "Compel, series"),
    "MMZ0603S601HT000": (MMZ_H, "Compel, series"),
    "MMZ0603S601HTD25": (MMZ_HTD25, "Compel, series"),
    "MMZ0603S800CT000": (MMZ_C, "Promelec, series"),
    "MMZ0603S800CTD25": (MMZ_CTD25, "Compel, series"),
    "MMZ0603S800HT000": (MMZ_H, "Compel, series"),
    "MMZ0603S800HTD25": (MMZ_HTD25, "Compel, series"),
    "LQH2MCN1R0M02L": (C + "b1bf5b8494bcc2b9387cbde9c43937d5/pn/mur~lqh2mcn1r0m02l.pdf", "Compel, own"),
    "LQH2MCN1R5M02L": (C + "65318cabea71696d4ef2df59d1f5dd2f/pn/mur~lqh2mcn1r5m02l.pdf", "Compel, own"),
    "LQH2MCN2R2M02L": (C + "056aab7cc07a0bf3eb1908088568e8b8/pn/mur~lqh2mcn2r2m02l.pdf", "Compel, own"),
    "LQH2MCN3R3M02L": (C + "448d55ca620d0a399755b99a800e0a4b/pn/mur~lqh2mcn3r3m02l.pdf", "Compel, own"),
    "LQH2MCN4R7M02L": (C + "3f629eaa6ba5d252a7aa9e54b5468bcf/pn/mur~lqh2mcn4r7m02l.pdf", "Compel, own"),
    "LQH2MCN5R6M02L": (C + "2693735ed549ed0793295b7fceaa70fa/pn/mur~lqh2mcn5r6m02l.pdf", "Compel, own"),
    "LQH2MCN6R8M02L": (C + "8acafb91159e803314e09ade46ec45bb/pn/mur~lqh2mcn6r8m02l.pdf", "Compel, own"),
    "LQH2MCN8R2M02L": (C + "d9255d3c338d134c656fc112777a1d4b/pn/mur~lqh2mcn8r2m02l.pdf", "Compel, own"),
    "LQH2MCN100K02L": (C + "000275b00a576b97d77b9d2a4746166f/ps/mur~lqh2mc_02.pdf", "Compel, series"),
    "LQH2MCN120K02L": (C + "ef3235559e51836ba55e294b625b9b93/pn/mur~lqh2mcn120k02l.pdf", "Compel, own"),
    "LQH2MCN150K02L": (C + "325e5885548d251809fc917009a78dbd/pn/mur~lqh2mcn150k02l.pdf", "Compel, own"),
    "LQH2MCN180K02L": (C + "1a1eb1a84759f873fa447fdb5c228a9f/pn/mur~lqh2mcn180k02l.pdf", "Compel, own"),
    "LQH2MCN220K02L": (C + "79c57990d744f61807bd6c8cbbf9e11b/pn/mur~lqh2mcn220k02l.pdf", "Compel, own"),
    "LQH2MCN270K02L": (C + "a96d2a7b129922a8c45dde63ce96f926/pn/mur~lqh2mcn270k02l.pdf", "Compel, own"),
    "LQH2MCN330K02L": (C + "82126785f604b347708579f53e6b5f78/pn/mur~lqh2mcn330k02l.pdf", "Compel, own"),
    "LQH2MCN390K02L": (C + "70b040bdf7630ac4d90ebedf12fe70c0/pn/mur~lqh2mcn390k02l.pdf", "Compel, own"),
    "LQH2MCN470K02L": (C + "08c145cc8cd68baa51f091a575102a9e/pn/mur~lqh2mcn470k02l.pdf", "Compel, own"),
    "LQH2MCN560K02L": (C + "68f46ec1ec9663afcaef1920ff84df06/pn/mur~lqh2mcn560k02l.pdf", "Compel, own"),
    "LQH2MCN680K02L": (C + "92f6f85c0e529bd79813d41fdb6f3262/pn/mur~lqh2mcn680k02l.pdf", "Compel, own"),
    "LQH2MCN820K02L": (C + "ca21c42f60a8e44f0290fb60d57774c1/pn/mur~lqh2mcn820k02l.pdf", "Compel, own"),
    "LSQNA201212T1R0M": (CB_100K, "Compel, series"),
    "LSQNA201212T2R2M": (CB_100M, "Compel, series"),
    "LSQNA201212T3R3M": (CB_100K, "Compel, series"),
    "LSQNA201212T4R7M": (CB_100M, "Compel, series"),
    "LSQNA201212T6R8M": (CB_100K, "Compel, series"),
    "LSQNA201212T100K": (CB_100K, "Compel, own"),
    "LSQNA201212T100M": (CB_100M, "Compel, own"),
    "LSQNA201212T150K": ("https://cdn.promelec.ru/upload/grab/es.mouser.com/datasheet/2/396/"
                         "wound02_e-13113-1206718.pdf", "Promelec, series"),
    "LSQNA201212T150M": ("https://datasheet.lcsc.com/datasheet/pdf/be2a75085fc4aee71cc9063d0eb9f3de.pdf"
                         "?productCode=C20241874", "LCSC, own"),
    "LSQNA201212T220M": (C + "037aa5970366b0ff89ccdadd3f1d673d/pn/taiyo~cb2012t220m.pdf", "Compel, own"),
    # CBMF1608T (now LSQNB160808T): Compel has a PDF for 100K only, Promelec none; the LCSC
    # file is the 14-page CB-series catalog, listing 100/220/470 as "CBMF1608T100[]" (K or M).
    "LSQNB160808T100K": (C + "f98ba1f0fed8598c3efb35e98d09a0f4/pn/taiyo~cbmf1608t100k.pdf", "Compel, own"),
    **{f"LSQNB160808T{code}": (CB_CATALOG_LCSC, "LCSC, series")
       for code in ("1R0M", "2R2M", "3R3M", "4R7M", "100M", "220K", "220M", "470K", "470M")},
}


def log(msg: str = "") -> None:
    sys.stdout.buffer.write((msg + "\n").encode("utf-8"))


def main() -> int:
    do_apply = "--apply" in sys.argv[1:]
    paths = [a for a in sys.argv[1:] if not a.startswith("--")]
    db = Path(paths[0]).resolve() if paths else ROOT / "GH_DB_LIB.sqlite"

    cn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = dict(cn.execute(f'SELECT "PartNumber", "ComponentLink1URL" FROM {q(TABLE)}').fetchall())
    cn.close()
    changes = {pn: url for pn, (url, _src) in LINKS.items() if pn in rows and rows[pn] != url}
    missing = sorted(set(rows) - set(LINKS))

    counts: dict[str, int] = {}
    for _url, src in LINKS.values():
        counts[src] = counts.get(src, 0) + 1
    log(f"база: {db}")
    log(f"ссылок: {len(LINKS)}; изменится {len(changes)}")
    for src, n in sorted(counts.items()):
        log(f"  {src:<18} {n}")
    if missing:
        log("без ссылки: " + ", ".join(missing))
    if not changes:
        return 0
    if not do_apply:
        log("\nэто предварительный просмотр; записать: python app/ind_datasheets.py --apply")
        return 0

    snapshot = db.parent / "backups" / f"{db.stem}_before-ind-ds_{datetime.now():%Y%m%d_%H%M%S}{db.suffix}"
    snapshot.parent.mkdir(exist_ok=True)
    backends.copy_database(db, snapshot)
    cn = sqlite3.connect(str(db), timeout=15, isolation_level=None)
    try:
        cn.execute("BEGIN IMMEDIATE")
        for pn, url in changes.items():
            cn.execute(f'UPDATE {q(TABLE)} SET "ComponentLink1URL" = ? WHERE "PartNumber" = ?', (url, pn))
        cn.execute("COMMIT")
    except Exception:
        cn.execute("ROLLBACK")
        raise
    finally:
        cn.close()
    log(f"\nзаписано: {len(changes)}; копия до изменений: {snapshot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
