"""Editor visual (compilador spec → SQL) y preguntas guardadas como fuentes de datos."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from patobase.builder import BuildError, catalog_from_schema, compile_spec
from patobase.engine import find_cycle, question_deps

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("bdata") / "data"
    env = {**os.environ, "PATOBASE_DATA_DIR": str(d)}
    subprocess.run([sys.executable, str(ROOT / "scripts" / "seed.py")], check=True, env=env, capture_output=True)
    return d


@pytest.fixture()
def client(data_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("PATOBASE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PATOBASE_META_DB", str(tmp_path / "meta.sqlite"))
    for mod in [m for m in sys.modules if m.startswith("patobase")]:
        del sys.modules[mod]
    from patobase.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def catalog(client):
    from patobase.builder import catalog_from_schema as cfs

    return cfs(client.app.state.engine.schema())


# ------------------------------------------------------------------ compilador

JOINED = {
    "source": "files.ventas",
    "joins": [{"table": "files.clientes", "how": "left", "left": "t0.cliente_id", "right": "cliente_id"}],
    "filters": [
        {"col": "t1.ciudad", "op": "in", "value": "Girona, Barcelona"},
        {"col": "t0.fecha", "op": "between", "value": "2025-03-01", "value2": "2025-12-31"},
    ],
    "metrics": [{"agg": "sum", "col": "t0.importe"}, {"agg": "count"}],
    "groups": [{"col": "t0.fecha", "bucket": "month"}, {"col": "t1.ciudad"}],
    "sort": [{"by": "suma_importe", "dir": "desc"}],
    "limit": 5,
}


def test_compile_join_filter_group(client, catalog):
    from patobase.builder import compile_spec as cs

    c = cs(JOINED, catalog)
    assert [x["name"] for x in c.columns] == ["fecha_mes", "ciudad", "suma_importe", "recuento"]
    assert 'LEFT JOIN "files"."clientes" AS t1 ON t0."cliente_id" = t1."cliente_id"' in c.sql
    assert "date_trunc('month'" in c.sql and "LIMIT 5" in c.sql
    res = client.app.state.engine.run(c.sql)
    assert len(res.rows) == 5 and {r[1] for r in res.rows} <= {"Girona", "Barcelona"}


def test_raw_columns_and_name_clash(client, catalog):
    from patobase.builder import compile_spec as cs

    spec = {
        "source": "files.ventas",
        "joins": [{"table": "files.clientes", "how": "inner", "left": "t0.cliente_id", "right": "cliente_id"}],
        "columns": ["t0.cliente_id", "t1.cliente_id"],
        "limit": 1,
    }
    assert [x["name"] for x in cs(spec, catalog).columns] == ["cliente_id", "clientes_cliente_id"]


@pytest.mark.parametrize(
    "spec, msg",
    [
        ({"source": "files.ventas; DROP TABLE x"}, "no existe"),
        (
            {"source": "files.ventas", "filters": [{"col": "t0.importe) OR 1=1 --", "op": "eq", "value": "1"}]},
            "no existe",
        ),
        (
            {"source": "files.ventas", "filters": [{"col": "t0.importe", "op": "gt", "value": "1 OR 1=1"}]},
            "no es un número",
        ),
        (
            {"source": "files.ventas", "filters": [{"col": "t0.importe", "op": "contains", "value": "x"}]},
            "no se puede usar",
        ),
        ({"source": "files.ventas", "metrics": [{"agg": "sum", "col": "t0.canal"}]}, "numérica"),
        ({"source": "files.ventas", "sort": [{"by": 'x" DESC; --'}]}, "no está en el resultado"),
        ({"source": "files.ventas", "limit": "10; DROP"}, "entero"),
        ({"source": "files.ventas", "groups": [{"col": "t0.canal", "bucket": "month"}]}, "no es una fecha"),
        ({"source": "files.ventas", "metrics": [{"agg": "exec"}]}, "Métrica no válida"),
    ],
)
def test_compile_rejects_bad_specs(catalog, spec, msg):
    from patobase.builder import BuildError as BE
    from patobase.builder import compile_spec as cs

    with pytest.raises(BE, match=msg):
        cs(spec, catalog)


def test_text_values_are_escaped(client, catalog):
    from patobase.builder import compile_spec as cs

    spec = {"source": "files.clientes", "filters": [{"col": "t0.nombre", "op": "contains", "value": "O'Brien'); --"}]}
    sql = cs(spec, catalog).sql
    assert "'o''brien''); --'" in sql.lower()
    assert client.app.state.engine.run(sql).rows == []


# --------------------------------------------------------- dependencias/ciclos


@pytest.mark.parametrize(
    "sql, deps",
    [
        ("select * from preguntas.a", {"a"}),
        ('FROM "preguntas"."b" AS t0', {"b"}),  # SQL generado por el editor visual
        ("from memory.preguntas.c", {"c"}),
        ('from PREGUNTAS . "d"', {"d"}),
        ("from mis_preguntas.e", set()),
        ("select preguntas_x from t", set()),
    ],
)
def test_question_deps(sql, deps):
    assert question_deps(sql) == deps


def test_find_cycle():
    assert find_cycle({"a": {"b"}, "b": {"c"}, "c": {"a"}}, "a") == ["a", "b", "c", "a"]
    assert find_cycle({"a": {"b"}, "b": set()}, "a") is None
    assert find_cycle({"a": {"a"}}, "a") == ["a", "a"]


# ------------------------------------------------------- flujo con la interfaz


def _save(client, **data):
    r = client.post("/questions", data=data)
    assert r.status_code == 200
    return r


def test_builder_page_and_run(client):
    assert client.get("/builder").status_code == 200
    r = client.post("/builder/compile", data={"spec": json.dumps(JOINED)})
    assert r.json()["columns"][0]["name"] == "fecha_mes"
    r = client.post("/builder/run", data={"spec": json.dumps(JOINED)})
    assert "vizPanel" in r.text and "error-box" not in r.text
    r = client.post("/builder/compile", data={"spec": json.dumps({"source": "nope"})})
    assert "no existe" in r.json()["error"]


def test_builder_question_saved_server_side_sql(client):
    # El SQL enviado por el navegador se ignora: se genera a partir de la especificación
    r = _save(client, name="Visual", kind="builder", spec=json.dumps(JOINED), sql="SELECT 'trampa'")
    assert "preguntas.visual" in r.text
    q = client.app.state.store.get_question(1)
    assert q.kind == "builder" and "trampa" not in q.sql and q.spec["source"] == "files.ventas"
    assert client.get("/builder?q=1").status_code == 200
    # /?q= de una pregunta visual lleva al editor visual
    assert client.get("/?q=1", follow_redirects=False).headers["location"] == "/builder?q=1"


def test_questions_as_sources_and_dependencies(client):
    _save(client, name="Ventas por canal", sql="SELECT canal, sum(importe) AS total FROM files.ventas GROUP BY 1")
    # Visible en el esquema y consultable desde SQL
    assert "preguntas" in client.get("/schema").text
    r = client.post("/query/run", data={"sql": "SELECT count(*) FROM preguntas.ventas_por_canal"})
    assert "error-box" not in r.text

    # Una pregunta visual que parte de la anterior
    spec = {"source": "preguntas.ventas_por_canal", "metrics": [{"agg": "max", "col": "t0.total"}]}
    _save(client, name="Máximo canal", kind="builder", spec=json.dumps(spec))
    r = client.post("/query/run", data={"sql": "SELECT * FROM preguntas.maximo_canal"})
    assert "error-box" not in r.text

    # No se puede borrar la primera mientras la segunda la usa
    r = client.post("/questions/1/delete", headers={"HX-Request": "true"})
    assert "No se puede borrar" in r.text and "Máximo canal" in r.text
    assert r.headers["HX-Retarget"] == "#toast"
    assert client.app.state.store.get_question(1) is not None

    # Ciclo: la primera no puede pasar a depender de la segunda
    r = _save(client, id="1", name="Ventas por canal", sql="SELECT * FROM preguntas.maximo_canal")
    assert "ciclo" in r.text
    assert "files.ventas" in client.app.state.store.get_question(1).sql

    # Una pregunta que se usa a sí misma
    r = _save(client, name="Yo misma", sql="SELECT * FROM preguntas.yo_misma")
    assert "ciclo" in r.text

    # Borrando primero la dependiente, ya se puede borrar la original
    client.post("/questions/2/delete", headers={"HX-Request": "true"})
    r = client.post("/questions/1/delete", headers={"HX-Request": "true"})
    assert r.text == ""
    r = client.post("/query/run", data={"sql": "SELECT * FROM preguntas.ventas_por_canal"})
    assert "error-box" in r.text


def test_alias_is_stable_on_rename(client):
    _save(client, name="Original", sql="SELECT 1 AS x")
    _save(client, id="1", name="Nuevo nombre", sql="SELECT 2 AS x")
    q = client.app.state.store.get_question(1)
    assert q.slug == "original" and q.name == "Nuevo nombre"
    r = client.post("/query/run", data={"sql": "SELECT x FROM preguntas.original"})
    assert ">2<" in r.text.replace(" ", "")


def test_views_survive_engine_rebuild(client):
    _save(client, name="Persistente", sql="SELECT 42 AS respuesta")
    client.post("/sources/reload")  # reconstruye el motor
    r = client.post("/query/run", data={"sql": "SELECT respuesta FROM preguntas.persistente"})
    assert "42" in r.text and "error-box" not in r.text


def test_migration_adds_columns_and_slugs(tmp_path):
    db = tmp_path / "old.sqlite"
    c = sqlite3.connect(db)
    c.executescript(
        "CREATE TABLE questions (id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', "
        "sql TEXT NOT NULL, viz TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);"
        "INSERT INTO questions (name, sql) VALUES ('Ventas', 'select 1'), ('ventas', 'select 2');"
    )
    c.commit()
    c.close()
    from patobase.store import Store

    qs = sorted(Store(db).list_questions(), key=lambda q: q.id)
    assert [(q.slug, q.kind) for q in qs] == [("ventas", "sql"), ("ventas_2", "sql")]


def test_compile_spec_module_level_imports():
    # Las funciones importadas al principio del módulo siguen funcionando
    assert callable(compile_spec) and callable(catalog_from_schema) and issubclass(BuildError, Exception)
