"""Read footprint names straight out of Altium .PcbLib files.

A .PcbLib is an OLE compound file with one storage per footprint. The storage name is not
usable as-is — Windows forbids ':' in storage names, so Altium stores "CAP: 0402" as
"CAP_ 0402". The real name sits at the start of that storage's Data stream, as a Pascal
string, so that is what we read.
"""
from __future__ import annotations

import struct
import threading
from pathlib import Path

import db

_lock = threading.Lock()
# path -> (mtime, names); re-read whenever the .PcbLib changes on disk.
_cache: dict[Path, tuple[float, list[str]]] = {}
_path_cache: dict[str, Path | None] = {}

SKIP_STORAGES = {"FileHeader", "FileVersionInfo", "Library", "SectionKeys"}


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace")


def read_pcblib(path: Path) -> list[str]:
    """Footprint names in one .PcbLib, in file order. Empty list if it cannot be read."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    with _lock:
        hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]

    try:
        import olefile
    except ImportError:
        return []

    names: list[str] = []
    try:
        ole = olefile.OleFileIO(str(path))
    except Exception:
        return []
    try:
        storages = [e[0] for e in ole.listdir(streams=False, storages=True) if len(e) == 1]
        for storage in storages:
            if storage in SKIP_STORAGES:
                continue
            try:
                head = ole.openstream([storage, "Data"]).read(600)
            except Exception:
                continue
            # uint32 block length, then a 1-byte-prefixed string holding the pattern name.
            if len(head) < 6:
                continue
            block = struct.unpack("<I", head[:4])[0]
            n = head[4]
            if not 0 < n <= min(block, len(head) - 5):
                continue
            name = _decode(head[5:5 + n]).strip()
            if name:
                names.append(name)
    finally:
        try:
            ole.close()
        except Exception:
            pass

    names = sorted(dict.fromkeys(names), key=str.casefold)
    with _lock:
        _cache[path] = (mtime, names)
    return names


def find_library(filename: str) -> Path | None:
    """Resolve a library file name the way Altium's DbLib does — search below the database's folder."""
    filename = (filename or "").strip().strip("\\/")
    if not filename:
        return None
    with _lock:
        if filename in _path_cache:
            return _path_cache[filename]
    root = db.library_root()
    candidate = root / filename
    found: Path | None = candidate if candidate.is_file() else None
    if found is None:
        found = next(iter(sorted(root.rglob(Path(filename).name))), None)
    with _lock:
        _path_cache[filename] = found
    return found


def clear_cache() -> None:
    with _lock:
        _cache.clear()
        _path_cache.clear()


# Footprint Ref N is chosen from the library named in the matching Footprint Path N.
REF_TO_PATH = {
    "Footprint Ref": "Footprint Path",
    "Footprint Ref 2": "Footprint Path 2",
    "Footprint Ref 3": "Footprint Path 3",
}


def footprint_options(table: str) -> dict[str, list[tuple[str, bool]]]:
    """{column: [(footprint name, exists in the .PcbLib)]} for each Footprint Ref column.

    Names come from the .PcbLib files the table points at, merged with whatever is already
    stored in that column — values already in the database are offered even when the library
    has no such footprint, flagged False so the form can mark them.
    """
    t = db.get_table(table)
    out: dict[str, list[tuple[str, bool]]] = {}
    for ref_col, path_col in REF_TO_PATH.items():
        if t.col(ref_col) is None:
            continue
        in_lib: list[str] = []
        if t.col(path_col) is not None:
            for lib_name in db.distinct_values(table, path_col, limit=20):
                if not lib_name.lower().endswith(".pcblib"):
                    continue
                lib = find_library(lib_name)
                if lib:
                    in_lib.extend(read_pcblib(lib))
        known = set(in_lib)
        used = db.distinct_values(table, ref_col, limit=db.DROPDOWN_MAX_DISTINCT)
        merged = sorted({n for n in in_lib + used if n}, key=str.casefold)
        if merged:
            # With no readable .PcbLib there is nothing to compare against, so flagging every
            # value as missing would be a lie — treat them all as fine instead.
            out[ref_col] = [(n, not known or n in known) for n in merged]
    return out
