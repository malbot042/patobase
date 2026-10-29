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
from collections.abc import Iterator
from contextlib import contextmanager
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


def _quote(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


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


# preguntas.x · "preguntas"."x" · memory.preguntas.x · PREGUNTAS . "x"
QUESTION_REF = re.compile(r'(?<![\w"])"?preguntas"?\s*\.\s*"?([A-Za-z0-9_]+)"?', re.IGNORECASE)


def question_deps(sql: str) -> set[str]:
    """Alias de otras preguntas que usa una consulta (preguntas.<alias>)."""
    return {m.lower() for m in QUESTION_REF.findall(sql or "")}


def find_cycle(graph: dict[str, set[str]], start: str) -> list[str] | None:
    """Devuelve el camino de un ciclo que pasa por `start`, si lo hay."""
    path: list[str] = []

    def visit(node: str, seen: set[str]) -> list[str] | None:
        path.append(node)
        for dep in graph.get(node, ()):
            if dep == start:
                return [*path, start]
            if dep not in seen:
                seen.add(dep)
                found = visit(dep, seen)
                if found:
                    return found
        path.pop()
        return None

    return visit(start, {start})


class Engine:
    def __init__(self, settings: Settings, sources: list[Source] | None = None, questions: list | None = None):
        self.settings = settings
        self.sources = [s for s in (sources or []) if s.enabled]
        self.status: dict[str, SourceStatus] = {}
        self.question_errors: dict[str, str] = {}  # alias → error al crear su vista
        self._ddl = threading.Lock()
        self.con = duckdb.connect(":memory:")
        if settings.extension_dir:
            self.con.execute(f"SET extension_directory = {q(settings.extension_dir)}")
        self._setup()
        self.sync_questions(questions or [])

    # ------------------------------------------------- preguntas como fuentes
    def sync_questions(self, questions: list) -> None:
        """Publica cada pregunta guardada como vista `preguntas.<alias>`.

        Las vistas se crean en orden de dependencias: se reintenta mientras alguna
        avance, así una pregunta puede usar otra que se crea más tarde.
        """
        with self._ddl:
            self.con.execute("DROP SCHEMA IF EXISTS preguntas CASCADE")
            self.con.execute("CREATE SCHEMA preguntas")
            self.question_errors = {}
            self._create_views({qq.slug: qq.sql for qq in questions if qq.slug})

    def upsert_question(self, slug: str, sql: str, all_questions: list) -> str | None:
        """Crea o actualiza la vista de una pregunta. Devuelve el error, si lo hay."""
        with self._ddl:
            pending = {slug: sql}
            # Las que fallaban antes (p. ej. porque dependían de esta) se reintentan
            pending.update({qq.slug: qq.sql for qq in all_questions if qq.slug in self.question_errors})
            self.question_errors.pop(slug, None)
            self._create_views(pending)
            return self.question_errors.get(slug)

    def drop_question(self, slug: str) -> None:
        with self._ddl:
            self.con.execute(f"DROP VIEW IF EXISTS preguntas.{_quote(slug)}")
            self.question_errors.pop(slug, None)

    def _create_views(self, pending: dict[str, str]) -> None:
        errors: dict[str, str] = {}
        while pending:
            progressed = False
            for slug, sql in list(pending.items()):
                try:
                    body = self.validate(sql)
                    self.con.execute(f"CREATE OR REPLACE VIEW preguntas.{_quote(slug)} AS {body}")
                except (QueryError, duckdb.Error) as e:
                    errors[slug] = _clean_error(str(e))
                    continue
                pending.pop(slug)
                errors.pop(slug, None)
                progressed = True
            if not progressed:
                break
        self.question_errors.update(errors)

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
                con.execute(f"CREATE VIEW files.{_ident(f.stem)} AS SELECT * FROM {reader}('{f.as_posix()}')")

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
        rows = (
            self.con.cursor()
            .execute(
                """
            SELECT database_name, schema_name, table_name, column_name, data_type
            FROM duckdb_columns()
            WHERE NOT internal AND database_name NOT IN ('system', 'temp')
              AND schema_name NOT IN ('information_schema', 'pg_catalog')
            ORDER BY database_name, schema_name, table_name, column_index
            """
            )
            .fetchall()
        )
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
            raise QueryError(f"Sentencia no permitida ({stmts[0].type.name}). Solo se admiten consultas de lectura.")
        return stmts[0].query.strip().rstrip(";")

    @contextmanager
    def stream(self, sql: str, max_rows: int, timeout_s: float | None = None) -> Iterator[RowStream]:
        """Ejecuta `sql` y entrega un RowStream para leerlo por lotes, con tiempo máximo."""
        sql = self.validate(sql)
        timeout_s = timeout_s or self.settings.export_timeout_s
        cur = self.con.cursor()
        timer = threading.Timer(timeout_s, cur.interrupt)
        timer.start()
        try:
            try:
                cur.execute(f"SELECT * FROM ({sql}) AS patobase_export LIMIT {int(max_rows) + 1}")
            except (duckdb.ParserException, duckdb.BinderException):
                cur.execute(sql)  # PRAGMA / SHOW / EXPLAIN no se pueden envolver
            if not cur.description:
                raise QueryError("La sentencia no devuelve resultados.")
            yield RowStream([d[0] for d in cur.description], [str(d[1]) for d in cur.description], cur, max_rows)
        except duckdb.InterruptException as e:
            raise QueryError(f"La exportación superó el límite de {timeout_s:g} s y se canceló.") from e
        except duckdb.Error as e:
            raise QueryError(str(e)) from e
        finally:
            timer.cancel()
            cur.close()

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
            raise QueryError(f"La consulta superó el límite de {self.settings.timeout_s:g} s y se canceló.") from e
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


@dataclass
class RowStream:
    """Resultado que se lee por lotes (para exportar sin cargarlo todo en memoria)."""

    columns: list[str]
    types: list[str]
    _cur: Any
    max_rows: int
    count: int = 0
    truncated: bool = False

    def batches(self, size: int = 10_000) -> Iterator[list[tuple]]:
        while self.count < self.max_rows:
            rows = self._cur.fetchmany(min(size, self.max_rows - self.count))
            if not rows:
                return
            self.count += len(rows)
            yield rows
        # Se pidió una fila de más para saber si había más resultados
        self.truncated = bool(self._cur.fetchmany(1))


def to_records(res: QueryResult) -> list[dict[str, Any]]:
    """Filas como dicts serializables a JSON (para Vega-Lite)."""
    out = []
    for row in res.rows:
        rec = {}
        for col, val in zip(res.columns, row, strict=False):
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


NUMERIC = (
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "FLOAT",
    "DOUBLE",
    "DECIMAL",
    "UBIGINT",
    "UINTEGER",
    "USMALLINT",
    "UTINYINT",
)
TEMPORAL = ("DATE", "TIMESTAMP", "TIME")


def vega_type(duck_type: str) -> str:
    t = duck_type.upper().strip()
    # Listas, arrays, structs, maps y uniones no son escalares aunque empiecen por INTEGER, etc.
    if t.endswith("]") or t.startswith(("STRUCT", "MAP", "UNION")):
        return "nominal"
    if t.startswith(NUMERIC):
        return "quantitative"
    if t.startswith(TEMPORAL):
        return "temporal"
    return "nominal"


def suggest_chart(columns: list[str], types: list[str]) -> dict[str, str]:
    """Elige un gráfico razonable según los tipos de las columnas."""
    vt = [vega_type(t) for t in types]
    nums = [c for c, t in zip(columns, vt, strict=False) if t == "quantitative"]
    dims = [c for c, t in zip(columns, vt, strict=False) if t != "quantitative"]
    temporal = [c for c, t in zip(columns, vt, strict=False) if t == "temporal"]
    if not nums or len(columns) < 2:
        return {"type": "table", "x": columns[0] if columns else "", "y": "", "color": ""}
    if temporal:
        return {"type": "line", "x": temporal[0], "y": nums[0], "color": dims[1] if len(dims) > 1 else ""}
    if dims:
        return {"type": "bar", "x": dims[0], "y": nums[0], "color": dims[1] if len(dims) > 1 else ""}
    return {"type": "point", "x": nums[0], "y": nums[1] if len(nums) > 1 else nums[0], "color": ""}
