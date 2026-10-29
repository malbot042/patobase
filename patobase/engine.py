"""Motor de consultas: una conexión DuckDB en memoria que federa las fuentes.

- Cada fichero ``*.duckdb`` de la carpeta de datos se adjunta en solo lectura.
- Cada ``*.parquet`` / ``*.csv`` se expone como vista en el esquema ``files``.
- Las sentencias de ``PATOBASE_ATTACH`` (Postgres, MySQL, ...) se ejecutan al arrancar.

Tras configurar las fuentes se desactiva el acceso externo y se bloquea la
configuración, así los usuarios no pueden leer ficheros arbitrarios ni
reactivar nada desde el editor SQL.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from .config import Settings
from .sources import ATTACHED, Plan, Source, SourceError, plan, q, tcp_reachable

# Sentencias que un usuario puede ejecutar desde el editor
ALLOWED = {
    duckdb.StatementType.SELECT,
    duckdb.StatementType.EXPLAIN,
    duckdb.StatementType.PRAGMA,  # SHOW TABLES, DESCRIBE...
}

_IDENT = re.compile(r"[^A-Za-z0-9_]")


def _ident(name: str) -> str:
    clean = _IDENT.sub("_", name)
    return f"_{clean}" if clean[:1].isdigit() else clean


def _sql_list(items: list[str]) -> str:
    return "[" + ", ".join(q(i) for i in items) + "]"


class QueryError(Exception):
    pass


@dataclass
class QueryResult:
    columns: list[str]
    types: list[str]
    rows: list[tuple]
    has_more: bool
    offset: int
    elapsed_ms: float


@dataclass
class SourceStatus:
    ok: bool
    message: str = ""
    tables: int = 0


def load_extension(con: duckdb.DuckDBPyConnection, name: str) -> None:
    """LOAD si ya está instalada; si no, INSTALL (necesita Internet la primera vez)."""
    try:
        con.execute(f"LOAD {name}")
    except duckdb.Error:
        try:
            con.execute(f"INSTALL {name}")
            con.execute(f"LOAD {name}")
        except duckdb.Error as e:
            raise SourceError(
                f"No se pudo instalar la extensión «{name}» de DuckDB. "
                "Comprueba que el servidor tiene salida a Internet o define "
                "PATOBASE_EXTENSION_DIR con las extensiones ya descargadas."
            ) from e


def apply_source(con: duckdb.DuckDBPyConnection, src: Source, data_dir: Path) -> tuple[Plan, int]:
    """Ejecuta el plan de una fuente en `con`. Devuelve el plan y el nº de tablas."""
    p = plan(src, data_dir)
    if p.tcp_check:
        tcp_reachable(*p.tcp_check)
    if p.extension:
        load_extension(con, p.extension)
    try:
        for stmt in p.statements:
            con.execute(stmt)
        n = con.execute(p.probe).fetchone()[0] if p.probe else 0
    except duckdb.Error as e:
        raise SourceError(_clean_error(str(e))) from e
    return p, int(n)


def _clean_error(msg: str) -> str:
    # Nunca mostrar credenciales que DuckDB pueda incluir en el mensaje
    msg = re.sub(r"\b(password|passwd|secret)=\S+", r"\1=***", msg, flags=re.I)
    msg = re.sub(r' with parameters ""', "", msg)
    return msg.strip().splitlines()[0][:400]


class Engine:
    def __init__(self, settings: Settings, sources: list[Source] | None = None):
        self.settings = settings
        self.sources = [s for s in (sources or []) if s.enabled]
        self.status: dict[str, SourceStatus] = {}
        self.con = duckdb.connect(":memory:")
        if settings.extension_dir:
            self.con.execute(f"SET extension_directory = {q(settings.extension_dir)}")
        self._setup()

    # ------------------------------------------------------------------ setup
    def _setup(self) -> None:
        s = self.settings
        data_dir = s.data_dir.resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        con = self.con
        allowed_dirs = [f"{data_dir.as_posix()}/"]
        allowed_paths: list[str] = []

        for f in sorted(data_dir.glob("*.duckdb")):
            con.execute(f"ATTACH '{f.as_posix()}' AS {_ident(f.stem)} (READ_ONLY)")

        files = sorted([*data_dir.glob("*.parquet"), *data_dir.glob("*.csv")])
        if files:
            con.execute("CREATE SCHEMA IF NOT EXISTS files")
            for f in files:
                reader = "read_parquet" if f.suffix == ".parquet" else "read_csv_auto"
                con.execute(
                    f"CREATE VIEW files.{_ident(f.stem)} AS "
                    f"SELECT * FROM {reader}('{f.as_posix()}')"
                )

        for stmt in filter(None, (x.strip() for x in s.extra_attach.split(";"))):
            con.execute(stmt)

        # Fuentes configuradas en la UI: cada una por separado, una caída no tumba al resto
        taken = {r[0] for r in con.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
        for src in self.sources:
            if src.name in taken:
                self.status[src.name] = SourceStatus(False, f"El nombre «{src.name}» ya lo usa otra base.")
                continue
            try:
                p, n = apply_source(con, src, data_dir)
            except SourceError as e:
                self.status[src.name] = SourceStatus(False, str(e))
                try:  # deja la conexión limpia si el ATTACH se quedó a medias
                    con.execute(f"DETACH DATABASE IF EXISTS {src.name}")
                except duckdb.Error:
                    pass
                continue
            if p.allowed_prefix:
                (allowed_dirs if p.allowed_prefix.endswith("/") else allowed_paths).append(p.allowed_prefix)
            self.status[src.name] = SourceStatus(True, "", n)
            if src.kind in ATTACHED:
                taken.add(src.name)

        # Sandbox: solo la carpeta de datos y los prefijos remotos configurados
        # son accesibles, y nadie puede cambiarlo desde el editor
        con.execute(f"SET allowed_directories={_sql_list(allowed_dirs)}")
        if allowed_paths:
            con.execute(f"SET allowed_paths={_sql_list(allowed_paths)}")
        con.execute("SET enable_external_access=false")
        con.execute("SET lock_configuration=true")

    # ----------------------------------------------------------------- schema
    def schema(self) -> dict[str, dict[str, list[tuple[str, str]]]]:
        """{base.esquema: {tabla: [(columna, tipo), ...]}}"""
        rows = self.con.cursor().execute(
            """
            SELECT database_name, schema_name, table_name, column_name, data_type
            FROM duckdb_columns()
            WHERE NOT internal AND database_name NOT IN ('system', 'temp')
              AND schema_name NOT IN ('information_schema', 'pg_catalog')
            ORDER BY database_name, schema_name, table_name, column_index
            """
        ).fetchall()
        out: dict[str, dict[str, list[tuple[str, str]]]] = {}
        for db, sch, tbl, col, typ in rows:
            key = sch if db == "memory" else f"{db}.{sch}"
            out.setdefault(key, {}).setdefault(tbl, []).append((col, typ))
        return out

    def completions(self) -> dict[str, list[str]]:
        """Formato que espera el autocompletado SQL de CodeMirror."""
        out: dict[str, list[str]] = {}
        for prefix, tables in self.schema().items():
            for tbl, cols in tables.items():
                names = [c for c, _ in cols]
                out[f"{prefix}.{tbl}"] = names
                out.setdefault(tbl, names)
        return out

    # ------------------------------------------------------------------ query
    @staticmethod
    def validate(sql: str) -> str:
        sql = sql.strip()
        if not sql:
            raise QueryError("La consulta está vacía.")
        try:
            stmts = duckdb.extract_statements(sql)
        except duckdb.Error as e:
            raise QueryError(str(e)) from e
        if len(stmts) != 1:
            raise QueryError("Ejecuta una sola sentencia cada vez.")
        if stmts[0].type not in ALLOWED:
            raise QueryError(
                f"Sentencia no permitida ({stmts[0].type.name}). Solo se admiten consultas de lectura."
            )
        return stmts[0].query.strip().rstrip(";")

    def run(self, sql: str, offset: int = 0, limit: int | None = None) -> QueryResult:
        import time

        sql = self.validate(sql)
        limit = limit or self.settings.page_size
        cur = self.con.cursor()
        timer = threading.Timer(self.settings.timeout_s, cur.interrupt)
        timer.start()
        t0 = time.perf_counter()
        try:
            rel = cur.sql(sql)
            if rel is None:
                raise QueryError("La sentencia no devuelve resultados.")
            rows = rel.limit(limit + 1, offset).fetchall()
            columns, types = rel.columns, [str(t) for t in rel.types]
        except duckdb.InterruptException as e:
            raise QueryError(
                f"La consulta superó el límite de {self.settings.timeout_s:g} s y se canceló."
            ) from e
        except duckdb.Error as e:
            raise QueryError(str(e)) from e
        finally:
            timer.cancel()
            cur.close()
        return QueryResult(
            columns=columns,
            types=types,
            rows=rows[:limit],
            has_more=len(rows) > limit,
            offset=offset,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )


def to_records(res: QueryResult) -> list[dict[str, Any]]:
    """Filas como dicts serializables a JSON (para Vega-Lite)."""
    out = []
    for row in res.rows:
        rec = {}
        for col, val in zip(res.columns, row):
            if val is None or isinstance(val, (bool, int, float, str)):
                rec[col] = val
            elif hasattr(val, "isoformat"):
                rec[col] = val.isoformat()
            else:
                try:
                    rec[col] = float(val)  # Decimal, HUGEINT...
                except (TypeError, ValueError):
                    rec[col] = str(val)
        out.append(rec)
    return out


NUMERIC = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "FLOAT", "DOUBLE", "DECIMAL", "UBIGINT", "UINTEGER", "USMALLINT", "UTINYINT")
TEMPORAL = ("DATE", "TIMESTAMP", "TIME")


def vega_type(duck_type: str) -> str:
    t = duck_type.upper()
    if t.startswith(NUMERIC):
        return "quantitative"
    if t.startswith(TEMPORAL):
        return "temporal"
    return "nominal"


def suggest_chart(columns: list[str], types: list[str]) -> dict[str, str]:
    """Elige un gráfico razonable según los tipos de las columnas."""
    vt = [vega_type(t) for t in types]
    nums = [c for c, t in zip(columns, vt) if t == "quantitative"]
    dims = [c for c, t in zip(columns, vt) if t != "quantitative"]
    temporal = [c for c, t in zip(columns, vt) if t == "temporal"]
    if not nums or len(columns) < 2:
        return {"type": "table", "x": columns[0] if columns else "", "y": "", "color": ""}
    if temporal:
        return {"type": "line", "x": temporal[0], "y": nums[0], "color": dims[1] if len(dims) > 1 else ""}
    if dims:
        return {"type": "bar", "x": dims[0], "y": nums[0], "color": dims[1] if len(dims) > 1 else ""}
    return {"type": "point", "x": nums[0], "y": nums[1] if len(nums) > 1 else nums[0], "color": ""}
