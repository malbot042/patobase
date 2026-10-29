"""Metadatos de la app (preguntas guardadas y dashboards) en SQLite.

Se usa SQLite y no DuckDB para los metadatos porque son escrituras pequeñas y
concurrentes, justo lo que DuckDB no hace bien (un solo escritor por proceso).
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .sources import Source

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    sql         TEXT NOT NULL,
    viz         TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS dashboards (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS dashboard_cards (
    id           INTEGER PRIMARY KEY,
    dashboard_id INTEGER NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE,
    question_id  INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL DEFAULT 0,
    width        INTEGER NOT NULL DEFAULT 1   -- 1 = media anchura, 2 = anchura completa
);
CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,
    config      TEXT NOT NULL DEFAULT '{}',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


# Columnas añadidas después de la primera versión: se crean si faltan
MIGRATIONS = {
    "questions": [
        ("kind", "TEXT NOT NULL DEFAULT 'sql'"),  # 'sql' | 'builder'
        ("spec", "TEXT NOT NULL DEFAULT ''"),  # JSON del editor visual
        ("slug", "TEXT"),  # alias estable: preguntas.<slug>
    ],
}


def slugify(text: str) -> str:
    """«Ventas por categoría (2026)» → ventas_por_categoria_2026"""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", "_", t).strip("_")[:50].rstrip("_")
    if not t:
        t = "pregunta"
    if t[0].isdigit():
        t = f"p_{t}"
    return t


@dataclass
class Question:
    id: int
    name: str
    description: str
    sql: str
    viz: dict[str, Any]
    updated_at: str
    kind: str = "sql"
    spec: dict[str, Any] = field(default_factory=dict)
    slug: str = ""

    @property
    def view(self) -> str:
        """Nombre con el que se consulta como fuente de datos."""
        return f"preguntas.{self.slug}"

    @property
    def edit_url(self) -> str:
        return f"/builder?q={self.id}" if self.kind == "builder" else f"/?q={self.id}"


@dataclass
class Dashboard:
    id: int
    name: str
    description: str


@dataclass
class Card:
    id: int
    question: Question
    position: int
    width: int


class Store:
    def __init__(self, path: Path):
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)
            for table, cols in MIGRATIONS.items():
                have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
                for col, ddl in cols:
                    if col not in have:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            for r in c.execute("SELECT id, name FROM questions WHERE slug IS NULL OR slug = ''").fetchall():
                c.execute(
                    "UPDATE questions SET slug = ? WHERE id = ?", (self._unique_slug(c, r["name"], r["id"]), r["id"])
                )
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS questions_slug ON questions(slug)")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c

    @staticmethod
    def _unique_slug(c: sqlite3.Connection, name: str, qid: int | None = None) -> str:
        base = slugify(name)
        slug, n = base, 2
        while c.execute("SELECT 1 FROM questions WHERE slug = ? AND id IS NOT ?", (slug, qid)).fetchone():
            slug, n = f"{base}_{n}", n + 1
        return slug

    @staticmethod
    def _q(r: sqlite3.Row) -> Question:
        return Question(
            r["id"],
            r["name"],
            r["description"],
            r["sql"],
            json.loads(r["viz"]),
            r["updated_at"],
            r["kind"] or "sql",
            json.loads(r["spec"]) if r["spec"] else {},
            r["slug"] or "",
        )

    # -------------------------------------------------------------- questions
    def list_questions(self) -> list[Question]:
        with self._conn() as c:
            return [self._q(r) for r in c.execute("SELECT * FROM questions ORDER BY updated_at DESC")]

    def get_question(self, qid: int) -> Question | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone()
            return self._q(r) if r else None

    def save_question(
        self,
        name: str,
        sql: str,
        viz: dict,
        description: str = "",
        qid: int | None = None,
        kind: str = "sql",
        spec: dict | None = None,
    ) -> int:
        """Guarda una pregunta. El alias (slug) se fija al crearla y no cambia al renombrarla,
        para no romper otras preguntas que la usen como fuente."""
        spec_json = json.dumps(spec) if spec else ""
        with self._conn() as c:
            if qid:
                c.execute(
                    "UPDATE questions SET name=?, description=?, sql=?, viz=?, kind=?, spec=?, "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (name, description, sql, json.dumps(viz), kind, spec_json, qid),
                )
                return qid
            cur = c.execute(
                "INSERT INTO questions (name, description, sql, viz, kind, spec, slug) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, description, sql, json.dumps(viz), kind, spec_json, self._unique_slug(c, name)),
            )
            return int(cur.lastrowid)

    def next_slug(self, name: str, qid: int | None = None) -> str:
        """El alias que tendría una pregunta nueva con este nombre (o el actual si ya existe)."""
        with self._conn() as c:
            if qid:
                r = c.execute("SELECT slug FROM questions WHERE id = ?", (qid,)).fetchone()
                if r and r["slug"]:
                    return r["slug"]
            return self._unique_slug(c, name, qid)

    def delete_question(self, qid: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM questions WHERE id = ?", (qid,))

    # ------------------------------------------------------------- dashboards
    def list_dashboards(self) -> list[Dashboard]:
        with self._conn() as c:
            return [
                Dashboard(r["id"], r["name"], r["description"])
                for r in c.execute("SELECT * FROM dashboards ORDER BY name")
            ]

    def get_dashboard(self, did: int) -> Dashboard | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM dashboards WHERE id = ?", (did,)).fetchone()
            return Dashboard(r["id"], r["name"], r["description"]) if r else None

    def create_dashboard(self, name: str, description: str = "") -> int:
        with self._conn() as c:
            return int(
                c.execute("INSERT INTO dashboards (name, description) VALUES (?, ?)", (name, description)).lastrowid
            )

    def delete_dashboard(self, did: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM dashboards WHERE id = ?", (did,))

    def cards(self, did: int) -> list[Card]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT dc.id AS card_id, dc.position, dc.width, q.*
                   FROM dashboard_cards dc JOIN questions q ON q.id = dc.question_id
                   WHERE dc.dashboard_id = ? ORDER BY dc.position, dc.id""",
                (did,),
            ).fetchall()
            return [Card(r["card_id"], self._q(r), r["position"], r["width"]) for r in rows]

    def add_card(self, did: int, qid: int, width: int = 1) -> int:
        with self._conn() as c:
            pos = c.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM dashboard_cards WHERE dashboard_id = ?", (did,)
            ).fetchone()[0]
            return int(
                c.execute(
                    "INSERT INTO dashboard_cards (dashboard_id, question_id, position, width) VALUES (?, ?, ?, ?)",
                    (did, qid, pos, width),
                ).lastrowid
            )

    def remove_card(self, card_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM dashboard_cards WHERE id = ?", (card_id,))

    def update_card(self, card_id: int, width: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE dashboard_cards SET width = ? WHERE id = ?", (width, card_id))

    # ---------------------------------------------------------------- sources
    @staticmethod
    def _src(r: sqlite3.Row) -> Source:
        return Source(r["id"], r["name"], r["kind"], json.loads(r["config"]), bool(r["enabled"]))

    def list_sources(self) -> list[Source]:
        with self._conn() as c:
            return [self._src(r) for r in c.execute("SELECT * FROM sources ORDER BY name")]

    def get_source(self, sid: int) -> Source | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM sources WHERE id = ?", (sid,)).fetchone()
            return self._src(r) if r else None

    def save_source(self, src: Source) -> int:
        with self._conn() as c:
            if src.id:
                c.execute(
                    "UPDATE sources SET name=?, kind=?, config=?, enabled=? WHERE id=?",
                    (src.name, src.kind, json.dumps(src.config), int(src.enabled), src.id),
                )
                return src.id
            cur = c.execute(
                "INSERT INTO sources (name, kind, config, enabled) VALUES (?, ?, ?, ?)",
                (src.name, src.kind, json.dumps(src.config), int(src.enabled)),
            )
            return int(cur.lastrowid)

    def set_source_enabled(self, sid: int, enabled: bool) -> None:
        with self._conn() as c:
            c.execute("UPDATE sources SET enabled = ? WHERE id = ?", (int(enabled), sid))

    def delete_source(self, sid: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM sources WHERE id = ?", (sid,))
