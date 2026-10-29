# patobase

Consultas SQL y dashboards al estilo Metabase, con **DuckDB** como motor, **Pico CSS** para los estilos y un
frontend sin build: **htmx** para todo lo que va al servidor y **Alpine.js** para
el estado de la interfaz.

![Stack](https://img.shields.io/badge/stack-FastAPI%20·%20DuckDB%20·%20htmx%20·%20Alpine%20·%20Pico-d4890b)

## Qué hace

- **Editor SQL** (CodeMirror) con resaltado, autocompletado de tablas y columnas reales y `Ctrl/⌘ + Enter`.
- **Fuentes de datos desde la UI**: PostgreSQL, MySQL/MariaDB, SQLite, DuckDB, S3/GCS/R2/MinIO y URLs HTTP, con «Probar conexión», activar/desactivar y recarga en caliente.
- **Explorador de esquemas**: todas las fuentes en el panel lateral; clic en una columna la inserta, «ver» lanza un `SELECT *`.
- **Resultados** en tabla paginada en servidor, o como **gráfico** (barras, líneas, área, dispersión, sectores) con Vega-Lite. El gráfico se sugiere solo según los tipos de las columnas.
- **Preguntas guardadas** con su configuración de gráfico.
- **Dashboards** con tarjetas de media o anchura completa, refresco manual o automático. Cada tarjeta carga en paralelo con htmx.
- **Modo oscuro** automático y formato de fechas y números en castellano.

## Fuentes de datos

### Desde la interfaz (página **Fuentes**)

| Tipo | Qué pide | Se consulta como |
|---|---|---|
| PostgreSQL | servidor, puerto, base, usuario, contraseña, esquema y SSL opcionales | `nombre.esquema.tabla` |
| MySQL / MariaDB | servidor, puerto, base, usuario, contraseña | `nombre.base.tabla` |
| SQLite | fichero dentro de la carpeta de datos | `nombre.main.tabla` |
| DuckDB | fichero dentro de la carpeta de datos | `nombre.main.tabla` |
| S3 / GCS / R2 / MinIO | ruta con comodines (`s3://bucket/ventas/*.parquet`), formato, credenciales, endpoint | `remote.nombre` |
| URL (HTTP) | URL de un CSV, Parquet o JSON | `remote.nombre` |

- **Probar conexión** antes de guardar muestra cuántas tablas o columnas ve y algunos nombres.
- Al guardar, activar, desactivar o borrar una fuente, el motor se **reconstruye en caliente**, sin reiniciar. Las consultas en curso terminan con el motor anterior.
- Una fuente que falla (host caído, contraseña incorrecta) **se marca en rojo** y no afecta al resto. El editor avisa en el panel lateral.
- Antes de conectar con Postgres o MySQL se comprueba el puerto con un timeout de 5 s: la extensión de MySQL puede quedarse colgada minutos si el host no responde.
- Los ficheros remotos admiten dos modos: **en vivo** (vista que lee el origen en cada consulta) o **copia en memoria** (se carga al recargar; más rápido para datos pequeños).
- Las credenciales se pasan a DuckDB con `CREATE SECRET`, nunca en cadenas de conexión, y **no se vuelven a mostrar** en la interfaz.

**Contraseñas.** Se guardan en la base de metadatos (`patobase_meta.sqlite`). Para no guardarlas, escribe `env:NOMBRE_VARIABLE` en el campo y define esa variable al arrancar:

```bash
export ERP_PASSWORD='...'
patobase          # en el formulario: contraseña = env:ERP_PASSWORD
```

Usa siempre **usuarios de solo lectura** en las bases de datos: patobase adjunta en `READ_ONLY`, pero el permiso del propio servidor es la mejor garantía.

**Extensiones de DuckDB.** Postgres, MySQL, SQLite y S3/HTTP necesitan extensiones que DuckDB descarga la primera vez. En un servidor sin salida a Internet, descárgalas en otra máquina y apunta `PATOBASE_EXTENSION_DIR` a esa carpeta.

### Carpeta de datos

Todo lo que pongas en la carpeta de datos (`./data` por defecto) aparece automáticamente, sin configurar nada. Usa **Recargar** en la página Fuentes después de añadir ficheros.

| Fichero | Se ve como |
|---|---|
| `*.duckdb` | base adjunta en solo lectura: `nombre.main.tabla` |
| `*.parquet`, `*.csv` | vista `files.nombre` |

### Por variable de entorno

`PATOBASE_ATTACH` sigue disponible para casos avanzados: sentencias DuckDB separadas por `;` que se ejecutan al arrancar.

## Arrancar

El proyecto se gestiona con [uv](https://docs.astral.sh/uv/): crea el entorno virtual, instala la versión de Python de `.python-version` y fija las dependencias exactas en `uv.lock`.

```bash
# Instalar uv (una sola vez): https://docs.astral.sh/uv/getting-started/installation/
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone https://github.com/malbot042/patobase && cd patobase
uv sync                         # crea .venv e instala dependencias (incluidas las de desarrollo)
uv run scripts/seed.py          # datos de ejemplo: ventas, clientes y una base 'tienda'
uv run patobase                 # http://127.0.0.1:8000
```

No hace falta activar el entorno: `uv run` usa siempre `.venv` y lo sincroniza con `uv.lock` antes de ejecutar (incluidas las dependencias de desarrollo, salvo que pases `--no-dev`).

### Tareas habituales

| Para… | Comando |
|---|---|
| Otro puerto o dirección | `uv run patobase --port 9000` · `uv run patobase --host 0.0.0.0 -p 9000` |
| Desarrollar con recarga automática | `uv run patobase --reload` |
| Ver todas las opciones | `uv run patobase --help` |
| Tests | `uv run pytest` |
| Lint / formato | `uv run ruff check` · `uv run ruff format` |
| Añadir una dependencia | `uv add paquete` (o `uv add --dev paquete`) |
| Quitar una dependencia | `uv remove paquete` |
| Actualizar dependencias | `uv lock --upgrade` y después `uv sync` |
| Producción (sin dependencias de desarrollo) | `uv sync --no-dev --locked` y arrancar con `uv run --no-dev patobase` |
| Generar el paquete (wheel) | `uv build` |

`uv.lock` se versiona en Git: así todo el mundo, y el despliegue, usa exactamente las mismas versiones.

Los tests de integración contra Postgres, MySQL, S3 y HTTP reales se activan con `PATOBASE_IT=1 uv run pytest`; las conexiones están documentadas al principio de `tests/test_sources.py`.

## Configuración

| Variable | Por defecto | |
|---|---|---|
| `PATOBASE_DATA_DIR` | `data` | carpeta de fuentes |
| `PATOBASE_META_DB` | `patobase_meta.sqlite` | preguntas, dashboards y fuentes |
| `PATOBASE_ATTACH` | | sentencias DuckDB extra al arrancar |
| `PATOBASE_EXTENSION_DIR` | | carpeta de extensiones DuckDB (servidores sin Internet) |
| `PATOBASE_PAGE_SIZE` | `100` | filas por página en la tabla |
| `PATOBASE_CHART_ROWS` | `2000` | filas máximas que se envían al gráfico |
| `PATOBASE_TIMEOUT` | `30` | segundos antes de cancelar una consulta |
| `PATOBASE_HOST` / `PATOBASE_PORT` | `127.0.0.1` / `8000` | lo mismo que `--host` / `--port` (el argumento tiene prioridad) |

## Seguridad

Al arrancar, después de adjuntar las fuentes, el motor:

1. Limita la lectura de ficheros a la carpeta de datos y a las rutas remotas configuradas en Fuentes (`allowed_directories` / `allowed_paths`). Cualquier otro bucket, URL o fichero queda bloqueado.
2. Desactiva el acceso externo (`enable_external_access = false`).
3. Bloquea la configuración (`lock_configuration = true`), así no se puede revertir desde el editor.

Además solo se aceptan sentencias de lectura (`SELECT`, `EXPLAIN`, `PRAGMA`/`SHOW`/`DESCRIBE`), una por petición, y cada consulta tiene un tiempo máximo.

> No hay autenticación todavía: no lo expongas fuera de tu red.

## Estructura

```
patobase/
  config.py        variables de entorno
  engine.py        DuckDB: fuentes, sandbox, ejecución, sugerencia de gráfico
  sources.py       tipos de fuente, formularios, validación y SQL que las adjunta
  store.py         SQLite: preguntas, dashboards, tarjetas y fuentes
  main.py          rutas FastAPI (páginas completas y parciales htmx)
  templates/       Jinja2; partials/ son los fragmentos que intercambia htmx
  static/app.css   layout de la app y ajustes de densidad sobre Pico CSS
  static/app.js    CodeMirror, Vega-Lite y componentes Alpine
  static/vendor/   librerías JS incluidas (sin CDN, funciona offline)
scripts/seed.py    datos de ejemplo
tests/             pytest
pyproject.toml     dependencias y configuración (uv, ruff, pytest)
uv.lock            versiones exactas de todas las dependencias
.python-version    versión de Python que usa uv
```

Los metadatos van en SQLite y no en DuckDB porque son escrituras pequeñas y concurrentes, justo lo que DuckDB no hace bien (un solo escritor por proceso).

## Próximos pasos posibles

- Parámetros en las preguntas (`WHERE fecha >= $desde`) y filtros globales del dashboard.
- Usuarios y permisos por fuente (y cifrar las contraseñas guardadas).
- Caché de resultados con TTL.
- Constructor visual de consultas (sin SQL) con SQLGlot o Ibis.
- Reordenar tarjetas arrastrando.
- Exportar resultados a CSV / Parquet.
