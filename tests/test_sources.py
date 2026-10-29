"""Fuentes de datos: validación, SQL generado y flujo completo de la UI.

Los tests de integración contra Postgres, MySQL, S3 y HTTP reales se activan con
PATOBASE_IT=1 y estas variables (valores por defecto entre paréntesis):
  PATOBASE_IT_PG    host:puerto:base:usuario:contraseña   (127.0.0.1:5433:erp:lectura:secreto)
  PATOBASE_IT_MYSQL host:puerto:base:usuario:contraseña   (127.0.0.1:3307:crm:lectura:secreto)
  PATOBASE_IT_S3    endpoint:bucket                       (127.0.0.1:5050:datos)  — p. ej. MinIO o moto
  PATOBASE_IT_HTTP  URL de un CSV                         (http://127.0.0.1:8899/paises.csv)
"""

import os
import sqlite3
import sys
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from patobase.sources import KINDS, Source, SourceError, from_form, plan, remote_prefix

# ---------------------------------------------------------------- unitarios


def test_remote_prefix():
    assert remote_prefix("s3://b/ventas/*.parquet") == "s3://b/ventas/"
    assert remote_prefix("s3://b/a/**/x.parquet") == "s3://b/a/"
    assert remote_prefix("https://x.org/datos.csv") == "https://x.org/datos.csv"


@pytest.mark.parametrize("name", ["", "1erp", "files", "remote", "con-guion", "a" * 41])
def test_invalid_names(name):
    with pytest.raises(SourceError):
        from_form("sqlite", {"name": name, "path": "x.sqlite"})


def test_name_must_be_unique():
    with pytest.raises(SourceError, match="Ya existe"):
        from_form("sqlite", {"name": "app", "path": "x.sqlite"}, taken={"app"})


def test_required_fields():
    with pytest.raises(SourceError, match="Servidor"):
        from_form("postgres", {"name": "erp", "port": "5432", "database": "d", "user": "u"})


def test_secret_kept_when_blank_on_edit():
    prev = Source(1, "erp", "postgres", {"host": "h", "port": 5432, "database": "d", "user": "u", "password": "s3cr3t"})
    new = from_form(
        "postgres", {"name": "erp", "host": "h2", "port": "5432", "database": "d", "user": "u", "password": ""}, prev
    )
    assert new.config["password"] == "s3cr3t" and new.config["host"] == "h2" and new.id == 1


def test_postgres_plan_uses_secret_and_escapes(tmp_path):
    src = Source(
        None,
        "erp",
        "postgres",
        {
            "host": "db",
            "port": 5432,
            "database": "erp",
            "user": "lectura",
            "password": "it's",
            "schema": "rrhh",
            "sslmode": "require",
        },
    )
    p = plan(src, tmp_path)
    assert "PASSWORD 'it''s'" in p.statements[0]
    assert "SCHEMA 'rrhh'" in p.statements[1] and "READ_ONLY" in p.statements[1]
    assert "sslmode=require" in p.statements[1] and p.tcp_check == ("db", 5432)
    assert "it's" not in p.statements[1]  # la contraseña nunca va en la cadena de conexión


def test_env_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("PB_TEST_PW", "desde-entorno")
    src = Source(
        None, "crm", "mysql", {"host": "h", "port": 3306, "database": "c", "user": "u", "password": "env:PB_TEST_PW"}
    )
    assert "PASSWORD 'desde-entorno'" in plan(src, tmp_path).statements[0]
    src.config["password"] = "env:NO_EXISTE_XYZ"
    with pytest.raises(SourceError, match="NO_EXISTE_XYZ"):
        plan(src, tmp_path)


def test_s3_plan(tmp_path):
    src = Source(
        None,
        "ventas",
        "s3",
        {
            "url": "s3://bucket/ventas/*.parquet",
            "format": "parquet",
            "mode": "live",
            "key_id": "AK",
            "secret": "SK",
            "region": "eu-west-1",
            "endpoint": "",
            "url_style": "vhost",
            "use_ssl": True,
        },
    )
    p = plan(src, tmp_path)
    assert "SCOPE 's3://bucket/ventas/'" in p.statements[0] and "KEY_ID 'AK'" in p.statements[0]
    assert p.statements[-1].startswith("CREATE OR REPLACE VIEW remote.ventas")
    assert p.allowed_prefix == "s3://bucket/ventas/"
    src.config["mode"] = "snapshot"
    assert "TABLE remote.ventas" in plan(src, tmp_path).statements[-1]


def test_file_sources_confined_to_data_dir(tmp_path):
    with pytest.raises(SourceError, match="dentro de la carpeta"):
        plan(Source(None, "x", "sqlite", {"path": "/etc/passwd"}), tmp_path)
    with pytest.raises(SourceError, match="dentro de la carpeta"):
        plan(Source(None, "x", "duckdb", {"path": "../fuera.duckdb"}), tmp_path)


def test_every_kind_has_a_form():
    for k in KINDS.values():
        assert k.fields and k.label


# ------------------------------------------------------------ flujo con la UI


@pytest.fixture()
def client(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    con = duckdb.connect(str(data / "historico.duckdb"))
    con.execute("CREATE TABLE ventas_2020 AS SELECT range AS id FROM range(10)")
    con.close()
    (data / "otra").mkdir()
    duckdb.connect(str(data / "otra" / "archivo.duckdb")).execute("CREATE TABLE t AS SELECT 1 AS x").close()
    monkeypatch.setenv("PATOBASE_DATA_DIR", str(data))
    monkeypatch.setenv("PATOBASE_META_DB", str(tmp_path / "meta.sqlite"))
    for mod in [m for m in sys.modules if m.startswith("patobase") and m != "patobase.sources"]:
        del sys.modules[mod]
    from patobase.main import app

    with TestClient(app) as c:
        yield c


def test_sources_page_empty(client):
    r = client.get("/sources")
    assert r.status_code == 200 and "Aún no hay fuentes externas" in r.text
    assert "historico.duckdb" in r.text  # ficheros de la carpeta local


def test_add_duckdb_source_end_to_end(client):
    form = {"kind": "duckdb", "name": "archivo", "path": "otra/archivo.duckdb"}
    r = client.post("/sources/test", data=form)
    assert "Conexión correcta" in r.text and "main.t" in r.text

    r = client.post("/sources", data=form)
    assert r.headers.get("HX-Redirect") == "/sources"
    assert "Conectada" in client.get("/sources").text
    assert "archivo.main" in client.get("/schema").text

    r = client.post("/query/run", data={"sql": "SELECT x FROM archivo.main.t"})
    assert "error-box" not in r.text

    sid = 1
    client.post(f"/sources/{sid}/toggle")
    assert "archivo.main" not in client.get("/schema").text
    client.post(f"/sources/{sid}/toggle")
    assert "archivo.main" in client.get("/schema").text

    client.post(f"/sources/{sid}/delete", headers={"HX-Target": "sources-table"})
    assert "archivo.main" not in client.get("/schema").text


def test_name_clash_with_data_dir_file(client):
    r = client.post("/sources", data={"kind": "duckdb", "name": "historico", "path": "otra/archivo.duckdb"})
    assert "Ya existe" in r.text


def test_unreachable_source_is_saved_but_flagged(client):
    form = {
        "kind": "postgres",
        "name": "caida",
        "host": "127.0.0.1",
        "port": "1",
        "database": "x",
        "user": "u",
        "password": "p",
        "sslmode": "disable",
    }
    r = client.post("/sources/test", data=form)
    assert "No se puede conectar" in r.text
    r = client.post("/sources", data=form)
    assert "Guardada, pero no se puede conectar" in r.text
    # La app sigue funcionando y avisa en el editor
    assert "fuente con error" in client.get("/schema").text
    assert "error-box" not in client.post("/query/run", data={"sql": "SELECT 1"}).text


def test_secret_never_rendered(client):
    form = {
        "kind": "postgres",
        "name": "pg",
        "host": "127.0.0.1",
        "port": "1",
        "database": "x",
        "user": "u",
        "password": "SuperSecreta123",
        "sslmode": "disable",
    }
    client.post("/sources", data=form)
    page = client.get("/sources/1/edit").text
    assert "SuperSecreta123" not in page and "sin cambios" in page
    assert "SuperSecreta123" not in client.get("/sources").text


# --------------------------------------------------------------- integración

IT = os.getenv("PATOBASE_IT") == "1"


def _parts(var, default):
    return os.getenv(var, default).split(":")


@pytest.mark.skipif(not IT, reason="PATOBASE_IT=1 para probar contra servidores reales")
def test_integration_real_servers(client, tmp_path):
    data = Path(os.environ["PATOBASE_DATA_DIR"])
    s = sqlite3.connect(data / "app.sqlite")
    s.execute("CREATE TABLE usuarios (id INT, email TEXT)")
    s.execute("INSERT INTO usuarios VALUES (1, 'a@b.c')")
    s.commit()
    s.close()

    h, port, db, user, pw = _parts("PATOBASE_IT_PG", "127.0.0.1:5433:erp:lectura:secreto")
    mh, mport, mdb, muser, mpw = _parts("PATOBASE_IT_MYSQL", "127.0.0.1:3307:crm:lectura:secreto")
    s3_host, s3_port, bucket = _parts("PATOBASE_IT_S3", "127.0.0.1:5050:datos")
    http_url = os.getenv("PATOBASE_IT_HTTP", "http://127.0.0.1:8899/paises.csv")

    forms = [
        {
            "kind": "postgres",
            "name": "erp",
            "host": h,
            "port": port,
            "database": db,
            "user": user,
            "password": pw,
            "sslmode": "disable",
        },
        {"kind": "mysql", "name": "crm", "host": mh, "port": mport, "database": mdb, "user": muser, "password": mpw},
        {"kind": "sqlite", "name": "app", "path": "app.sqlite"},
        {
            "kind": "s3",
            "name": "ventas_s3",
            "url": f"s3://{bucket}/ventas/*.parquet",
            "format": "parquet",
            "mode": "live",
            "region": "us-east-1",
            "key_id": "x",
            "secret": "y",
            "endpoint": f"{s3_host}:{s3_port}",
            "url_style": "path",
        },  # use_ssl desmarcado
        {"kind": "url", "name": "paises", "url": http_url, "format": "csv", "mode": "snapshot"},
    ]
    for f in forms:
        r = client.post("/sources/test", data=f)
        assert "Conexión correcta" in r.text, (f["kind"], r.text)
        assert client.post("/sources", data=f).headers.get("HX-Redirect") == "/sources", f["kind"]

    page = client.get("/sources").text
    assert page.count("Conectada") == 5

    for sql in [
        "SELECT count(*) FROM erp.public.facturas",
        "SELECT count(*) FROM crm.oportunidades",
        "SELECT * FROM app.usuarios",
        "SELECT count(*) FROM remote.ventas_s3",
        "SELECT * FROM remote.paises",
    ]:
        assert "error-box" not in client.post("/query/run", data={"sql": sql}).text, sql

    # El sandbox sigue cerrado para todo lo demás
    for sql in [
        f"SELECT * FROM read_parquet('s3://{bucket}/otra/*.parquet')",
        "SELECT * FROM read_csv('http://127.0.0.1:1/x.csv')",
        "SELECT * FROM read_csv('/etc/passwd')",
    ]:
        assert "Permission" in client.post("/query/run", data={"sql": sql}).text, sql


def test_name_is_lowercased():
    assert from_form("sqlite", {"name": " Erp ", "path": "x.sqlite"}).name == "erp"
