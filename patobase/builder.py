"""Editor visual: convierte una especificación JSON en SQL de DuckDB.

La especificación sigue los pasos del «notebook» de Metabase::

    {
      "source":  "files.ventas",                       # tabla o preguntas.<alias>
      "joins":   [{"table": "files.clientes", "how": "left",
                   "left": "t0.cliente_id", "right": "cliente_id"}],
      "columns": ["t0.fecha", "t1.ciudad"],            # solo sin resumir; vacío = todas
      "filters": [{"col": "t0.fecha", "op": "last_days", "value": "90"}],
      "metrics": [{"agg": "sum", "col": "t0.importe"}],
      "groups":  [{"col": "t0.fecha", "bucket": "month"}],
      "sort":    [{"by": "suma_importe", "dir": "desc"}],
      "limit":   100
    }

Todo identificador se comprueba contra el catálogo real (tablas y columnas que
existen) y se entrecomilla; todo valor va como literal SQL escapado. Nada del
JSON llega al SQL sin pasar por esa validación.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .engine import vega_type

MAX_JOINS = 4
MAX_ITEMS = 30
MAX_LIMIT = 1_000_000

AGGS = {
    "count": ("Recuento", None),
    "count_distinct": ("Valores distintos de", None),
    "sum": ("Suma de", "quantitative"),
    "avg": ("Media de", "quantitative"),
    "min": ("Mínimo de", None),
    "max": ("Máximo de", None),
}
AGG_ALIAS = {
    "count": "recuento",
    "count_distinct": "distintos",
    "sum": "suma",
    "avg": "media",
    "min": "min",
    "max": "max",
}

BUCKETS = {"": "", "day": "día", "week": "semana", "month": "mes", "quarter": "trimestre", "year": "año"}

# Operadores de filtro por tipo de columna (vega_type)
OPS: dict[str, str] = {
    "eq": "es igual a",
    "ne": "no es igual a",
    "gt": "mayor que",
    "gte": "mayor o igual que",
    "lt": "menor que",
    "lte": "menor o igual que",
    "between": "entre",
    "contains": "contiene",
    "not_contains": "no contiene",
    "starts": "empieza por",
    "ends": "termina en",
    "in": "es uno de",
    "last_days": "en los últimos N días",
    "null": "está vacío",
    "not_null": "no está vacío",
}
OPS_BY_TYPE = {
    "quantitative": ["eq", "ne", "gt", "gte", "lt", "lte", "between", "in", "null", "not_null"],
    "temporal": ["eq", "gt", "gte", "lt", "lte", "between", "last_days", "null", "not_null"],
    "nominal": ["eq", "ne", "contains", "not_contains", "starts", "ends", "in", "null", "not_null"],
}
NO_VALUE = {"null", "not_null"}


class BuildError(Exception):
    pass


@dataclass
class Table:
    key: str  # "files.ventas", "tienda.main.productos", "preguntas.mi_pregunta"
    prefix: str  # "files", "tienda.main", "preguntas"
    name: str
    columns: dict[str, str]  # columna → tipo DuckDB

    @property
    def sql(self) -> str:
        return ".".join(quote(p) for p in self.prefix.split(".")) + "." + quote(self.name)


@dataclass
class Compiled:
    sql: str
    columns: list[dict[str, str]] = field(default_factory=list)  # [{"name", "type"}] de la salida


def quote(ident: str) -> str:
    return '"' + str(ident).replace('"', '""') + '"'


def literal(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def catalog_from_schema(schema: dict[str, dict[str, list[tuple[str, str]]]]) -> dict[str, Table]:
    """Convierte Engine.schema() en {clave_tabla: Table}."""
    out = {}
    for prefix, tables in schema.items():
        for name, cols in tables.items():
            key = f"{prefix}.{name}"
            out[key] = Table(key, prefix, name, dict(cols))
    return out


def _alias_name(text: str) -> str:
    t = re.sub(r"[^0-9A-Za-z_áéíóúüñÁÉÍÓÚÜÑ]+", "_", text).strip("_").lower()
    return t or "col"


class _Ctx:
    def __init__(self, catalog: dict[str, Table]):
        self.catalog = catalog
        self.tables: list[Table] = []  # índice = alias tN

    def add_table(self, key: str) -> int:
        if not isinstance(key, str) or key not in self.catalog:
            raise BuildError(f"La tabla «{key}» no existe o no está disponible.")
        self.tables.append(self.catalog[key])
        return len(self.tables) - 1

    def column(self, ref: Any) -> tuple[str, str, str, int]:
        """«t1.ciudad» → (sql, nombre, tipo, índice de tabla)."""
        m = re.fullmatch(r"t(\d+)\.(.+)", str(ref or ""), flags=re.S)
        if not m:
            raise BuildError(f"Columna no válida: «{ref}».")
        idx, col = int(m.group(1)), m.group(2)
        if idx >= len(self.tables) or col not in self.tables[idx].columns:
            raise BuildError(f"La columna «{col}» no existe.")
        return f"t{idx}.{quote(col)}", col, self.tables[idx].columns[col], idx


class _Names:
    """Nombres de salida únicos."""

    def __init__(self):
        self.used: set[str] = set()

    def take(self, wanted: str) -> str:
        name, n = wanted, 2
        while name in self.used:
            name, n = f"{wanted}_{n}", n + 1
        self.used.add(name)
        return name


def _value(raw: Any, vtype: str, label: str) -> str:
    text = "" if raw is None else str(raw).strip()
    if text == "":
        raise BuildError(f"Falta el valor del filtro sobre «{label}».")
    if vtype == "quantitative":
        try:
            float(text.replace(",", "."))
        except ValueError as e:
            raise BuildError(f"«{text}» no es un número (filtro sobre «{label}»).") from e
        return text.replace(",", ".")
    return literal(text)


def _filter_sql(ctx: _Ctx, f: dict) -> str:
    col_sql, col, dtype, _ = ctx.column(f.get("col"))
    vtype = vega_type(dtype)
    op = f.get("op", "")
    if op not in OPS_BY_TYPE[vtype]:
        raise BuildError(f"El filtro «{OPS.get(op, op)}» no se puede usar con «{col}».")
    if op == "null":
        return f"{col_sql} IS NULL"
    if op == "not_null":
        return f"{col_sql} IS NOT NULL"
    if op == "last_days":
        n = str(f.get("value", "")).strip()
        if not n.isdigit() or not 0 < int(n) <= 100_000:
            raise BuildError(f"Indica un número de días válido (filtro sobre «{col}»).")
        return f"{col_sql} >= current_date - INTERVAL {int(n)} DAY"
    if op == "in":
        items = [x.strip() for x in str(f.get("value", "")).split(",") if x.strip()]
        if not items:
            raise BuildError(f"Indica uno o más valores separados por comas (filtro sobre «{col}»).")
        return f"{col_sql} IN ({', '.join(_value(x, vtype, col) for x in items[:500])})"
    if op in ("contains", "not_contains", "starts", "ends"):
        v = literal(str(f.get("value", "")))
        if v == "''":
            raise BuildError(f"Falta el texto del filtro sobre «{col}».")
        text = f"lower(CAST({col_sql} AS VARCHAR))"
        fn = {"contains": "contains", "not_contains": "contains", "starts": "starts_with", "ends": "ends_with"}[op]
        expr = f"{fn}({text}, lower({v}))"
        return f"NOT {expr}" if op == "not_contains" else expr
    a = _value(f.get("value"), vtype, col)
    if op == "between":
        b = _value(f.get("value2"), vtype, col)
        return f"{col_sql} BETWEEN {a} AND {b}"
    sym = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
    return f"{col_sql} {sym} {a}"


def compile_spec(spec: dict, catalog: dict[str, Table]) -> Compiled:
    if not isinstance(spec, dict):
        raise BuildError("Especificación no válida.")
    ctx = _Ctx(catalog)
    if not spec.get("source"):
        raise BuildError("Elige los datos de partida.")
    ctx.add_table(spec["source"])

    # ---- uniones
    joins_sql = []
    joins = spec.get("joins") or []
    if len(joins) > MAX_JOINS:
        raise BuildError(f"Como máximo {MAX_JOINS} uniones.")
    for j in joins:
        idx = ctx.add_table(j.get("table"))
        how = {"inner": "JOIN", "left": "LEFT JOIN"}.get(j.get("how", "left"))
        if not how:
            raise BuildError("Tipo de unión no válido.")
        left_sql, _, _, left_idx = ctx.column(j.get("left"))
        if left_idx >= idx:
            raise BuildError("La columna de la izquierda debe ser de una tabla anterior.")
        right = j.get("right")
        if right not in ctx.tables[idx].columns:
            raise BuildError(f"La columna «{right}» no existe en {ctx.tables[idx].key}.")
        joins_sql.append(f"{how} {ctx.tables[idx].sql} AS t{idx} ON {left_sql} = t{idx}.{quote(right)}")

    # ---- filtros
    filters = spec.get("filters") or []
    if len(filters) > MAX_ITEMS:
        raise BuildError("Demasiados filtros.")
    where = [_filter_sql(ctx, f) for f in filters]

    # ---- columnas de salida
    names = _Names()
    select: list[str] = []
    out_cols: list[dict[str, str]] = []
    group_by: list[str] = []
    metrics = spec.get("metrics") or []
    groups = spec.get("groups") or []
    if len(metrics) > MAX_ITEMS or len(groups) > MAX_ITEMS:
        raise BuildError("Demasiadas métricas o agrupaciones.")

    if metrics or groups:
        for g in groups:
            col_sql, col, dtype, _ = ctx.column(g.get("col"))
            bucket = g.get("bucket") or ""
            if bucket not in BUCKETS:
                raise BuildError("Granularidad no válida.")
            if bucket:
                if vega_type(dtype) != "temporal":
                    raise BuildError(f"«{col}» no es una fecha: no se puede agrupar por {BUCKETS[bucket]}.")
                expr, otype = f"CAST(date_trunc({literal(bucket)}, {col_sql}) AS DATE)", "DATE"
                alias = names.take(_alias_name(f"{col}_{BUCKETS[bucket]}"))
            else:
                expr, otype = col_sql, dtype
                alias = names.take(_alias_name(col))
            select.append(f"{expr} AS {quote(alias)}")
            group_by.append(expr)
            out_cols.append({"name": alias, "type": otype})
        for m in metrics:
            agg = m.get("agg")
            if agg not in AGGS:
                raise BuildError("Métrica no válida.")
            if agg == "count":
                expr, otype, alias = "count(*)", "BIGINT", names.take("recuento")
            else:
                col_sql, col, dtype, _ = ctx.column(m.get("col"))
                need = AGGS[agg][1]
                if need and vega_type(dtype) != need:
                    raise BuildError(f"«{AGGS[agg][0]}» necesita una columna numérica y «{col}» no lo es.")
                if agg == "count_distinct":
                    expr, otype = f"count(DISTINCT {col_sql})", "BIGINT"
                else:
                    fn = {"sum": "sum", "avg": "avg", "min": "min", "max": "max"}[agg]
                    expr = f"{fn}({col_sql})"
                    otype = "DOUBLE" if agg == "avg" else dtype
                alias = names.take(_alias_name(f"{AGG_ALIAS[agg]}_{col}"))
            select.append(f"{expr} AS {quote(alias)}")
            out_cols.append({"name": alias, "type": otype})
    else:
        wanted = spec.get("columns") or []
        refs = (
            [ctx.column(c) for c in wanted]
            if wanted
            else [ctx.column(f"t{i}.{c}") for i, t in enumerate(ctx.tables) for c in t.columns]
        )
        for col_sql, col, dtype, idx in refs:
            # Si el nombre ya está (misma columna en otra tabla), se prefija con la tabla
            alias = names.take(col if col not in names.used else f"{ctx.tables[idx].name}_{col}")
            select.append(f"{col_sql} AS {quote(alias)}")
            out_cols.append({"name": alias, "type": dtype})

    # ---- orden y límite
    order = []
    out_names = {c["name"] for c in out_cols}
    for s in spec.get("sort") or []:
        by = s.get("by")
        if not by:  # orden añadido pero aún sin columna: se ignora
            continue
        if by not in out_names:
            raise BuildError(f"No se puede ordenar por «{by}»: no está en el resultado.")
        order.append(f"{quote(by)} {'DESC' if s.get('dir') == 'desc' else 'ASC'}")

    limit = spec.get("limit")
    if limit not in (None, ""):
        try:
            limit = int(limit)
        except (TypeError, ValueError) as e:
            raise BuildError("El límite debe ser un número entero.") from e
        if not 1 <= limit <= MAX_LIMIT:
            raise BuildError(f"El límite debe estar entre 1 y {MAX_LIMIT:,}.".replace(",", "."))

    sql = "SELECT " + ",\n       ".join(select)
    sql += f"\nFROM {ctx.tables[0].sql} AS t0"
    for j in joins_sql:
        sql += f"\n{j}"
    if where:
        sql += "\nWHERE " + "\n  AND ".join(where)
    if group_by and metrics:
        sql += "\nGROUP BY " + ", ".join(group_by)
    elif group_by:  # agrupar sin métricas = valores distintos
        sql = sql.replace("SELECT ", "SELECT DISTINCT ", 1)
    if order:
        sql += "\nORDER BY " + ", ".join(order)
    elif group_by:
        sql += "\nORDER BY " + ", ".join(str(i + 1) for i in range(len(groups)))
    if limit not in (None, ""):
        sql += f"\nLIMIT {limit}"
    return Compiled(sql, out_cols)


def ui_catalog(catalog: dict[str, Table]) -> dict[str, Any]:
    """Lo que necesita el editor visual en el navegador: tablas, columnas y tipos."""
    return {
        "tables": {
            k: {"prefix": t.prefix, "name": t.name, "columns": [[c, vega_type(d)] for c, d in t.columns.items()]}
            for k, t in sorted(catalog.items())
        },
        "ops": OPS,
        "opsByType": OPS_BY_TYPE,
        "noValue": sorted(NO_VALUE),
        "aggs": {k: v[0] for k, v in AGGS.items()},
        "aggNeeds": {k: v[1] for k, v in AGGS.items()},
        "buckets": BUCKETS,
    }
