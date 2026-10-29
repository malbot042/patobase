"""Genera datos de ejemplo en ./data (o PATOBASE_DATA_DIR).

- data/ventas.parquet     → se ve como files.ventas
- data/clientes.csv       → se ve como files.clientes
- data/tienda.duckdb      → base adjunta 'tienda' con productos y pedidos

Uso: uv run scripts/seed.py
"""

import os
from pathlib import Path

import duckdb

data = Path(os.getenv("PATOBASE_DATA_DIR", "data"))
data.mkdir(parents=True, exist_ok=True)
con = duckdb.connect()
con.execute("SELECT setseed(0.42)")

con.execute("""
CREATE TABLE clientes AS
SELECT
    i AS cliente_id,
    'Cliente ' || i AS nombre,
    (['Barcelona','Girona','Madrid','Valencia','Sevilla','Bilbao','Zaragoza'])[1 + (i % 7)] AS ciudad,
    (['Particular','Empresa','Administración'])[1 + CAST(hash(i) % 3 AS INT)] AS segmento,
    CAST(DATE '2022-01-01' + INTERVAL (CAST(hash(i * 7) % 900 AS INT)) DAY AS DATE) AS alta
FROM range(1, 501) t(i)
""")

con.execute("""
CREATE TABLE ventas AS
SELECT
    i AS venta_id,
    CAST(DATE '2025-01-01' + INTERVAL (floor(random() * 660)) DAY AS DATE) AS fecha,
    1 + CAST(floor(random() * 500) AS INT) AS cliente_id,
    (['Electrónica','Hogar','Jardín','Libros','Deportes','Moda'])[1 + CAST(floor(random() * 6) AS INT)] AS categoria,
    (['Online','Tienda','Teléfono'])[1 + CAST(floor(pow(random(), 1.6) * 3) AS INT)] AS canal,
    1 + CAST(floor(random() * 5) AS INT) AS unidades,
    round(5 + random() * 250, 2) AS precio_unitario
FROM range(1, 20001) t(i)
""")
con.execute("ALTER TABLE ventas ADD COLUMN importe DECIMAL(12,2)")
con.execute("UPDATE ventas SET importe = unidades * precio_unitario")

con.execute(f"COPY ventas TO '{(data / 'ventas.parquet').as_posix()}' (FORMAT parquet)")
con.execute(f"COPY clientes TO '{(data / 'clientes.csv').as_posix()}' (HEADER)")

db = data / "tienda.duckdb"
db.unlink(missing_ok=True)
con.execute(f"ATTACH '{db.as_posix()}' AS tienda")
con.execute("""
CREATE TABLE tienda.productos AS
SELECT i AS producto_id, 'Producto ' || i AS nombre,
       (['Electrónica','Hogar','Jardín','Libros','Deportes','Moda'])[1 + (i % 6)] AS categoria,
       round(3 + random() * 300, 2) AS precio,
       CAST(floor(random() * 400) AS INT) AS stock
FROM range(1, 201) t(i)
""")
con.execute("""
CREATE TABLE tienda.pedidos AS
SELECT i AS pedido_id,
       1 + CAST(floor(random() * 200) AS INT) AS producto_id,
       CAST(TIMESTAMP '2026-01-01' + INTERVAL (floor(random() * 300 * 24 * 60)) MINUTE AS TIMESTAMP) AS creado,
       (['pendiente','enviado','entregado','devuelto'])[1 + CAST(floor(pow(random(), .5) * 4) AS INT)] AS estado
FROM range(1, 5001) t(i)
""")
con.execute("DETACH tienda")
print(f"Datos de ejemplo creados en {data.resolve()}")
