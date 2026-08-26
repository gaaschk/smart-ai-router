"""SqliteStore — default self-contained SQLite implementation of MatrixStore."""
from __future__ import annotations
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from smart_ai_router.models import (
    ApiKey,
    ChatMessage,
    Conversation,
    FileRecord,
    ModelSpec,
    ProviderConfig,
    UsageRecord,
)
from smart_ai_router.profiler import apply_ratings, baseline_profile
from smart_ai_router.store.base import MatrixStore


def _utcnow_iso() -> str:
    """UTC timestamp in ISO-8601, used for key/usage bookkeeping."""
    return datetime.now(timezone.utc).isoformat()


# Local alias: `baseline_profile` reads better at its definition than at the two
# call sites here, where the point is just "write the deterministic one".
_baseline = baseline_profile

# The one UsageRecord.kind that means "a user asked for this". Everything else is
# the router spending on its own behalf (classification, model profiling), and the
# SQL below uses COALESCE(kind, ...) rather than a bare comparison so a row
# somehow written without a kind still counts as user traffic — which is what it
# would have been before the column existed.
_PROXY_KIND = "proxy"
_IS_PROXY = f"COALESCE(kind, '{_PROXY_KIND}') = '{_PROXY_KIND}'"
_IS_OVERHEAD = f"COALESCE(kind, '{_PROXY_KIND}') != '{_PROXY_KIND}'"


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
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    id             TEXT PRIMARY KEY,
                    user           TEXT DEFAULT '',
                    filename       TEXT DEFAULT '',
                    purpose        TEXT DEFAULT 'assistants',
                    mime           TEXT DEFAULT 'application/octet-stream',
                    bytes          INTEGER DEFAULT 0,
                    path           TEXT DEFAULT '',
                    extracted_text TEXT DEFAULT '',
                    created_at     TEXT DEFAULT ''
                )
            """)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_files_user ON files (user)"
            )
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id         TEXT PRIMARY KEY,
                    user       TEXT DEFAULT '',
                    title      TEXT DEFAULT 'New chat',
                    created_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT '',
                    shared     INTEGER DEFAULT 1
                )
            """)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_user "
                "ON conversations (user, updated_at)"
            )
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    ordinal         INTEGER DEFAULT 0,
                    role            TEXT DEFAULT 'user',
                    content         TEXT DEFAULT '',
                    content_json    INTEGER DEFAULT 0,
                    ts              TEXT DEFAULT ''
                )
            """)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_messages_conv "
                "ON chat_messages (conversation_id, ordinal)"
            )
            # A conversation's grouping labels. Many-to-many on purpose: a thread
            # can sit in more than one group, so this is a join table rather than
            # a column on conversations.
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS conversation_tags (
                    conversation_id TEXT NOT NULL,
                    tag             TEXT NOT NULL,
                    PRIMARY KEY (conversation_id, tag)
                )
            """)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversation_tags_tag "
                "ON conversation_tags (tag)"
            )
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT DEFAULT ''
                )
            """)
            # Additive migration: vision column added after initial release
            try:
                self._conn.execute("ALTER TABLE models ADD COLUMN vision INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # already exists
            # Additive migration: flag rows whose tokens were estimated locally
            # (streamed responses without a provider usage block) vs measured.
            try:
                self._conn.execute(
                    "ALTER TABLE usage_log ADD COLUMN tokens_estimated INTEGER DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass  # already exists
            # Additive migration: per-field capability profile + the provider
            # description it was derived from. Stored as JSON rather than one
            # column per taxonomy field so the taxonomy can grow without a
            # schema change; an empty string means "not profiled yet", and the
            # router falls back to the four competence columns for those rows
            # until the next sync fills them in.
            #
            # profile_ratings_json / profile_note hold the optional LLM shape
            # judgment. Deliberately separate from profile_json rather than baked
            # into it: profile_json stays the deterministic sync output, so a
            # re-sync with fresh benchmarks re-levels the profile and the ratings
            # re-apply on read with no second LLM call.
            #
            # `agentic` is measured loop stamina, on its own column rather than in
            # profile_json because it is not a taxonomy field. DEFAULT 0.0 reads as
            # "never measured" everywhere, which is what a pre-migration row
            # honestly is — and what the router treats as exempt, not incapable.
            #
            # structured_outputs / reasoning are capability flags in the same family
            # as tools and vision, and get columns for the same reason: the router
            # filters on them. DEFAULT 0 makes a pre-migration row read as "does not
            # support schema-constrained replies", which is the safe direction — it
            # keeps that row out of helper duty until the next sync says otherwise,
            # rather than sending it a schema it would silently ignore.
            for column, decl in (
                ("profile_json", "TEXT DEFAULT ''"),
                ("description", "TEXT DEFAULT ''"),
                ("profile_ratings_json", "TEXT DEFAULT ''"),
                ("profile_note", "TEXT DEFAULT ''"),
                ("agentic", "REAL DEFAULT 0.0"),
                ("structured_outputs", "INTEGER DEFAULT 0"),
                ("reasoning", "INTEGER DEFAULT 0"),
                # Output ceiling the *model* imposes (ModelSpec.max_output).
                # DEFAULT 0 reads as "unknown", which is what a pre-migration row
                # honestly is, and is the safe direction: unknown means we send
                # the configured budget rather than a number derived from a guess
                # at the model's limit.
                ("max_output", "INTEGER DEFAULT 0"),
            ):
                try:
                    self._conn.execute(
                        f"ALTER TABLE models ADD COLUMN {column} {decl}"
                    )
                except sqlite3.OperationalError:
                    pass  # already exists
            # Additive migration: the full prompt profile behind each routing
            # decision. domain/complexity are a lossy summary; this is what the
            # router actually matched on, and it is what makes a profiler change
            # auditable against real traffic instead of synthetic prompts.
            try:
                self._conn.execute(
                    "ALTER TABLE usage_log ADD COLUMN profile_json TEXT DEFAULT ''"
                )
            except sqlite3.OperationalError:
                pass  # already exists
            # Additive migration: what kind of call this row is (UsageRecord.kind).
            # The router bills for calls nobody requested — prompt classification,
            # the refine pass, model profiling — and those used to go unrecorded, so
            # the usage page understated the real spend. They are logged as rows of
            # their own now, and this column is what keeps them out of the user-traffic
            # aggregates. DEFAULT 'proxy' is correct for every pre-existing row: before
            # this column existed, a usage row could only be a proxied request.
            try:
                self._conn.execute(
                    "ALTER TABLE usage_log ADD COLUMN kind TEXT DEFAULT 'proxy'"
                )
            except sqlite3.OperationalError:
                pass  # already exists
            # Additive migration: which classifier produced the profile that routed
            # this request (UsageRecord.classifier). Every failure in the classifier
            # chain degrades silently to a cheaper judgment and still answers the
            # request, so a broken pin looks exactly like a working one from the
            # outside. '' for rows written before the column, which is honest —
            # those rows genuinely don't know.
            try:
                self._conn.execute(
                    "ALTER TABLE usage_log ADD COLUMN classifier TEXT DEFAULT ''"
                )
            except sqlite3.OperationalError:
                pass  # already exists
            # Additive migration: whether the admin identity may see this thread.
            # DEFAULT 1 backfills every existing chat as shared, which is both the
            # product default and the only honest read of history written before the
            # choice existed — nobody who created those threads asked for privacy, so
            # silently hiding them would misreport what the admin used to be able to
            # see rather than protect anything.
            try:
                self._conn.execute(
                    "ALTER TABLE conversations ADD COLUMN shared INTEGER DEFAULT 1"
                )
            except sqlite3.OperationalError:
                pass  # already exists
            # Additive migration: this reply was cut off at the output ceiling.
            # DEFAULT 0 backfills history as untruncated, which is a claim we
            # can't verify — the flag wasn't recorded, so some old replies really
            # were cut off and will keep looking merely terse. Guessing from the
            # text would be worse: a reply that legitimately ends without
            # punctuation would get a warning it doesn't deserve, and a warning
            # that is sometimes wrong is worth less than no warning at all.
            try:
                self._conn.execute(
                    "ALTER TABLE chat_messages ADD COLUMN truncated INTEGER DEFAULT 0"
                )
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
                    competence_reasoning, competence_general,
                    profile_json, description,
                    profile_ratings_json, profile_note, agentic,
                    structured_outputs, reasoning, max_output
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    competence_general=excluded.competence_general,
                    profile_json=excluded.profile_json,
                    description=excluded.description,
                    profile_ratings_json=excluded.profile_ratings_json,
                    profile_note=excluded.profile_note,
                    agentic=excluded.agentic,
                    structured_outputs=excluded.structured_outputs,
                    reasoning=excluded.reasoning,
                    max_output=excluded.max_output
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
                    # The *baseline* goes in profile_json, never the composed
                    # profile. `spec.profile` came back from a read with ratings
                    # already applied, so writing it would re-apply them on the
                    # next read and drift the scores further every round trip.
                    json.dumps(_baseline(spec), sort_keys=True) if _baseline(spec) else "",
                    spec.description,
                    json.dumps(spec.profile_ratings, sort_keys=True)
                    if spec.profile_ratings else "",
                    spec.profile_note,
                    float(max(0.0, min(1.0, spec.agentic))),
                    1 if spec.structured_outputs else 0,
                    1 if spec.reasoning else 0,
                    max(0, int(spec.max_output or 0)),
                ),
            )
            self._conn.commit()

    def get(self, value: str) -> ModelSpec | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM models WHERE value=?", (value,)
            ).fetchone()
        return self._row_to_spec(row) if row else None

    def delete_model(self, value: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM models WHERE value=?", (value,))
            self._conn.commit()
        return cur.rowcount > 0

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

    # ── Settings (UI-managed runtime config) ────────────────────────────────────

    def get_setting(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO settings (key, value) VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value),
            )
            self._conn.commit()

    def all_settings(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

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

    def recreate_api_key(
        self, key_prefix: str, *, new_hash: str, new_prefix: str
    ) -> ApiKey | None:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE api_keys SET key_hash=?, key_prefix=?, last_used_at=? "
                "WHERE key_prefix=?",
                (new_hash, new_prefix, "", key_prefix),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            row = self._conn.execute(
                "SELECT * FROM api_keys WHERE key_hash=?", (new_hash,)
            ).fetchone()
        return self._row_to_api_key(row) if row else None

    # ── Usage log ────────────────────────────────────────────────────────────

    def record_usage(self, usage: UsageRecord) -> None:
        ts = usage.ts or _utcnow_iso()
        with self._lock:
            self._conn.execute(
                """INSERT INTO usage_log (
                    ts, kind, user, key_prefix, routed_model, domain, complexity,
                    prompt_tokens, completion_tokens, cost_usd, status,
                    tokens_estimated, profile_json, classifier
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ts, usage.kind or _PROXY_KIND,
                    usage.user, usage.key_prefix, usage.routed_model,
                    usage.domain, usage.complexity,
                    usage.prompt_tokens, usage.completion_tokens,
                    usage.cost_usd, usage.status,
                    1 if usage.tokens_estimated else 0,
                    json.dumps(usage.profile, sort_keys=True)
                    if usage.profile else "",
                    usage.classifier,
                ),
            )
            self._conn.commit()

    def recent_usage(self, user: str, since_ts: str) -> list[UsageRecord]:
        """A user's recent *requests* — the rate limiter's counter.

        Overhead rows are excluded on purpose. A key that sent one prompt made one
        request, even though the router also classified it and maybe re-profiled
        it; counting those against the key's own quota would spend its allowance on
        work it never asked for, and would shrink the allowance whenever an
        operator changed the classifier configuration.
        """
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM usage_log WHERE user=? AND ts>=? AND {_IS_PROXY} "
                "ORDER BY ts",
                (user, since_ts),
            ).fetchall()
        return [self._row_to_usage(r) for r in rows]

    def spend_since(self, *, user_prefix: str, since_ts: str) -> float:
        """Sum cost_usd for a family of users, counting overhead rows.

        See MatrixStore.spend_since for why overhead is included. `user_prefix`
        is matched with LIKE, so it is escaped: an unescaped '_' is a single-char
        wildcard in SQL and 'anon:' contains none today, but a caller passing a
        prefix with '_' would silently widen the budget to other users.
        """
        if not user_prefix:
            return 0.0
        escaped = (
            user_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS spend FROM usage_log "
                "WHERE user LIKE ? ESCAPE '\\' AND ts>=?",
                (f"{escaped}%", since_ts),
            ).fetchone()
        return float(row["spend"] or 0.0)

    def spend_for_user(self, *, user: str, since_ts: str) -> float:
        """Sum cost_usd for one exact user, counting overhead rows.

        Equality rather than LIKE: see MatrixStore.spend_for_user for why a
        per-account cap can't be a prefix match.
        """
        if not user:
            return 0.0
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS spend FROM usage_log "
                "WHERE user=? AND ts>=?",
                (user, since_ts),
            ).fetchone()
        return float(row["spend"] or 0.0)

    def usage_profiles(
        self, *, since_ts: str = "", limit: int = 200
    ) -> list[dict]:
        """Distinct prompt profiles this deployment has actually routed.

        Grouped by (profile, chosen model) with a count, so the profile audit
        replays *real* traffic rather than prompts someone invented to make a
        change look good — and weights each distinct profile by how often it
        shows up. Rows predating the profile column, and legacy
        (domain, complexity) routes, have no profile and are skipped.

        Ordered by count so a `limit` keeps the profiles that matter most.
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT profile_json, routed_model, COUNT(*) AS requests
                   FROM usage_log
                   WHERE ts >= ? AND profile_json != ''
                   GROUP BY profile_json, routed_model
                   ORDER BY requests DESC, profile_json
                   LIMIT ?""",
                (since_ts or "", max(1, limit)),
            ).fetchall()

        out: list[dict] = []
        for row in rows:
            try:
                decoded = json.loads(row["profile_json"])
            except (ValueError, TypeError):
                continue
            if not isinstance(decoded, dict):
                continue
            out.append({
                "profile": decoded,
                "routed_model": row["routed_model"] or "",
                "requests": row["requests"] or 0,
            })
        return out

    def usage_summary(
        self, *, user: str | None = None, since_ts: str = ""
    ) -> dict:
        """Aggregate usage_log for the dashboard, computed in SQL (GROUP BY).

        user=None → all users (admin view, includes a by_user breakdown);
        a value → only that user's rows, and by_user is omitted. since_ts
        (ISO-8601) bounds the window; "" means no lower bound. Cost/token sums
        coalesce NULLs to 0 so an empty window returns zeroed totals, not None.

        Every aggregate here covers **user traffic only** (kind='proxy'), so the
        headline numbers keep meaning "what our users did" as they always have.
        The router's own calls — prompt classification, the refine pass, model
        profiling — are real money and are summarized separately under
        "overhead", broken out by kind and by model.
        """
        # WHERE clause shared by every aggregate query below.
        where = "WHERE ts >= ?"
        params: list = [since_ts or ""]
        if user is not None:
            where += " AND user = ?"
            params.append(user)

        def _agg(
            select: str, group_by: str = "", *, overhead: bool = False
        ) -> list[sqlite3.Row]:
            kind = _IS_OVERHEAD if overhead else _IS_PROXY
            sql = f"SELECT {select} FROM usage_log {where} AND {kind}"
            if group_by:
                sql += f" {group_by}"
            return self._conn.execute(sql, params).fetchall()

        _sums = (
            "COUNT(*) AS requests, "
            "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
            "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
            "COALESCE(SUM(cost_usd), 0.0) AS cost_usd, "
            "COALESCE(SUM(tokens_estimated), 0) AS estimated_rows"
        )

        with self._lock:
            total = _agg(_sums)[0]
            by_model = _agg(
                f"routed_model AS key, {_sums}",
                "GROUP BY routed_model ORDER BY cost_usd DESC, requests DESC",
            )
            by_day = _agg(
                f"substr(ts, 1, 10) AS key, {_sums}",
                "GROUP BY substr(ts, 1, 10) ORDER BY key",
            )
            by_domain = _agg(
                f"(domain || '/' || complexity) AS key, {_sums}",
                "GROUP BY key ORDER BY requests DESC",
            )
            # Which classifier decided each request. Ordered by request count
            # rather than cost because these rows all cost the same to route and
            # what matters is the *mix*: a deployment expecting `llm` and seeing
            # mostly `keyword` is silently routing on the fallback profiler.
            by_classifier = _agg(
                f"classifier AS key, {_sums}",
                "GROUP BY classifier ORDER BY requests DESC",
            )
            by_user = (
                _agg(
                    f"user AS key, {_sums}",
                    "GROUP BY user ORDER BY cost_usd DESC, requests DESC",
                )
                if user is None
                else None
            )
            oh_total = _agg(_sums, overhead=True)[0]
            oh_by_kind = _agg(
                f"kind AS key, {_sums}",
                "GROUP BY kind ORDER BY cost_usd DESC, requests DESC",
                overhead=True,
            )
            oh_by_model = _agg(
                f"routed_model AS key, {_sums}",
                "GROUP BY routed_model ORDER BY cost_usd DESC, requests DESC",
                overhead=True,
            )

        def _row(r: sqlite3.Row) -> dict:
            return {
                "requests": r["requests"] or 0,
                "prompt_tokens": r["prompt_tokens"] or 0,
                "completion_tokens": r["completion_tokens"] or 0,
                "cost_usd": r["cost_usd"] or 0.0,
                "estimated_rows": r["estimated_rows"] or 0,
            }

        def _keyed(rows: list[sqlite3.Row]) -> list[dict]:
            return [{"key": r["key"] or "", **_row(r)} for r in rows]

        result = {
            "totals": _row(total),
            "by_model": _keyed(by_model),
            "by_day": _keyed(by_day),
            "by_domain": _keyed(by_domain),
            "by_classifier": _keyed(by_classifier),
            "overhead": {
                "totals": _row(oh_total),
                "by_kind": _keyed(oh_by_kind),
                "by_model": _keyed(oh_by_model),
            },
        }
        if by_user is not None:
            result["by_user"] = _keyed(by_user)
        return result

    # ── Files ──────────────────────────────────────────────────────────────────

    def create_file(self, rec: FileRecord) -> FileRecord:
        if not rec.created_at:
            rec.created_at = _utcnow_iso()
        with self._lock:
            self._conn.execute(
                """INSERT INTO files
                   (id, user, filename, purpose, mime, bytes, path, extracted_text, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (rec.id, rec.user, rec.filename, rec.purpose, rec.mime, rec.bytes,
                 rec.path, rec.extracted_text, rec.created_at),
            )
            self._conn.commit()
        return rec

    def get_file(self, file_id: str) -> FileRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM files WHERE id=?", (file_id,)
            ).fetchone()
        return self._row_to_file(row) if row else None

    def list_files(self, user: str | None = None) -> list[FileRecord]:
        with self._lock:
            if user is None:
                rows = self._conn.execute(
                    "SELECT * FROM files ORDER BY created_at"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM files WHERE user=? ORDER BY created_at", (user,)
                ).fetchall()
        return [self._row_to_file(r) for r in rows]

    def delete_file(self, file_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM files WHERE id=?", (file_id,))
            self._conn.commit()
        return cur.rowcount > 0

    # ── Chat history ─────────────────────────────────────────────────────────────

    def create_conversation(self, conv: Conversation) -> Conversation:
        now = _utcnow_iso()
        if not conv.created_at:
            conv.created_at = now
        if not conv.updated_at:
            conv.updated_at = conv.created_at
        with self._lock:
            self._conn.execute(
                """INSERT INTO conversations
                       (id, user, title, created_at, updated_at, shared)
                   VALUES (?,?,?,?,?,?)""",
                (conv.id, conv.user, conv.title, conv.created_at, conv.updated_at,
                 int(conv.shared)),
            )
            if conv.tags:
                self._write_tags_locked(conv.id, conv.tags)
            self._conn.commit()
        return conv

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if row is None:
                return None
            conv = self._row_to_conversation(row)
            conv.tags = self._tags_locked([conv.id]).get(conv.id, [])
        return conv

    def list_conversations(
        self,
        user: str | None = None,
        *,
        tag: str | None = None,
        caller: str | None = None,
    ) -> list[Conversation]:
        """Conversations for a scope, newest first.

        `caller` is the identity doing the asking, and it governs privacy: a thread
        with shared=0 is returned only to its own owner. Passing caller=None means
        "shared threads only", which is the fail-safe direction — a caller that
        forgets to identify itself loses rows rather than leaking them.
        """
        with self._lock:
            where, params = [], []
            if user is not None:
                where.append("user=?")
                params.append(user)
            if caller is None:
                where.append("shared=1")
            else:
                where.append("(shared=1 OR user=?)")
                params.append(caller)
            if tag:
                where.append(
                    "id IN (SELECT conversation_id FROM conversation_tags WHERE tag=?)"
                )
                params.append(tag)
            sql = "SELECT * FROM conversations"
            if where:
                sql += " WHERE " + " AND ".join(where)
            rows = self._conn.execute(sql + " ORDER BY updated_at DESC", params).fetchall()
            convs = [self._row_to_conversation(r) for r in rows]
            # One batched lookup for the whole page rather than a query per row.
            tags = self._tags_locked([c.id for c in convs])
            for c in convs:
                c.tags = tags.get(c.id, [])
        return convs

    def list_conversation_users(self, *, caller: str | None = None) -> list[str]:
        """Owners with at least one conversation the caller may see.

        Private threads don't count: an owner whose every thread is private must not
        surface here, or the picker itself would report that they exist.
        """
        with self._lock:
            sql = "SELECT DISTINCT user FROM conversations WHERE "
            if caller is None:
                rows = self._conn.execute(sql + "shared=1 ORDER BY user").fetchall()
            else:
                rows = self._conn.execute(
                    sql + "(shared=1 OR user=?) ORDER BY user", (caller,)
                ).fetchall()
        return [r["user"] or "" for r in rows]

    def update_conversation(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
        shared: bool | None = None,
    ) -> bool:
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if exists is None:
                return False
            if title is not None:
                self._conn.execute(
                    "UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                    (title, _utcnow_iso(), conversation_id),
                )
            if shared is not None:
                # Like retagging, changing who can see a thread is not activity on
                # it, so `updated_at` stays put.
                self._conn.execute(
                    "UPDATE conversations SET shared=? WHERE id=?",
                    (int(shared), conversation_id),
                )
            if tags is not None:
                # Filing a thread under a label isn't activity, so `updated_at`
                # stays put — retagging must not reshuffle the sidebar's order.
                self._conn.execute(
                    "DELETE FROM conversation_tags WHERE conversation_id=?",
                    (conversation_id,),
                )
                self._write_tags_locked(conversation_id, tags)
            self._conn.commit()
        return True

    def reassign_conversations(self, *, from_user: str, to_user: str) -> int:
        """Hand one owner's threads to another. Messages and tags follow for free —
        both hang off the conversation id, which does not change."""
        if not from_user or not to_user or from_user == to_user:
            return 0
        with self._lock:
            cur = self._conn.execute(
                "UPDATE conversations SET user=? WHERE user=?", (to_user, from_user)
            )
            self._conn.commit()
        return cur.rowcount or 0

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._lock:
            self._conn.execute(
                "DELETE FROM chat_messages WHERE conversation_id=?", (conversation_id,)
            )
            self._conn.execute(
                "DELETE FROM conversation_tags WHERE conversation_id=?", (conversation_id,)
            )
            cur = self._conn.execute(
                "DELETE FROM conversations WHERE id=?", (conversation_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    # Both tag helpers assume self._lock is already held (it is a plain Lock, so
    # re-acquiring it from a nested call would deadlock).

    def _write_tags_locked(self, conversation_id: str, tags: list[str]) -> None:
        self._conn.executemany(
            "INSERT OR IGNORE INTO conversation_tags (conversation_id, tag) VALUES (?,?)",
            [(conversation_id, t) for t in tags],
        )

    def _tags_locked(self, ids: list[str]) -> dict[str, list[str]]:
        """Tags for many conversations at once, keyed by conversation id."""
        out: dict[str, list[str]] = {}
        # Chunked to stay well under SQLite's bound-parameter limit (999 on the
        # oldest builds we might run against).
        for start in range(0, len(ids), 500):
            batch = ids[start:start + 500]
            marks = ",".join("?" * len(batch))
            rows = self._conn.execute(
                "SELECT conversation_id, tag FROM conversation_tags "
                f"WHERE conversation_id IN ({marks}) ORDER BY tag",
                batch,
            ).fetchall()
            for r in rows:
                out.setdefault(r["conversation_id"], []).append(r["tag"])
        return out

    def add_chat_message(self, msg: ChatMessage) -> ChatMessage:
        ts = msg.ts or _utcnow_iso()
        with self._lock:
            # Next ordinal = current max + 1 (0 for the first message).
            row = self._conn.execute(
                "SELECT COALESCE(MAX(ordinal), -1) + 1 AS n FROM chat_messages "
                "WHERE conversation_id=?",
                (msg.conversation_id,),
            ).fetchone()
            ordinal = row["n"] if msg.ordinal == 0 else msg.ordinal
            cur = self._conn.execute(
                """INSERT INTO chat_messages
                   (conversation_id, ordinal, role, content, content_json, ts,
                    truncated)
                   VALUES (?,?,?,?,?,?,?)""",
                (msg.conversation_id, ordinal, msg.role, msg.content,
                 1 if msg.content_json else 0, ts, 1 if msg.truncated else 0),
            )
            # Appending a message bumps the conversation's recency.
            self._conn.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?",
                (ts, msg.conversation_id),
            )
            self._conn.commit()
            msg.id = cur.lastrowid
        msg.ordinal = ordinal
        msg.ts = ts
        return msg

    def list_chat_messages(self, conversation_id: str) -> list[ChatMessage]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM chat_messages WHERE conversation_id=? ORDER BY ordinal",
                (conversation_id,),
            ).fetchall()
        return [self._row_to_chat_message(r) for r in rows]

    @staticmethod
    def _row_to_conversation(row: sqlite3.Row) -> Conversation:
        return Conversation(
            id=row["id"],
            user=row["user"] or "",
            title=row["title"] or "New chat",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            shared=bool(row["shared"]),
        )

    @staticmethod
    def _row_to_chat_message(row: sqlite3.Row) -> ChatMessage:
        return ChatMessage(
            id=row["id"],
            conversation_id=row["conversation_id"],
            ordinal=row["ordinal"] or 0,
            role=row["role"] or "user",
            content=row["content"] or "",
            content_json=bool(row["content_json"]),
            ts=row["ts"] or "",
            truncated=bool(row["truncated"]),
        )

    @staticmethod
    def _row_to_file(row: sqlite3.Row) -> FileRecord:
        return FileRecord(
            id=row["id"],
            user=row["user"] or "",
            filename=row["filename"] or "",
            purpose=row["purpose"] or "assistants",
            mime=row["mime"] or "application/octet-stream",
            bytes=row["bytes"] or 0,
            path=row["path"] or "",
            extracted_text=row["extracted_text"] or "",
            created_at=row["created_at"] or "",
        )

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

    @classmethod
    def _row_to_usage(cls, row: sqlite3.Row) -> UsageRecord:
        return UsageRecord(
            id=row["id"],
            ts=row["ts"] or "",
            kind=(row["kind"] or _PROXY_KIND)
            if "kind" in row.keys() else _PROXY_KIND,
            user=row["user"] or "",
            key_prefix=row["key_prefix"] or "",
            routed_model=row["routed_model"] or "",
            domain=row["domain"] or "",
            complexity=row["complexity"] or "",
            prompt_tokens=row["prompt_tokens"] or 0,
            completion_tokens=row["completion_tokens"] or 0,
            cost_usd=row["cost_usd"] or 0.0,
            status=row["status"] or 200,
            tokens_estimated=bool(
                row["tokens_estimated"]
                if "tokens_estimated" in row.keys() else 0
            ),
            profile=cls._json_column(row, "profile_json"),
            classifier=(row["classifier"] or "")
            if "classifier" in row.keys() else "",
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
    def _profile_from_row(row: sqlite3.Row) -> dict[str, float]:
        """Decode profile_json, tolerating rows written before it existed and
        any hand-edited value. A bad blob degrades to "not profiled" (so the
        router uses the legacy competence columns) rather than failing every
        read of the model matrix."""
        try:
            raw = row["profile_json"]
        except (IndexError, KeyError):
            return {}
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        if not isinstance(decoded, dict):
            return {}
        out: dict[str, float] = {}
        for key, val in decoded.items():
            try:
                out[str(key)] = float(val)
            except (TypeError, ValueError):
                continue
        return out

    @staticmethod
    def _ratings_from_row(row: sqlite3.Row) -> dict[str, str]:
        """Decode profile_ratings_json. Same tolerance as _profile_from_row: a
        bad blob means "not rated", which routes on the deterministic profile
        rather than breaking every read."""
        try:
            raw = row["profile_ratings_json"]
        except (IndexError, KeyError):
            return {}
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        if not isinstance(decoded, dict):
            return {}
        return {str(k): str(v) for k, v in decoded.items()}

    @staticmethod
    def _json_column(row: sqlite3.Row, name: str) -> dict | None:
        """Decode an optional JSON-object column, or None if absent/unusable."""
        try:
            raw = row[name]
        except (IndexError, KeyError):
            return None
        if not raw:
            return None
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return decoded if isinstance(decoded, dict) else None

    @staticmethod
    def _column(row: sqlite3.Row, name: str, default: str = "") -> str:
        """Read a column that may predate its migration."""
        try:
            return row[name] or default
        except (IndexError, KeyError):
            return default

    @staticmethod
    def _num_column(row: sqlite3.Row, name: str, default: float = 0.0) -> float:
        """_column for a numeric column: NULL and absent both mean the default."""
        try:
            val = row[name]
        except (IndexError, KeyError):
            return default
        return default if val is None else float(val)

    @staticmethod
    def _bool_column(row: sqlite3.Row, name: str, default: bool = False) -> bool:
        """_column for a flag column: NULL and absent both mean the default."""
        try:
            val = row[name]
        except (IndexError, KeyError):
            return default
        return default if val is None else bool(val)

    @classmethod
    def _row_to_spec(cls, row: sqlite3.Row) -> ModelSpec:
        # Compose the LLM shape onto the stored baseline here, once per read,
        # so `spec.profile` is always the profile the router should match on and
        # neither router.py nor any of its callers has to know enrichment is a
        # feature. `profile_rules` is set only when the overlay changed something,
        # which is the invariant profiler.baseline_profile() relies on.
        rules = cls._profile_from_row(row)
        ratings = cls._ratings_from_row(row)
        effective = apply_ratings(rules, ratings)
        return ModelSpec(
            value=row["value"],
            provider=row["provider"] or "",
            cost=row["cost"] or 0,
            ctx_k=row["ctx_k"] or 0,
            max_output=int(cls._num_column(row, "max_output")),
            tools=bool(row["tools"]),
            vision=bool(row["vision"]) if row["vision"] is not None else False,
            reliability=row["reliability"] if row["reliability"] is not None else 1.0,
            cost_input=row["cost_input"] or 0.0,
            cost_output=row["cost_output"] or 0.0,
            agentic=cls._num_column(row, "agentic"),
            structured_outputs=cls._bool_column(row, "structured_outputs"),
            reasoning=cls._bool_column(row, "reasoning"),
            competence={
                "coding":    row["competence_coding"]    or 0.0,
                "docs":      row["competence_docs"]      or 0.0,
                "reasoning": row["competence_reasoning"] or 0.0,
                "general":   row["competence_general"]   or 0.0,
            },
            profile=effective,
            description=cls._column(row, "description"),
            profile_rules=rules if effective != rules else {},
            profile_ratings=ratings,
            profile_note=cls._column(row, "profile_note"),
        )
