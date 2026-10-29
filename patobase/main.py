"""patobase: consultas SQL y dashboards al estilo Metabase con DuckDB + htmx + Alpine."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from .engine import Engine, QueryError, suggest_chart, to_records, vega_type
from .store import Store

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.filters["vega_type"] = vega_type
templates.env.filters["tojson_attr"] = lambda v: json.dumps(v, ensure_ascii=False, default=str)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.engine = Engine(settings)
    app.state.store = Store(settings.meta_db)
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
def editor(request: Request, q: int | None = None):
    question = store(request).get_question(q) if q else None
    if q and not question:
        raise HTTPException(404, "Pregunta no encontrada")
    default_sql = "SELECT *\nFROM files.ventas\nLIMIT 100"
    return render(
        request,
        "editor.html",
        question=question,
        sql=question.sql if question else default_sql,
        dashboards=store(request).list_dashboards(),
    )


@app.get("/schema", response_class=HTMLResponse)
def schema(request: Request):
    return render(request, "partials/schema_tree.html", schema=engine(request).schema())


@app.get("/api/completions")
def completions(request: Request):
    return JSONResponse(engine(request).completions())


@app.post("/query/run", response_class=HTMLResponse)
def run_query(request: Request, sql: str = Form(...), viz: str = Form("")):
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
        col_types={c: vega_type(t) for c, t in zip(res.columns, res.types)},
        truncated=res.has_more,
        chart_rows=settings.chart_rows,
    )


@app.post("/query/page", response_class=HTMLResponse)
def page_query(request: Request, sql: str = Form(...), offset: int = Form(0)):
    try:
        res = engine(request).run(sql, offset=max(offset, 0), limit=settings.page_size)
    except QueryError as e:
        return render(request, "partials/error.html", error=str(e))
    return render(
        request, "partials/table.html", res=res, page=res.rows, sql=sql,
        page_size=settings.page_size, has_more=res.has_more,
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
    sql: str = Form(...),
    viz: str = Form("{}"),
    description: str = Form(""),
    id: int | None = Form(None),
):
    name = name.strip()
    if not name:
        return render(request, "partials/toast.html", kind="error", message="Ponle un nombre a la pregunta.")
    try:
        Engine.validate(sql)
        viz_obj = json.loads(viz or "{}")
    except (QueryError, json.JSONDecodeError) as e:
        return render(request, "partials/toast.html", kind="error", message=f"No se ha guardado: {e}")
    qid = store(request).save_question(name, sql, viz_obj, description, id)
    resp = render(request, "partials/saved.html", qid=qid, name=name, dashboards=store(request).list_dashboards())
    resp.headers["HX-Replace-Url"] = f"/?q={qid}"
    return resp


@app.post("/questions/{qid}/delete")
def delete_question(request: Request, qid: int):
    store(request).delete_question(qid)
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
    config = q.viz if q.viz.get("x") in res.columns or q.viz.get("type") == "table" else suggest_chart(res.columns, res.types)
    return render(
        request,
        "partials/card_body.html",
        q=q,
        res=res,
        page=res.rows[:50],
        records=to_records(res),
        config=config,
        col_types={c: vega_type(t) for c, t in zip(res.columns, res.types)},
    )


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
        request, "dashboard.html", d=d, cards=store(request).cards(did),
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


def run() -> None:  # punto de entrada `patobase`
    import os

    import uvicorn

    uvicorn.run(
        "patobase.main:app",
        host=os.getenv("PATOBASE_HOST", "127.0.0.1"),
        port=int(os.getenv("PATOBASE_PORT", "8000")),
        reload=os.getenv("PATOBASE_RELOAD") == "1",
    )
