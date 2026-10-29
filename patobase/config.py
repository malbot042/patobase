"""Configuración de patobase, leída de variables de entorno."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    # Carpeta con ficheros .duckdb / .parquet / .csv que se exponen como fuentes
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("PATOBASE_DATA_DIR", "data")))
    # Base SQLite con preguntas guardadas y dashboards
    meta_db: Path = field(default_factory=lambda: Path(os.getenv("PATOBASE_META_DB", "patobase_meta.sqlite")))
    # Sentencias ATTACH extra (separadas por ';'), p. ej. Postgres/MySQL
    extra_attach: str = field(default_factory=lambda: os.getenv("PATOBASE_ATTACH", ""))
    # Carpeta de extensiones DuckDB (para servidores sin salida a Internet)
    extension_dir: str = field(default_factory=lambda: os.getenv("PATOBASE_EXTENSION_DIR", ""))
    page_size: int = int(os.getenv("PATOBASE_PAGE_SIZE", "100"))
    chart_rows: int = int(os.getenv("PATOBASE_CHART_ROWS", "2000"))
    timeout_s: float = float(os.getenv("PATOBASE_TIMEOUT", "30"))
    # Exportaciones: filas máximas (CSV/Excel), filas en PDF y tiempo máximo
    export_rows: int = int(os.getenv("PATOBASE_EXPORT_ROWS", "1000000"))
    pdf_rows: int = int(os.getenv("PATOBASE_PDF_ROWS", "2000"))
    export_timeout_s: float = float(os.getenv("PATOBASE_EXPORT_TIMEOUT", "300"))


settings = Settings()
