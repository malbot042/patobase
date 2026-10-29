# patobase

Consultas SQL y dashboards al estilo Metabase, con **DuckDB** como motor, **Pico CSS** para los estilos y un
frontend sin build: **htmx** para todo lo que va al servidor y **Alpine.js** para
el estado de la interfaz.

![Stack](https://img.shields.io/badge/stack-FastAPI%20·%20DuckDB%20·%20htmx%20·%20Alpine%20·%20Pico-d4890b)

## Qué hace

- **Editor SQL** (CodeMirror) con resaltado, autocompletado de tablas y columnas reales y `Ctrl/⌘ + Enter`.
- **Explorador de esquemas**: todas las fuentes en el panel lateral; clic en una columna la inserta, «ver» lanza un `SELECT *`.
- **Resultados** en tabla paginada en servidor, o como **gráfico** (barras, líneas, área, dispersión, sectores) con Vega-Lite. El gráfico se sugiere solo según los tipos de las columnas.
- **Preguntas guardadas** con su configuración de gráfico.
- **Dashboards** con tarjetas de media o anchura completa, refresco manual o automático. Cada tarjeta carga en paralelo con htmx.
- **Modo oscuro** automático y formato de fechas y números en castellano.

## Fuentes de datos

Todo lo que pongas en la carpeta de datos (`./data` por defecto) aparece automáticamente:

| Fichero | Se ve como |
|---|---|
| `*.duckdb` | base adjunta en solo lectura: `nombre.main.tabla` |
| `*.parquet`, `*.csv` | vista `files.nombre` |

Para Postgres, MySQL, S3, etc. usa `PATOBASE_ATTACH` con sentencias `ATTACH` separadas por `;`:

```bash
export PATOBASE_ATTACH="INSTALL postgres; LOAD postgres; ATTACH 'dbname=erp host=localhost user=lectura' AS erp (TYPE postgres, READ_ONLY)"
```

## Arrancar

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python scripts/seed.py        # datos de ejemplo: ventas, clientes y una base 'tienda'
patobase                      # http://127.0.0.1:8000
```

Para desarrollar con recarga automática: `PATOBASE_RELOAD=1 patobase`.

Tests: `pytest`.

## Configuración

| Variable | Por defecto | |
|---|---|---|
| `PATOBASE_DATA_DIR` | `data` | carpeta de fuentes |
| `PATOBASE_META_DB` | `patobase_meta.sqlite` | preguntas y dashboards |
| `PATOBASE_ATTACH` | | sentencias `ATTACH` extra |
| `PATOBASE_PAGE_SIZE` | `100` | filas por página en la tabla |
| `PATOBASE_CHART_ROWS` | `2000` | filas máximas que se envían al gráfico |
| `PATOBASE_TIMEOUT` | `30` | segundos antes de cancelar una consulta |
| `PATOBASE_HOST` / `PATOBASE_PORT` | `127.0.0.1` / `8000` | |

## Seguridad

Al arrancar, después de adjuntar las fuentes, el motor:

1. Limita la lectura de ficheros a la carpeta de datos (`allowed_directories`).
2. Desactiva el acceso externo (`enable_external_access = false`).
3. Bloquea la configuración (`lock_configuration = true`), así no se puede revertir desde el editor.

Además solo se aceptan sentencias de lectura (`SELECT`, `EXPLAIN`, `PRAGMA`/`SHOW`/`DESCRIBE`), una por petición, y cada consulta tiene un tiempo máximo.

> No hay autenticación todavía: no lo expongas fuera de tu red.

## Estructura

```
patobase/
  config.py        variables de entorno
  engine.py        DuckDB: fuentes, sandbox, ejecución, sugerencia de gráfico
  store.py         SQLite: preguntas, dashboards y tarjetas
  main.py          rutas FastAPI (páginas completas y parciales htmx)
  templates/       Jinja2; partials/ son los fragmentos que intercambia htmx
  static/app.css   layout de la app y ajustes de densidad sobre Pico CSS
  static/app.js    CodeMirror, Vega-Lite y componentes Alpine
  static/vendor/   librerías JS incluidas (sin CDN, funciona offline)
scripts/seed.py    datos de ejemplo
tests/             pytest
```

Los metadatos van en SQLite y no en DuckDB porque son escrituras pequeñas y concurrentes, justo lo que DuckDB no hace bien (un solo escritor por proceso).

## Próximos pasos posibles

- Parámetros en las preguntas (`WHERE fecha >= $desde`) y filtros globales del dashboard.
- Usuarios y permisos por fuente.
- Caché de resultados con TTL.
- Constructor visual de consultas (sin SQL) con SQLGlot o Ibis.
- Reordenar tarjetas arrastrando.
- Exportar resultados a CSV / Parquet.
