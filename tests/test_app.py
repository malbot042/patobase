import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("pb")
    env = {**os.environ, "PATOBASE_DATA_DIR": str(tmp / "data")}
    subprocess.run([sys.executable, str(ROOT / "scripts" / "seed.py")], check=True, env=env, capture_output=True)
    os.environ["PATOBASE_DATA_DIR"] = str(tmp / "data")
    os.environ["PATOBASE_META_DB"] = str(tmp / "meta.sqlite")
    # La config se lee al importar: recargar con el entorno de test
    for mod in [m for m in sys.modules if m.startswith("patobase")]:
        del sys.modules[mod]
    from patobase.main import app

    with TestClient(app) as c:
        yield c


def test_editor_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "sql-editor" in r.text


def test_schema_lists_sources(client):
    r = client.get("/schema")
    assert "ventas" in r.text and "clientes" in r.text and "tienda.main" in r.text
    comp = client.get("/api/completions").json()
    assert "importe" in comp["files.ventas"]


def test_run_query_renders_table_and_chart(client):
    sql = "SELECT categoria, sum(importe) AS total FROM files.ventas GROUP BY 1 ORDER BY 2 DESC"
    r = client.post("/query/run", data={"sql": sql})
    assert r.status_code == 200
    assert "vizPanel" in r.text and "Electrónica" in r.text
    assert "&#34;type&#34;: &#34;bar&#34;" in r.text  # sugerencia automática


def test_pagination(client):
    r = client.post("/query/page", data={"sql": "SELECT * FROM files.ventas ORDER BY venta_id", "offset": 100})
    assert "Filas 101–200" in r.text


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE files.ventas",
        "COPY (SELECT 1) TO 'x.csv'",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SET enable_external_access = true",
        "SELECT 1; SELECT 2",
    ],
)
def test_sandbox_blocks_dangerous_sql(client, sql):
    r = client.post("/query/run", data={"sql": sql})
    assert "error-box" in r.text


def test_save_question_and_dashboard(client):
    r = client.post(
        "/questions",
        data={
            "name": "Ventas por canal",
            "sql": "SELECT canal, count(*) n FROM files.ventas GROUP BY 1",
            "viz": '{"type": "arc", "x": "canal", "y": "n"}',
        },
    )
    assert "Guardada" in r.text
    qid = int(r.headers["HX-Replace-Url"].split("=")[1])

    r = client.post("/dashboards", data={"name": "Resumen"}, follow_redirects=False)
    did = int(r.headers["location"].rsplit("/", 1)[1])
    client.post(f"/dashboards/{did}/cards", data={"question_id": qid, "width": 2})

    page = client.get(f"/dashboards/{did}").text
    assert "Ventas por canal" in page and f"/questions/{qid}/card" in page
    card = client.get(f"/questions/{qid}/card").text
    assert "vizChart" in card and "Online" in card


def test_new_question_with_empty_id(client):
    r = client.post("/questions", data={"id": "", "name": "Nueva", "sql": "SELECT 1 AS a", "viz": "{}"})
    assert r.status_code == 200 and "Guardada" in r.text


def test_cli_port_and_host(monkeypatch):
    import uvicorn

    from patobase.main import run

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    monkeypatch.setenv("PATOBASE_PORT", "8500")
    run([])
    run(["--port", "9000", "--host", "0.0.0.0"])
    assert calls[0]["port"] == 8500  # variable de entorno
    assert calls[1] == {"host": "0.0.0.0", "port": 9000, "reload": False}  # argumento gana
    with pytest.raises(SystemExit):
        run(["--port", "70000"])


def test_types_with_time_zone_and_complex_values(client):
    """Regresión: timestamptz necesita pytz; el resto de tipos no debe romper la tabla ni el gráfico."""
    sql = (
        "SELECT now() AS ahora, TIMESTAMPTZ '2026-10-29 10:00:00+01' AS tstz, TIMETZ '10:00:00+01' AS ttz, "
        "INTERVAL 3 DAY AS intervalo, uuid() AS id, [1, 2] AS lista, {'a': 1} AS registro, MAP {'k': 1} AS mapa, "
        "'\\xAA'::BLOB AS binario, 2::HUGEINT AS enorme, 'a'::ENUM('a', 'b') AS enumerado"
    )
    r = client.post("/query/run", data={"sql": sql})
    assert r.status_code == 200
    assert "error-box" not in r.text, r.text[:500]
    assert "+01:00" in r.text or "+00:00" in r.text
