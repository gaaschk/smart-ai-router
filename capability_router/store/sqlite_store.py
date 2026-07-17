"""SqliteStore — default self-contained SQLite implementation of MatrixStore."""
from __future__ import annotations
import sqlite3
import threading
from pathlib import Path
from capability_router.models import ModelSpec
from capability_router.store.base import MatrixStore


class SqliteStore(MatrixStore):
    def __init__(self, path: str | Path = "~/.capability_router.db"):
        self._path = Path(path).expanduser()
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS models (
                    value         TEXT PRIMARY KEY,
                    provider      TEXT DEFAULT '',
                    cost          INTEGER DEFAULT 0,
                    ctx_k         INTEGER DEFAULT 0,
                    tools         INTEGER DEFAULT 0,
                    reliability   REAL DEFAULT 1.0,
                    cost_input    REAL DEFAULT 0.0,
                    cost_output   REAL DEFAULT 0.0,
                    competence_coding     REAL DEFAULT 0.0,
                    competence_docs       REAL DEFAULT 0.0,
                    competence_reasoning  REAL DEFAULT 0.0,
                    competence_general    REAL DEFAULT 0.0
                )
            """)
            self._conn.commit()

    def all_models(self) -> list[ModelSpec]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM models").fetchall()
        return [self._row_to_spec(r) for r in rows]

    def upsert_model(self, spec: ModelSpec) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO models (
                    value, provider, cost, ctx_k, tools, reliability,
                    cost_input, cost_output,
                    competence_coding, competence_docs,
                    competence_reasoning, competence_general
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(value) DO UPDATE SET
                    provider=excluded.provider,
                    cost=excluded.cost,
                    ctx_k=excluded.ctx_k,
                    tools=excluded.tools,
                    reliability=excluded.reliability,
                    cost_input=excluded.cost_input,
                    cost_output=excluded.cost_output,
                    competence_coding=excluded.competence_coding,
                    competence_docs=excluded.competence_docs,
                    competence_reasoning=excluded.competence_reasoning,
                    competence_general=excluded.competence_general
                """,
                (
                    spec.value, spec.provider, spec.cost, spec.ctx_k,
                    1 if spec.tools else 0,
                    float(max(0.0, min(1.0, spec.reliability))),
                    spec.cost_input, spec.cost_output,
                    spec.competence.get("coding", 0.0),
                    spec.competence.get("docs", 0.0),
                    spec.competence.get("reasoning", 0.0),
                    spec.competence.get("general", 0.0),
                ),
            )
            self._conn.commit()

    def get(self, value: str) -> ModelSpec | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM models WHERE value=?", (value,)
            ).fetchone()
        return self._row_to_spec(row) if row else None

    @staticmethod
    def _row_to_spec(row: sqlite3.Row) -> ModelSpec:
        return ModelSpec(
            value=row["value"],
            provider=row["provider"] or "",
            cost=row["cost"] or 0,
            ctx_k=row["ctx_k"] or 0,
            tools=bool(row["tools"]),
            reliability=row["reliability"] if row["reliability"] is not None else 1.0,
            cost_input=row["cost_input"] or 0.0,
            cost_output=row["cost_output"] or 0.0,
            competence={
                "coding":    row["competence_coding"]    or 0.0,
                "docs":      row["competence_docs"]      or 0.0,
                "reasoning": row["competence_reasoning"] or 0.0,
                "general":   row["competence_general"]   or 0.0,
            },
        )
