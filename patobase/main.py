"""patobase: consultas SQL y dashboards al estilo Metabase con DuckDB + htmx + Alpine."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import asynccontextmanager
from pathlib import Path

import duckdb
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .builder import BuildError, catalog_from_schema, compile_spec, ui_catalog
from .config import settings
from .engine import Engine, QueryError, apply_source, find_cycle, question_deps, suggest_chart, to_records, vega_type
from .sources import ATTACHED, KINDS, Source, SourceError, from_form, q
from .store import Store

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.filters["vega_type"] = vega_type


def fmt_num(v):
    """Números al estilo español: 1 234 567,89 (espacio fino como separador de miles)."""
    from decimal import Decimal

    if isinstance(v, bool) or not isinstance(v, (int, float, Decimal)):
        return v
    s = f"{v:,.2f}" if isinstance(v, (float, Decimal)) else f"{v:,}"
    return s.replace(",", "\u2009").replace(".", ",")


templates.env.filters["num"] = fmt_num
templates.env.filters["tojson_attr"] = lambda v: json.dumps(v, ensure_ascii=False, default=str)


_rebuild_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="patobase-src")
TEST_TIMEOUT_S = 20


def rebuild_engine(app: FastAPI) -> Engine:
    """Crea un motor nuevo con las fuentes actuales y lo sustituye en caliente.

    Las consultas en curso siguen con el motor anterior hasta que terminan.
    """
    with _rebuild_lock:
        new = Engine(settings, app.state.store.list_sources(), app.state.store.list_questions())
        app.state.engine = new
        return new


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.store = Store(settings.meta_db)
    rebuild_engine(app)
    yield
    app.state.engine.con.close()


app = FastAPI(title="patobase", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


def engine(request: Request) -> Engine:
    return request.app.state.engine


def store(request: Request) -> Store:
    return request.app.state.store


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def render(request: Request, template: str, status_code: int = 200, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


# ---------------------------------------------------------------------------
# Editor
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def editor(request: Request, q: int | None = None, sql: str = ""):
    question = store(request).get_question(q) if q else None
    if q and not question:
        raise HTTPException(404, "Pregunta no encontrada")
    if question and question.kind == "builder" and not sql:
        return RedirectResponse(question.edit_url, 303)
    default_sql = "SELECT *\nFROM files.ventas\nLIMIT 100"
    return render(
        request,
        "editor.html",
        question=question,
        sql=sql or (question.sql if question else default_sql),
        dashboards=store(request).list_dashboards(),
    )


@app.get("/schema", response_class=HTMLResponse)
def schema(request: Request):
    eng = engine(request)
    failing = [name for name, st in eng.status.items() if not st.ok]
    return render(request, "partials/schema_tree.html", schema=eng.schema(), failing=failing)


@app.get("/api/completions")
def completions(request: Request):
    return JSONResponse(engine(request).completions())


@app.post("/query/run", response_class=HTMLResponse)
def run_query(request: Request, sql: str = Form(...), viz: str = Form("")):
    return results_response(request, sql, viz)


def results_response(request: Request, sql: str, viz: str = "", extra: str = "") -> HTMLResponse:
    """Ejecuta `sql` y pinta el panel de resultados (tabla + gráfico)."""
    try:
        res = engine(request).run(sql, limit=settings.chart_rows)
    except QueryError as e:
        return render(request, "partials/error.html", error=str(e))
    try:
        saved = json.loads(viz) if viz else None
    except json.JSONDecodeError:
        saved = None
    # Si la configuración guardada encaja con las columnas actuales, se respeta
    if saved and saved.get("x") in res.columns:
        config = saved
    else:
        config = suggest_chart(res.columns, res.types)
    page = res.rows[: settings.page_size]
    return render(
        request,
        "partials/results.html",
        res=res,
        page=page,
        sql=sql,
        page_size=settings.page_size,
        has_more=len(res.rows) > settings.page_size or res.has_more,
        records=to_records(res),
        config=config,
        col_types={c: vega_type(t) for c, t in zip(res.columns, res.types, strict=False)},
        truncated=res.has_more,
        chart_rows=settings.chart_rows,
        extra=extra,
    )


@app.post("/query/page", response_class=HTMLResponse)
def page_query(request: Request, sql: str = Form(...), offset: int = Form(0)):
    try:
        res = engine(request).run(sql, offset=max(offset, 0), limit=settings.page_size)
    except QueryError as e:
        return render(request, "partials/error.html", error=str(e))
    return render(
        request,
        "partials/table.html",
        res=res,
        page=res.rows,
        sql=sql,
        page_size=settings.page_size,
        has_more=res.has_more,
    )


# ---------------------------------------------------------------------------
# Preguntas guardadas
# ---------------------------------------------------------------------------
@app.get("/questions", response_class=HTMLResponse)
def questions(request: Request):
    return render(request, "questions.html", questions=store(request).list_questions())


@app.post("/questions", response_class=HTMLResponse)
def save_question(
    request: Request,
    name: str = Form(...),
    sql: str = Form(""),
    viz: str = Form("{}"),
    description: str = Form(""),
    id: int | None = Form(None),
    kind: str = Form("sql"),
    spec: str = Form(""),
):
    def fail(msg: str) -> HTMLResponse:
        return render(request, "partials/toast.html", kind="error", message=msg)

    st = store(request)
    name = name.strip()
    if not name:
        return fail("Ponle un nombre a la pregunta.")
    try:
        viz_obj = json.loads(viz or "{}")
        spec_obj = None
        if kind == "builder":
            # El SQL de una pregunta visual se genera siempre aquí, nunca se acepta del navegador
            spec_obj = json.loads(spec or "{}")
            sql = compile_spec(spec_obj, catalog_from_schema(engine(request).schema())).sql
        elif kind != "sql":
            return fail("Tipo de pregunta no válido.")
        sql = Engine.validate(sql)
    except (QueryError, BuildError, json.JSONDecodeError) as e:
        return fail(f"No se ha guardado: {e}")

    # Una pregunta no puede depender (directa o indirectamente) de sí misma
    slug = st.next_slug(name, id)
    graph = {qq.slug: question_deps(qq.sql) for qq in st.list_questions() if qq.id != id}
    graph[slug] = question_deps(sql)
    cycle = find_cycle(graph, slug)
    if cycle:
        return fail("No se ha guardado: crearía un ciclo entre preguntas (" + " → ".join(cycle) + ").")

    qid = st.save_question(name, sql, viz_obj, description, id, kind, spec_obj)
    q = st.get_question(qid)
    view_error = engine(request).upsert_question(q.slug, q.sql, st.list_questions())
    resp = render(
        request,
        "partials/saved.html",
        q=q,
        qid=qid,
        name=name,
        view_error=view_error,
        dashboards=st.list_dashboards(),
    )
    resp.headers["HX-Replace-Url"] = q.edit_url
    return resp


@app.post("/questions/{qid}/delete")
def delete_question(request: Request, qid: int):
    st = store(request)
    q = st.get_question(qid)
    if q:
        users = [o.name for o in st.list_questions() if o.id != qid and q.slug in question_deps(o.sql)]
        if users:
            msg = f"No se puede borrar: la usan como fuente {', '.join('«' + u + '»' for u in users)}."
            resp = render(request, "partials/toast.html", kind="error", message=msg)
            resp.headers["HX-Retarget"] = "#toast"
            resp.headers["HX-Reswap"] = "innerHTML"
            return resp
        st.delete_question(qid)
        engine(request).drop_question(q.slug)
    if is_htmx(request):
        return Response("")
    return RedirectResponse("/questions", 303)


@app.get("/questions/{qid}/card", response_class=HTMLResponse)
def question_card(request: Request, qid: int):
    """Renderiza una pregunta guardada con su visualización (para dashboards)."""
    q = store(request).get_question(qid)
    if not q:
        return render(request, "partials/error.html", error="La pregunta ya no existe.")
    try:
        res = engine(request).run(q.sql, limit=settings.chart_rows)
    except QueryError as e:
        return render(request, "partials/error.html", error=str(e))
    config = (
        q.viz
        if q.viz.get("x") in res.columns or q.viz.get("type") == "table"
        else suggest_chart(res.columns, res.types)
    )
    return render(
        request,
        "partials/card_body.html",
        q=q,
        res=res,
        page=res.rows[:50],
        records=to_records(res),
        config=config,
        col_types={c: vega_type(t) for c, t in zip(res.columns, res.types, strict=False)},
    )


# ---------------------------------------------------------------------------
# Editor visual
# ---------------------------------------------------------------------------
@app.get("/builder", response_class=HTMLResponse)
def builder_page(request: Request, q: int | None = None, source: str = ""):
    question = store(request).get_question(q) if q else None
    if q and not question:
        raise HTTPException(404, "Pregunta no encontrada")
    if question and question.kind != "builder":
        return RedirectResponse(question.edit_url, 303)
    catalog = catalog_from_schema(engine(request).schema())
    spec = question.spec if question else {"source": source if source in catalog else ""}
    return render(
        request,
        "builder.html",
        question=question,
        spec=spec,
        catalog=ui_catalog(catalog),
        dashboards=store(request).list_dashboards(),
    )


async def _spec_from(request: Request) -> tuple[dict, str]:
    form = await request.form()
    try:
        spec = json.loads(str(form.get("spec", "")) or "{}")
    except json.JSONDecodeError as e:
        raise BuildError("Especificación no válida.") from e
    return spec, str(form.get("viz", ""))


@app.post("/builder/compile")
async def builder_compile(request: Request):
    """SQL generado y columnas de salida, para la vista previa y el paso «Ordenar»."""
    try:
        spec, _ = await _spec_from(request)
        c = compile_spec(spec, catalog_from_schema(engine(request).schema()))
    except BuildError as e:
        return JSONResponse({"error": str(e)})
    return JSONResponse({"sql": c.sql, "columns": c.columns})


@app.post("/builder/run", response_class=HTMLResponse)
async def builder_run(request: Request):
    try:
        spec, viz = await _spec_from(request)
        c = compile_spec(spec, catalog_from_schema(engine(request).schema()))
    except BuildError as e:
        return render(request, "partials/error.html", error=str(e))
    return await run_in_threadpool(results_response, request, c.sql, viz)


# ---------------------------------------------------------------------------
# Dashboards
# ---------------------------------------------------------------------------
@app.get("/dashboards", response_class=HTMLResponse)
def dashboards(request: Request):
    return render(request, "dashboards.html", dashboards=store(request).list_dashboards())


@app.post("/dashboards")
def create_dashboard(request: Request, name: str = Form(...), description: str = Form("")):
    did = store(request).create_dashboard(name.strip() or "Sin título", description)
    if is_htmx(request):
        return Response(headers={"HX-Redirect": f"/dashboards/{did}"})
    return RedirectResponse(f"/dashboards/{did}", 303)


@app.get("/dashboards/{did}", response_class=HTMLResponse)
def dashboard(request: Request, did: int):
    d = store(request).get_dashboard(did)
    if not d:
        raise HTTPException(404, "Dashboard no encontrado")
    return render(
        request,
        "dashboard.html",
        d=d,
        cards=store(request).cards(did),
        questions=store(request).list_questions(),
    )


@app.post("/dashboards/{did}/delete")
def delete_dashboard(request: Request, did: int):
    store(request).delete_dashboard(did)
    if is_htmx(request):
        return Response(headers={"HX-Redirect": "/dashboards"})
    return RedirectResponse("/dashboards", 303)


@app.post("/dashboards/{did}/cards", response_class=HTMLResponse)
def add_card(request: Request, did: int, question_id: int = Form(...), width: int = Form(1)):
    st = store(request)
    st.add_card(did, question_id, 2 if width == 2 else 1)
    if is_htmx(request) and request.headers.get("HX-Target") != "dash-grid":
        return Response(headers={"HX-Redirect": f"/dashboards/{did}"})
    return render(request, "partials/dash_grid.html", d=st.get_dashboard(did), cards=st.cards(did))


@app.post("/cards", response_class=HTMLResponse)
def add_card_from_editor(request: Request, dashboard_id: str = Form(""), id: str = Form("")):
    """Añadir la pregunta abierta en el editor a un dashboard."""
    if not dashboard_id.isdigit():
        return Response("")
    st = store(request)
    d = st.get_dashboard(int(dashboard_id))
    if not id.isdigit() or not d:
        return render(request, "partials/toast.html", kind="error", message="Guarda la pregunta antes de añadirla.")
    st.add_card(d.id, int(id))
    return render(request, "partials/toast.html", kind="ok", message=f"Añadida a «{d.name}»")


@app.post("/cards/{card_id}/delete")
def remove_card(request: Request, card_id: int):
    store(request).remove_card(card_id)
    return Response("")


@app.post("/cards/{card_id}/width")
def card_width(request: Request, card_id: int, width: int = Form(...)):
    store(request).update_card(card_id, 2 if width == 2 else 1)
    return Response("")


# ---------------------------------------------------------------------------
# Fuentes de datos
# ---------------------------------------------------------------------------
def taken_names(request: Request, exclude: int | None = None) -> set[str]:
    names = {s.name for s in store(request).list_sources() if s.id != exclude}
    names |= {f.stem.lower() for f in settings.data_dir.glob("*.duckdb")}
    return names


def sources_ctx(request: Request) -> dict:
    eng = engine(request)
    data_files = sorted(f.name for f in settings.data_dir.glob("*") if f.suffix in (".duckdb", ".parquet", ".csv"))
    return {
        "sources": store(request).list_sources(),
        "status": eng.status,
        "kinds": KINDS,
        "data_files": data_files,
        "data_dir": settings.data_dir.resolve(),
    }


@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request):
    return render(request, "sources.html", **sources_ctx(request))


@app.get("/sources/new", response_class=HTMLResponse)
def source_new(request: Request, kind: str = ""):
    return render(request, "source_edit.html", src=None, kind=kind if kind in KINDS else "", kinds=KINDS)


@app.get("/sources/{sid}/edit", response_class=HTMLResponse)
def source_edit(request: Request, sid: int):
    src = store(request).get_source(sid)
    if not src:
        raise HTTPException(404, "Fuente no encontrada")
    return render(
        request, "source_edit.html", src=src, kind=src.kind, kinds=KINDS, status=engine(request).status.get(src.name)
    )


@app.get("/sources/form", response_class=HTMLResponse)
def source_form(request: Request, kind: str):
    if kind not in KINDS:
        return Response("")
    return render(request, "partials/source_form.html", src=None, kind=kind, k=KINDS[kind])


def _parse_source(request: Request, form: dict) -> Source:
    sid = int(form["id"]) if str(form.get("id", "")).isdigit() else None
    previous = store(request).get_source(sid) if sid else None
    return from_form(str(form.get("kind", "")), form, previous, taken_names(request, sid))


def _test_source(src: Source) -> tuple[int, list[str]]:
    con = duckdb.connect(":memory:")
    try:
        if settings.extension_dir:
            con.execute(f"SET extension_directory = {q(settings.extension_dir)}")
        _, n = apply_source(con, src, settings.data_dir)
        if src.kind in ATTACHED:
            names = [
                r[0]
                for r in con.execute(
                    "SELECT schema_name || '.' || table_name FROM duckdb_tables() "
                    "WHERE database_name = ? AND schema_name NOT IN ('information_schema', 'pg_catalog') "
                    "ORDER BY 1 LIMIT 8",
                    [src.name],
                ).fetchall()
            ]
        else:
            names = [r[0] for r in con.execute(f"DESCRIBE remote.{src.name}").fetchall()][:8]
        return n, names
    finally:
        con.close()


@app.post("/sources/test", response_class=HTMLResponse)
async def source_test(request: Request):
    form = dict(await request.form())
    try:
        src = _parse_source(request, form)
        n, names = _pool.submit(_test_source, src).result(timeout=TEST_TIMEOUT_S)
    except FutureTimeout:
        return render(request, "partials/source_result.html", ok=False, message=f"Sin respuesta en {TEST_TIMEOUT_S} s.")
    except SourceError as e:
        return render(request, "partials/source_result.html", ok=False, message=str(e))
    unit = "tabla" if src.kind in ATTACHED else "columna"
    return render(request, "partials/source_result.html", ok=True, n=n, unit=unit, names=names)


@app.post("/sources", response_class=HTMLResponse)
async def source_save(request: Request):
    form = dict(await request.form())
    try:
        src = _parse_source(request, form)
    except SourceError as e:
        return render(request, "partials/source_result.html", ok=False, message=str(e))
    store(request).save_source(src)
    new = await run_in_threadpool(rebuild_engine, request.app)
    st = new.status.get(src.name)
    if st and not st.ok:
        # Se guarda igualmente (puede ser un corte temporal), pero se avisa
        resp = render(request, "partials/source_result.html", ok=False, saved=True, message=st.message)
        return resp
    return Response(headers={"HX-Redirect": "/sources"})


@app.post("/sources/reload", response_class=HTMLResponse)
def sources_reload(request: Request):
    rebuild_engine(request.app)
    return render(request, "partials/sources_table.html", **sources_ctx(request))


@app.post("/sources/{sid}/toggle", response_class=HTMLResponse)
def source_toggle(request: Request, sid: int):
    src = store(request).get_source(sid)
    if src:
        store(request).set_source_enabled(sid, not src.enabled)
        rebuild_engine(request.app)
    return render(request, "partials/sources_table.html", **sources_ctx(request))


@app.post("/sources/{sid}/delete", response_class=HTMLResponse)
def source_delete(request: Request, sid: int):
    store(request).delete_source(sid)
    rebuild_engine(request.app)
    if request.headers.get("HX-Target") == "sources-table":
        return render(request, "partials/sources_table.html", **sources_ctx(request))
    return Response(headers={"HX-Redirect": "/sources"})


def run(argv: list[str] | None = None) -> None:  # punto de entrada `patobase`
    """Arranca el servidor. Prioridad: argumento > variable de entorno > valor por defecto."""
    import argparse
    import os

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="patobase",
        description="Consultas SQL y dashboards al estilo Metabase con DuckDB.",
        epilog="Las variables PATOBASE_HOST, PATOBASE_PORT y PATOBASE_RELOAD=1 dan los valores por defecto.",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("PATOBASE_HOST", "127.0.0.1"),
        help="dirección en la que escuchar (por defecto: %(default)s; 0.0.0.0 para toda la red)",
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=int(os.getenv("PATOBASE_PORT", "8000")),
        help="puerto (por defecto: %(default)s)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        default=os.getenv("PATOBASE_RELOAD") == "1",
        help="recargar al cambiar el código (desarrollo)",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error(f"puerto no válido: {args.port}")

    uvicorn.run("patobase.main:app", host=args.host, port=args.port, reload=args.reload)
