"""SqliteStore — default self-contained SQLite implementation of MatrixStore."""
from __future__ import annotations
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from smart_ai_router.models import ApiKey, ModelSpec, ProviderConfig, UsageRecord
from smart_ai_router.store.base import MatrixStore


def _utcnow_iso() -> str:
    """UTC timestamp in ISO-8601, used for key/usage bookkeeping."""
    return datetime.now(timezone.utc).isoformat()


class SqliteStore(MatrixStore):
    def __init__(self, path: str | Path = "~/.smart_ai_router.db"):
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
                    vision        INTEGER DEFAULT 0,
                    reliability   REAL DEFAULT 1.0,
                    cost_input    REAL DEFAULT 0.0,
                    cost_output   REAL DEFAULT 0.0,
                    competence_coding     REAL DEFAULT 0.0,
                    competence_docs       REAL DEFAULT 0.0,
                    competence_reasoning  REAL DEFAULT 0.0,
                    competence_general    REAL DEFAULT 0.0
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS providers (
                    name     TEXT PRIMARY KEY,
                    kind     TEXT NOT NULL,
                    enabled  INTEGER DEFAULT 1,
                    api_key  TEXT DEFAULT '',
                    base_url TEXT DEFAULT '',
                    timeout  INTEGER DEFAULT 15
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_hash      TEXT UNIQUE NOT NULL,
                    user          TEXT NOT NULL,
                    key_prefix    TEXT DEFAULT '',
                    enabled       INTEGER DEFAULT 1,
                    scope_models  TEXT DEFAULT '',
                    max_tier      INTEGER DEFAULT 0,
                    rl_window_s   INTEGER DEFAULT 0,
                    rl_max_req    INTEGER DEFAULT 0,
                    rl_max_tokens INTEGER DEFAULT 0,
                    created_at    TEXT DEFAULT '',
                    last_used_at  TEXT DEFAULT ''
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS usage_log (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts                TEXT DEFAULT '',
                    user              TEXT DEFAULT '',
                    key_prefix        TEXT DEFAULT '',
                    routed_model      TEXT DEFAULT '',
                    domain            TEXT DEFAULT '',
                    complexity        TEXT DEFAULT '',
                    prompt_tokens     INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    cost_usd          REAL DEFAULT 0.0,
                    status            INTEGER DEFAULT 200
                )
            """)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_user_ts ON usage_log (user, ts)"
            )
            # Additive migration: vision column added after initial release
            try:
                self._conn.execute("ALTER TABLE models ADD COLUMN vision INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # already exists
            self._conn.commit()

    def all_models(self) -> list[ModelSpec]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM models").fetchall()
        return [self._row_to_spec(r) for r in rows]

    def upsert_model(self, spec: ModelSpec) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO models (
                    value, provider, cost, ctx_k, tools, vision, reliability,
                    cost_input, cost_output,
                    competence_coding, competence_docs,
                    competence_reasoning, competence_general
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(value) DO UPDATE SET
                    provider=excluded.provider,
                    cost=excluded.cost,
                    ctx_k=excluded.ctx_k,
                    tools=excluded.tools,
                    vision=excluded.vision,
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
                    1 if spec.vision else 0,
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

    # ── Provider config ───────────────────────────────────────────────────────

    def all_providers(self) -> list[ProviderConfig]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM providers").fetchall()
        return [self._row_to_provider(r) for r in rows]

    def get_provider(self, name: str) -> ProviderConfig | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM providers WHERE name=?", (name,)
            ).fetchone()
        return self._row_to_provider(row) if row else None

    def upsert_provider(self, cfg: ProviderConfig) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO providers (name, kind, enabled, api_key, base_url, timeout)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET
                       kind=excluded.kind,
                       enabled=excluded.enabled,
                       api_key=excluded.api_key,
                       base_url=excluded.base_url,
                       timeout=excluded.timeout
                """,
                (
                    cfg.name, cfg.kind,
                    1 if cfg.enabled else 0,
                    cfg.api_key, cfg.base_url, cfg.timeout,
                ),
            )
            self._conn.commit()

    def delete_provider(self, name: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM providers WHERE name=?", (name,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ── API keys ────────────────────────────────────────────────────────────

    def all_api_keys(self) -> list[ApiKey]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM api_keys ORDER BY id"
            ).fetchall()
        return [self._row_to_api_key(r) for r in rows]

    def create_api_key(self, key: ApiKey) -> ApiKey:
        created = key.created_at or _utcnow_iso()
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO api_keys (
                    key_hash, user, key_prefix, enabled, scope_models, max_tier,
                    rl_window_s, rl_max_req, rl_max_tokens, created_at, last_used_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    key.key_hash, key.user, key.key_prefix,
                    1 if key.enabled else 0,
                    key.scope_models, key.max_tier,
                    key.rl_window_s, key.rl_max_req, key.rl_max_tokens,
                    created, key.last_used_at,
                ),
            )
            self._conn.commit()
            key.id = cur.lastrowid
        key.created_at = created
        return key

    def get_api_key_by_hash(self, key_hash: str) -> ApiKey | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM api_keys WHERE key_hash=?", (key_hash,)
            ).fetchone()
        return self._row_to_api_key(row) if row else None

    def touch_api_key(self, key_hash: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE api_keys SET last_used_at=? WHERE key_hash=?",
                (_utcnow_iso(), key_hash),
            )
            self._conn.commit()

    def set_api_key_enabled(self, key_prefix: str, enabled: bool) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE api_keys SET enabled=? WHERE key_prefix=?",
                (1 if enabled else 0, key_prefix),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def delete_api_key(self, key_prefix: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM api_keys WHERE key_prefix=?", (key_prefix,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ── Usage log ────────────────────────────────────────────────────────────

    def record_usage(self, usage: UsageRecord) -> None:
        ts = usage.ts or _utcnow_iso()
        with self._lock:
            self._conn.execute(
                """INSERT INTO usage_log (
                    ts, user, key_prefix, routed_model, domain, complexity,
                    prompt_tokens, completion_tokens, cost_usd, status
                ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    ts, usage.user, usage.key_prefix, usage.routed_model,
                    usage.domain, usage.complexity,
                    usage.prompt_tokens, usage.completion_tokens,
                    usage.cost_usd, usage.status,
                ),
            )
            self._conn.commit()

    def recent_usage(self, user: str, since_ts: str) -> list[UsageRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM usage_log WHERE user=? AND ts>=? ORDER BY ts",
                (user, since_ts),
            ).fetchall()
        return [self._row_to_usage(r) for r in rows]

    @staticmethod
    def _row_to_api_key(row: sqlite3.Row) -> ApiKey:
        return ApiKey(
            id=row["id"],
            key_hash=row["key_hash"],
            user=row["user"],
            key_prefix=row["key_prefix"] or "",
            enabled=bool(row["enabled"]),
            scope_models=row["scope_models"] or "",
            max_tier=row["max_tier"] or 0,
            rl_window_s=row["rl_window_s"] or 0,
            rl_max_req=row["rl_max_req"] or 0,
            rl_max_tokens=row["rl_max_tokens"] or 0,
            created_at=row["created_at"] or "",
            last_used_at=row["last_used_at"] or "",
        )

    @staticmethod
    def _row_to_usage(row: sqlite3.Row) -> UsageRecord:
        return UsageRecord(
            id=row["id"],
            ts=row["ts"] or "",
            user=row["user"] or "",
            key_prefix=row["key_prefix"] or "",
            routed_model=row["routed_model"] or "",
            domain=row["domain"] or "",
            complexity=row["complexity"] or "",
            prompt_tokens=row["prompt_tokens"] or 0,
            completion_tokens=row["completion_tokens"] or 0,
            cost_usd=row["cost_usd"] or 0.0,
            status=row["status"] or 200,
        )

    @staticmethod
    def _row_to_provider(row: sqlite3.Row) -> ProviderConfig:
        return ProviderConfig(
            name=row["name"],
            kind=row["kind"],
            enabled=bool(row["enabled"]),
            api_key=row["api_key"] or "",
            base_url=row["base_url"] or "",
            timeout=row["timeout"] or 15,
        )

    @staticmethod
    def _row_to_spec(row: sqlite3.Row) -> ModelSpec:
        return ModelSpec(
            value=row["value"],
            provider=row["provider"] or "",
            cost=row["cost"] or 0,
            ctx_k=row["ctx_k"] or 0,
            tools=bool(row["tools"]),
            vision=bool(row["vision"]) if row["vision"] is not None else False,
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
