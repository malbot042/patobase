"""Metadatos de la app (preguntas guardadas y dashboards) en SQLite.

Se usa SQLite y no DuckDB para los metadatos porque son escrituras pequeñas y
concurrentes, justo lo que DuckDB no hace bien (un solo escritor por proceso).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
"""


@dataclass
class Question:
    id: int
    name: str
    description: str
    sql: str
    viz: dict[str, Any]
    updated_at: str


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

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c

    @staticmethod
    def _q(r: sqlite3.Row) -> Question:
        return Question(r["id"], r["name"], r["description"], r["sql"], json.loads(r["viz"]), r["updated_at"])

    # -------------------------------------------------------------- questions
    def list_questions(self) -> list[Question]:
        with self._conn() as c:
            return [self._q(r) for r in c.execute("SELECT * FROM questions ORDER BY updated_at DESC")]

    def get_question(self, qid: int) -> Question | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone()
            return self._q(r) if r else None

    def save_question(self, name: str, sql: str, viz: dict, description: str = "", qid: int | None = None) -> int:
        with self._conn() as c:
            if qid:
                c.execute(
                    "UPDATE questions SET name=?, description=?, sql=?, viz=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (name, description, sql, json.dumps(viz), qid),
                )
                return qid
            cur = c.execute(
                "INSERT INTO questions (name, description, sql, viz) VALUES (?, ?, ?, ?)",
                (name, description, sql, json.dumps(viz)),
            )
            return int(cur.lastrowid)

    def delete_question(self, qid: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM questions WHERE id = ?", (qid,))

    # ------------------------------------------------------------- dashboards
    def list_dashboards(self) -> list[Dashboard]:
        with self._conn() as c:
            return [Dashboard(r["id"], r["name"], r["description"]) for r in c.execute("SELECT * FROM dashboards ORDER BY name")]

    def get_dashboard(self, did: int) -> Dashboard | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM dashboards WHERE id = ?", (did,)).fetchone()
            return Dashboard(r["id"], r["name"], r["description"]) if r else None

    def create_dashboard(self, name: str, description: str = "") -> int:
        with self._conn() as c:
            return int(c.execute("INSERT INTO dashboards (name, description) VALUES (?, ?)", (name, description)).lastrowid)

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
            pos = c.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM dashboard_cards WHERE dashboard_id = ?", (did,)).fetchone()[0]
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
