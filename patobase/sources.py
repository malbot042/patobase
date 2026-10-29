"""Fuentes de datos externas configurables desde la UI.

Cada tipo de fuente declara sus campos (para pintar el formulario) y sabe
convertirse en las sentencias DuckDB que la adjuntan. Las credenciales se pasan
siempre con ``CREATE SECRET`` (valores como literales SQL), nunca concatenadas
en cadenas de conexión, para no tener que escapar contraseñas raras.

Cualquier campo secreto admite la forma ``env:NOMBRE_VARIABLE`` para no guardar
la contraseña en la base de metadatos.
"""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
RESERVED = {
    "files",
    "remote",
    "memory",
    "system",
    "temp",
    "main",
    "information_schema",
    "pg_catalog",
    "default",
}
TCP_TIMEOUT_S = 5.0


class SourceError(Exception):
    pass


# ---------------------------------------------------------------------------
# Definición de tipos y campos del formulario
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Field:
    key: str
    label: str
    type: str = "text"  # text | password | number | select | checkbox
    required: bool = False
    default: Any = ""
    placeholder: str = ""
    help: str = ""
    options: tuple[tuple[str, str], ...] = ()
    secret: bool = False
    wide: bool = False  # ocupa toda la fila en el formulario


@dataclass(frozen=True)
class Kind:
    key: str
    label: str
    description: str
    extension: str | None  # extensión DuckDB necesaria
    fields: tuple[Field, ...] = field(default_factory=tuple)


_FORMAT = Field(
    "format", "Formato", "select", True, "parquet", options=(("parquet", "Parquet"), ("csv", "CSV"), ("json", "JSON"))
)
_MODE = Field(
    "mode",
    "Modo",
    "select",
    True,
    "live",
    options=(
        ("live", "En vivo (consulta el origen cada vez)"),
        ("snapshot", "Copia en memoria (se refresca al recargar)"),
    ),
    help="La copia es más rápida para datos pequeños; en vivo, siempre al día.",
)

KINDS: dict[str, Kind] = {
    "postgres": Kind(
        "postgres",
        "PostgreSQL",
        "Postgres, Supabase, Neon, RDS, Aurora…",
        "postgres",
        (
            Field("host", "Servidor", required=True, placeholder="db.miempresa.local"),
            Field("port", "Puerto", "number", True, 5432),
            Field("database", "Base de datos", required=True, placeholder="erp"),
            Field("user", "Usuario", required=True, placeholder="lectura"),
            Field(
                "password", "Contraseña", "password", secret=True, help="O env:NOMBRE_VARIABLE para leerla del entorno."
            ),
            Field("schema", "Esquema", placeholder="todos", help="Opcional: limita la fuente a un esquema."),
            Field(
                "sslmode",
                "SSL",
                "select",
                False,
                "prefer",
                options=(("prefer", "Si está disponible"), ("require", "Obligatorio"), ("disable", "Desactivado")),
            ),
        ),
    ),
    "mysql": Kind(
        "mysql",
        "MySQL / MariaDB",
        "MySQL, MariaDB, PlanetScale…",
        "mysql",
        (
            Field("host", "Servidor", required=True, placeholder="mysql.miempresa.local"),
            Field("port", "Puerto", "number", True, 3306),
            Field("database", "Base de datos", required=True, placeholder="crm"),
            Field("user", "Usuario", required=True, placeholder="lectura"),
            Field(
                "password", "Contraseña", "password", secret=True, help="O env:NOMBRE_VARIABLE para leerla del entorno."
            ),
        ),
    ),
    "sqlite": Kind(
        "sqlite",
        "SQLite",
        "Un fichero .sqlite / .db",
        "sqlite",
        (
            Field(
                "path",
                "Fichero",
                required=True,
                placeholder="app.sqlite",
                wide=True,
                help="Ruta dentro de la carpeta de datos.",
            ),
        ),
    ),
    "duckdb": Kind(
        "duckdb",
        "DuckDB",
        "Otro fichero .duckdb",
        None,
        (
            Field(
                "path",
                "Fichero",
                required=True,
                placeholder="historico.duckdb",
                wide=True,
                help="Ruta dentro de la carpeta de datos.",
            ),
        ),
    ),
    "s3": Kind(
        "s3",
        "S3 / R2 / GCS / MinIO",
        "Ficheros en almacenamiento de objetos",
        "httpfs",
        (
            Field(
                "url",
                "Ruta",
                required=True,
                placeholder="s3://bucket/ventas/*.parquet",
                wide=True,
                help="Admite comodines (*, **). gs:// para Google Cloud Storage; R2 y MinIO usan s3:// con su endpoint.",
            ),
            _FORMAT,
            _MODE,
            Field("region", "Región", placeholder="eu-west-1"),
            Field("key_id", "Access key ID", placeholder="vacío = sin credenciales"),
            Field("secret", "Secret access key", "password", secret=True),
            Field(
                "endpoint",
                "Endpoint",
                placeholder="vacío = AWS",
                help="Para R2, MinIO u otros compatibles, p. ej. <cuenta>.r2.cloudflarestorage.com",
            ),
            Field(
                "url_style",
                "Estilo de URL",
                "select",
                False,
                "vhost",
                options=(("vhost", "Virtual host (AWS)"), ("path", "Path (MinIO)")),
            ),
            Field("use_ssl", "Usar HTTPS", "checkbox", False, True),
        ),
    ),
    "url": Kind(
        "url",
        "URL (HTTP)",
        "Un CSV, Parquet o JSON publicado en la web",
        "httpfs",
        (
            Field("url", "URL", required=True, placeholder="https://ejemplo.org/datos.csv", wide=True),
            _FORMAT,
            _MODE,
        ),
    ),
}

ATTACHED = {"postgres", "mysql", "sqlite", "duckdb"}  # aparecen como base adjunta
REMOTE = {"s3", "url"}  # aparecen como remote.<nombre>


# ---------------------------------------------------------------------------
# Fuente concreta
# ---------------------------------------------------------------------------
@dataclass
class Source:
    id: int | None
    name: str
    kind: str
    config: dict[str, Any]
    enabled: bool = True

    @property
    def kind_def(self) -> Kind:
        return KINDS[self.kind]

    def summary(self) -> str:
        c = self.config
        if self.kind in ("postgres", "mysql"):
            return f"{c.get('user', '')}@{c.get('host', '')}:{c.get('port', '')}/{c.get('database', '')}"
        if self.kind in ("sqlite", "duckdb"):
            return str(c.get("path", ""))
        return str(c.get("url", ""))

    def where(self) -> str:
        """Cómo se llama en SQL."""
        if self.kind in REMOTE:
            return f"remote.{self.name}"
        return f"{self.name}.…"


@dataclass
class Plan:
    """Lo que hay que ejecutar en DuckDB para activar una fuente."""

    extension: str | None
    statements: list[str]
    allowed_prefix: str | None = None  # prefijo remoto a permitir en el sandbox
    tcp_check: tuple[str, int] | None = None
    probe: str = ""  # consulta barata para comprobar que funciona


def q(value: Any) -> str:
    """Literal SQL entre comillas simples."""
    return "'" + str(value).replace("'", "''") + "'"


def resolve_secret(value: str) -> str:
    if isinstance(value, str) and value.startswith("env:"):
        var = value[4:].strip()
        if var not in os.environ:
            raise SourceError(f"La variable de entorno {var} no está definida.")
        return os.environ[var]
    return value


def safe_path(data_dir: Path, raw: str) -> Path:
    """Solo se permiten ficheros dentro de la carpeta de datos."""
    base = data_dir.resolve()
    p = (base / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    if p != base and base not in p.parents:
        raise SourceError(f"El fichero debe estar dentro de la carpeta de datos ({base}).")
    if not p.is_file():
        raise SourceError(f"No existe el fichero {p.name} en la carpeta de datos.")
    return p


def remote_prefix(url: str) -> str:
    """Parte fija de una ruta con comodines: s3://b/x/*.parquet → s3://b/x/"""
    cut = len(url)
    for ch in "*?[{":
        i = url.find(ch)
        if i != -1:
            cut = min(cut, i)
    head = url[:cut]
    if cut < len(url):
        head = head[: head.rfind("/") + 1]
    return head


def reader(fmt: str, url: str) -> str:
    fn = {"parquet": "read_parquet", "csv": "read_csv_auto", "json": "read_json_auto"}[fmt]
    return f"{fn}({q(url)})"


def plan(src: Source, data_dir: Path) -> Plan:
    c = src.config
    n = src.name
    secret = f"pb_{n}"

    if src.kind == "postgres":
        opts = [f"sslmode={c.get('sslmode') or 'prefer'}", "connect_timeout=5"]
        attach_opts = ["TYPE postgres", f"SECRET {secret}", "READ_ONLY"]
        if c.get("schema"):
            attach_opts.append(f"SCHEMA {q(c['schema'])}")
        return Plan(
            "postgres",
            [
                f"CREATE OR REPLACE SECRET {secret} (TYPE postgres, HOST {q(c['host'])}, PORT {int(c['port'])}, "
                f"DATABASE {q(c['database'])}, USER {q(c['user'])}, PASSWORD {q(resolve_secret(c.get('password', '')))})",
                f"ATTACH {q(' '.join(opts))} AS {n} ({', '.join(attach_opts)})",
            ],
            tcp_check=(c["host"], int(c["port"])),
            probe=_count_tables(n),
        )

    if src.kind == "mysql":
        return Plan(
            "mysql",
            [
                f"CREATE OR REPLACE SECRET {secret} (TYPE mysql, HOST {q(c['host'])}, PORT {int(c['port'])}, "
                f"DATABASE {q(c['database'])}, USER {q(c['user'])}, PASSWORD {q(resolve_secret(c.get('password', '')))})",
                f"ATTACH '' AS {n} (TYPE mysql, SECRET {secret}, READ_ONLY)",
            ],
            tcp_check=(c["host"], int(c["port"])),
            probe=_count_tables(n),
        )

    if src.kind in ("sqlite", "duckdb"):
        path = safe_path(data_dir, c["path"])
        typ = ", TYPE sqlite" if src.kind == "sqlite" else ""
        return Plan(
            "sqlite" if src.kind == "sqlite" else None,
            [f"ATTACH {q(path.as_posix())} AS {n} (READ_ONLY{typ})"],
            probe=_count_tables(n),
        )

    if src.kind in REMOTE:
        url = c["url"].strip()
        stmts = []
        if src.kind == "s3":
            if not re.match(r"^(s3|gs|gcs)://", url):
                raise SourceError("La ruta debe empezar por s3:// o gs://")
            secret_type = "gcs" if url.startswith(("gs://", "gcs://")) else "s3"
            parts = [f"TYPE {secret_type}", f"SCOPE {q(remote_prefix(url))}"]
            if c.get("key_id"):
                parts += [f"KEY_ID {q(c['key_id'])}", f"SECRET {q(resolve_secret(c.get('secret', '')))}"]
            else:
                parts.append("PROVIDER config")
            if c.get("region"):
                parts.append(f"REGION {q(c['region'])}")
            if c.get("endpoint"):
                parts.append(f"ENDPOINT {q(c['endpoint'])}")
            parts.append(f"URL_STYLE {q(c.get('url_style') or 'vhost')}")
            parts.append(f"USE_SSL {'true' if c.get('use_ssl', True) else 'false'}")
            stmts.append(f"CREATE OR REPLACE SECRET {secret} ({', '.join(parts)})")
        elif not re.match(r"^https?://", url):
            raise SourceError("La URL debe empezar por http:// o https://")
        obj = "VIEW" if c.get("mode", "live") == "live" else "TABLE"
        stmts += [
            "CREATE SCHEMA IF NOT EXISTS remote",
            f"CREATE OR REPLACE {obj} remote.{n} AS SELECT * FROM {reader(c.get('format') or 'parquet', url)}",
        ]
        return Plan(
            "httpfs", stmts, allowed_prefix=remote_prefix(url), probe=f"SELECT count(*) FROM (DESCRIBE remote.{n})"
        )

    raise SourceError(f"Tipo de fuente desconocido: {src.kind}")


def _count_tables(db: str) -> str:
    return (
        "SELECT count(*) FROM duckdb_tables() "
        f"WHERE database_name = {q(db)} AND schema_name NOT IN ('information_schema', 'pg_catalog')"
    )


def tcp_reachable(host: str, port: int, timeout: float = TCP_TIMEOUT_S) -> None:
    """Comprobación previa: algunas extensiones (MySQL) se cuelgan minutos si el host no responde."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError as e:
        raise SourceError(f"No se puede conectar con {host}:{port} ({e.strerror or e}).") from e


# ---------------------------------------------------------------------------
# Validación de lo que llega del formulario
# ---------------------------------------------------------------------------
def from_form(kind: str, form: dict[str, Any], previous: Source | None = None, taken: set[str] = frozenset()) -> Source:
    if kind not in KINDS:
        raise SourceError("Elige un tipo de fuente.")
    name = str(form.get("name", "")).strip().lower()
    if not NAME_RE.match(name):
        raise SourceError("El nombre debe empezar por letra y tener solo minúsculas, números o _ (máx. 40).")
    if name in RESERVED:
        raise SourceError(f"«{name}» es un nombre reservado.")
    if name in taken and (not previous or previous.name != name):
        raise SourceError(f"Ya existe una fuente llamada «{name}».")

    cfg: dict[str, Any] = {}
    for f in KINDS[kind].fields:
        raw = form.get(f.key)
        if f.type == "checkbox":
            cfg[f.key] = raw in ("on", "true", "1", True)
            continue
        val = "" if raw is None else str(raw).strip()
        if f.secret and not val and previous:
            val = previous.config.get(f.key, "")  # vacío en edición = sin cambios
        if f.type == "number" and val:
            if not val.isdigit():
                raise SourceError(f"«{f.label}» debe ser un número.")
            val = int(val)
        if f.required and val in ("", None):
            raise SourceError(f"Falta «{f.label}».")
        cfg[f.key] = val if val != "" else (f.default if f.type == "select" else "")
    return Source(previous.id if previous else None, name, kind, cfg, previous.enabled if previous else True)
