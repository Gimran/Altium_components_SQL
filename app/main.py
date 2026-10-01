"""Local web UI for adding/editing rows in the Altium component database."""
from __future__ import annotations

import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import assign_footprints
import backends
import db
import footprints

APP_DIR = Path(__file__).resolve().parent
PAGE_SIZE = 100


def _warm_caches() -> None:
    """Fill the schema, picklist and footprint caches so the first page is not slow."""
    try:
        for name in db.list_tables():
            table = db.get_table(name)
            db.filterable_columns(table)
            db.picklists(name)
            footprints.footprint_options(name)
    except Exception:
        pass            # a cold cache only costs time, never correctness


@asynccontextmanager
async def lifespan(_app: FastAPI):
    threading.Thread(target=_warm_caches, daemon=True).start()
    yield


app = FastAPI(title="Component DB", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


def ctx(request: Request, **kw) -> dict:
    base = {
        "request": request,
        "tables": db.list_tables(),
        "db_file": str(db.db_path()),
        "active": kw.get("table"),
        "wal": backends.wal_status(db.db_path()),
        "synced": request.query_params.get("synced", ""),
    }
    base.update(kw)
    return base


@app.exception_handler(db.DbError)
async def db_error(request: Request, exc: db.DbError):
    return templates.TemplateResponse(
        request, "error.html", {"request": request, "tables": [], "message": str(exc),
                                "db_file": str(db.db_path()), "active": None},
        status_code=400,
    )


@app.get("/", response_class=HTMLResponse)
def index():
    tables = db.list_tables()
    if not tables:
        return HTMLResponse("<h1>В базе нет таблиц</h1>", status_code=404)
    return RedirectResponse(f"/t/{tables[0]}", status_code=302)


FILTER_PREFIX = "f."


def _filters(request: Request, t: db.Table) -> dict[str, str]:
    """Column filters arrive as ?f.<column name>=<value>."""
    known = {c.name for c in t.columns}
    out = {}
    for key, value in request.query_params.items():
        if key.startswith(FILTER_PREFIX) and value:
            col = key[len(FILTER_PREFIX):]
            if col in known:
                out[col] = value
    return out


@app.get("/t/{table}", response_class=HTMLResponse)
def table_view(request: Request, table: str, q: str = "", page: int = 1,
               sort: str = "", desc: bool = False, msg: str = ""):
    t = db.get_table(table)
    page = max(1, page)
    filters = _filters(request, t)
    rows, total = db.search_rows(table, q, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE,
                                 sort=sort, desc=desc, filters=filters)
    pages = max(1, -(-total // PAGE_SIZE))
    base = {"q": q, "sort": sort, "desc": int(desc)}
    base.update({FILTER_PREFIX + k: v for k, v in filters.items()})

    def page_url(p: int) -> str:
        return f"/t/{table}?" + urlencode({**base, "page": p})

    return templates.TemplateResponse(request, "list.html", ctx(
        request, table=table, t=t, rows=rows, total=total, q=q,
        page=page, pages=pages, page_url=page_url, sort=sort, desc=desc,
        grid=db.grid_columns(t), msg=msg, filters=filters,
        filter_cols=db.filterable_columns(t),
        filter_options=db.filter_options(table, q, filters),
        empty_filter=db.EMPTY_FILTER, filter_prefix=FILTER_PREFIX,
    ))


def form_page(request: Request, table: str, *, values: dict, row_id, clone: str = "",
              warning: str = "", error: str = "", status: int = 200):
    """Render the record form. Footprint choices are read from the .PcbLib files."""
    return templates.TemplateResponse(request, "form.html", ctx(
        request, table=table, t=db.get_table(table), values=values, row_id=row_id,
        clone=clone, picklists=db.picklists(table),
        selects=footprints.footprint_options(table), manual_value=db.MANUAL_ENTRY,
        warning=warning, error=error,
    ), status_code=status)


@app.get("/t/{table}/new", response_class=HTMLResponse)
def new_form(request: Request, table: str, clone: str = ""):
    t = db.get_table(table)
    if clone:
        values = dict(db.get_row(table, clone) or {})
        values.pop(t.id_col, None)
        # Identity fields must not carry over, and calculated ones would show the source's value.
        for key in ("PartNumber", "Supplier Part Number 1", "Supplier Part Number 2"):
            values.pop(key, None)
        for c in t.calculated:
            values.pop(c.name, None)
        source = clone
    else:
        values = db.defaults_for(table)
        source = ""
    return form_page(request, table, values=values, row_id=None, clone=source)


@app.post("/t/{table}/new", response_class=HTMLResponse)
async def new_submit(request: Request, table: str):
    data = dict(await request.form())
    force = data.pop("_force", "") == "1"
    clone = data.pop("_clone", "")
    pn = (data.get("PartNumber") or "").strip()
    warning = ""
    if pn and not force and db.part_number_exists(table, pn):
        warning = f"PartNumber «{pn}» уже есть в таблице. Сохранить дубликат?"
    if not warning:
        try:
            new_id = db.insert_row(table, data)
        except db.DbError as e:
            return form_page(request, table, values=data, row_id=None, clone=clone,
                             error=str(e), status=400)
        return RedirectResponse(
            f"/t/{table}?" + urlencode({"msg": f"Добавлено: {pn or new_id}", "q": pn}),
            status_code=303)
    return form_page(request, table, values=data, row_id=None, clone=clone,
                     warning=warning, status=409)


REPLACE_PREVIEW_MAX = 500


def _replace_args(t: db.Table, params) -> dict:
    """Find/replace fields plus the list's search and filters, from a query or a form."""
    editable = [c.name for c in t.editable]
    column = params.get("column", "")
    return {
        "column": column if column in editable else "",
        "find": params.get("find", ""),
        "repl": params.get("repl", ""),
        "mode": params.get("mode", "part") if params.get("mode") in db.REPLACE_MODES else "part",
        "case": params.get("case", "") == "1",
        "q": params.get("q", ""),
        "filters": {k[len(FILTER_PREFIX):]: v for k, v in params.items()
                    if k.startswith(FILTER_PREFIX) and v and t.col(k[len(FILTER_PREFIX):])},
    }


def replace_page(request: Request, table: str, args: dict, *, changes=None, error="",
                 notice="", done=None, status=200):
    t = db.get_table(table)
    scope = {"q": args["q"], **{FILTER_PREFIX + k: v for k, v in args["filters"].items()}}
    return templates.TemplateResponse(request, "replace.html", ctx(
        request, table=table, t=t, args=args, modes=db.REPLACE_MODES,
        columns=[c for c in t.editable], changes=changes,
        shown=(changes or [])[:REPLACE_PREVIEW_MAX], preview_max=REPLACE_PREVIEW_MAX,
        errors=sum(1 for c in changes or [] if c["error"]),
        token=db.plan_token(changes) if changes else "",
        scope=scope, back_url=f"/t/{table}?" + urlencode({k: v for k, v in scope.items() if v}),
        empty_filter=db.EMPTY_FILTER, filter_prefix=FILTER_PREFIX,
        error=error, notice=notice, done=done,
    ), status_code=status)


@app.get("/t/{table}/replace", response_class=HTMLResponse)
def replace_form(request: Request, table: str):
    t = db.get_table(table)
    args = _replace_args(t, request.query_params)
    changes, error = None, ""
    if args["column"] and request.query_params.get("preview"):
        try:
            changes = db.replace_plan(table, args["column"], args["find"], args["repl"],
                                      args["mode"], args["case"], args["q"], args["filters"])
        except db.DbError as e:
            error = str(e)
    return replace_page(request, table, args, changes=changes, error=error)


@app.post("/t/{table}/replace", response_class=HTMLResponse)
async def replace_submit(request: Request, table: str):
    t = db.get_table(table)
    form = await request.form()
    args = _replace_args(t, form)
    try:
        changes = db.replace_plan(table, args["column"], args["find"], args["repl"],
                                  args["mode"], args["case"], args["q"], args["filters"])
    except db.DbError as e:
        return replace_page(request, table, args, error=str(e), status=400)
    if db.plan_token(changes) != form.get("token", ""):
        return replace_page(request, table, args, changes=changes, status=409,
                            notice="Данные изменились после предпросмотра — проверьте список ещё раз.")
    try:
        snapshot = db.replace_apply(table, args["column"], changes)
    except db.DbError as e:
        return replace_page(request, table, args, changes=changes, error=str(e), status=400)
    return replace_page(request, table, args,
                        done={"count": len(changes), "snapshot": snapshot.name})


@app.get("/t/{table}/{row_id}/edit", response_class=HTMLResponse)
def edit_form(request: Request, table: str, row_id: str):
    values = db.get_row(table, row_id)
    if values is None:
        return HTMLResponse("<h1>Запись не найдена</h1>", status_code=404)
    return form_page(request, table, values=values, row_id=row_id)


@app.post("/t/{table}/{row_id}/edit", response_class=HTMLResponse)
async def edit_submit(request: Request, table: str, row_id: str):
    data = dict(await request.form())
    force = data.pop("_force", "") == "1"
    data.pop("_clone", "")
    pn = (data.get("PartNumber") or "").strip()
    if pn and not force and db.part_number_exists(table, pn, exclude_id=row_id):
        return form_page(
            request, table, values=data, row_id=row_id, status=409,
            warning=f"PartNumber «{pn}» уже занят другой записью. Сохранить всё равно?")
    try:
        db.update_row(table, row_id, data)
    except db.DbError as e:
        return form_page(request, table, values=data, row_id=row_id,
                         error=str(e), status=400)
    return RedirectResponse(
        f"/t/{table}?" + urlencode({"msg": f"Сохранено: {pn or row_id}", "q": pn}),
        status_code=303)


@app.post("/t/{table}/{row_id}/delete")
def delete(table: str, row_id: str):
    db.delete_row(table, row_id)
    return RedirectResponse(f"/t/{table}?" + urlencode({"msg": f"Удалена запись {row_id}"}),
                            status_code=303)


def _footprint_state() -> dict:
    """Everything the footprints page shows: the dry run, and whether it can be applied."""
    path = db.db_path()
    footprints.clear_cache()            # re-read the .PcbLib: it may have changed since
    plans = assign_footprints.plan(path)
    rows, new_cols = assign_footprints.pending(plans)
    blocked = ""
    if backends.locked_by_someone(path):
        blocked = ("База сейчас занята другим процессом (обычно библиотекой, загруженной "
                   "в Altium) — записать не получится. Закройте её в Altium и обновите страницу.")
    return {"blocked": blocked, "plans": plans, "rows": rows, "new_cols": new_cols}


def _footprint_tables(plans: dict) -> list[dict]:
    return [{
        "name": table, "total": len(rows), "changed": len(updates),
        "sizes": assign_footprints.by_size(table, summary),
        "missing": {lib: assign_footprints.missing_sorted(names) for lib, names in missing.items()},
        "skipped": dict(skipped),
    } for table, (_cols, rows, updates, summary, missing, skipped) in plans.items()]


@app.get("/footprints", response_class=HTMLResponse)
def footprints_page(request: Request):
    state = _footprint_state()
    return templates.TemplateResponse(request, "footprints.html", ctx(
        request, table="__footprints__", blocked=state["blocked"], rows=state["rows"],
        new_cols=state["new_cols"], report=_footprint_tables(state["plans"]), result=None,
    ))


@app.post("/footprints/apply", response_class=HTMLResponse)
def footprints_apply(request: Request):
    state = _footprint_state()
    lines: list[str] = []
    ok, status = False, 409
    if state["blocked"]:
        lines.append(state["blocked"])
    elif not state["rows"] and not state["new_cols"]:
        lines.append("Менять нечего.")
        ok, status = True, 200
    else:
        try:
            ok, _snapshot = assign_footprints.apply(db.db_path(), state["plans"],
                                                    out=lambda s: lines.append(s.strip()))
            status = 200 if ok else 500
        except Exception as exc:                # rolled back inside apply()
            lines.append(f"Ошибка, ничего не изменено: {exc}")
            status = 500
        db.refresh_schema()
        footprints.clear_cache()
        if state["new_cols"]:
            lines.append("Добавлены колонки — чтобы Altium их увидел, запустите "
                         "python app/make_dblib.py")
    after = _footprint_state()
    return templates.TemplateResponse(request, "footprints.html", ctx(
        request, table="__footprints__", blocked=after["blocked"], rows=after["rows"],
        new_cols=after["new_cols"], report=_footprint_tables(after["plans"]),
        result={"ok": ok, "lines": [l for l in lines if l], "changed": state["rows"]},
    ), status_code=status)


@app.post("/sync")
def sync(request: Request):
    """Checkpoint the WAL, then back to the page the button was pressed on."""
    result = backends.checkpoint(db.db_path())
    back = urlsplit(request.headers.get("referer", "/"))
    query = [(k, v) for k, v in parse_qsl(back.query) if k != "synced"]
    query.append(("synced", f"{result['moved']}/{result['frames']}"))
    return RedirectResponse(f"{back.path or '/'}?{urlencode(query)}", status_code=303)


@app.post("/refresh")
def refresh():
    db.refresh_schema()
    footprints.clear_cache()
    return RedirectResponse("/", status_code=303)


def main() -> None:
    import uvicorn
    cfg = db.CONFIG
    host, port = cfg.get("host", "127.0.0.1"), int(cfg.get("port", 8777))
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/"
    if cfg.get("open_browser", True):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"База : {db.db_path()}")
    print(f"Адрес: {url}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
