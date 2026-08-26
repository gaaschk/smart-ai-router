"""MatrixStore — interface for persisting the capability matrix and provider config."""
from __future__ import annotations
from abc import ABC, abstractmethod
from smart_ai_router.models import (
    ApiKey,
    ChatMessage,
    Conversation,
    FileRecord,
    ModelSpec,
    ProviderConfig,
    UsageRecord,
)


class MatrixStore(ABC):
    @abstractmethod
    def all_models(self) -> list[ModelSpec]: ...

    @abstractmethod
    def upsert_model(self, spec: ModelSpec) -> None: ...

    @abstractmethod
    def get(self, value: str) -> ModelSpec | None: ...

    @abstractmethod
    def delete_model(self, value: str) -> bool:
        """Remove a model by value. Returns False if nothing matched."""

    # ── Provider config ───────────────────────────────────────────────────────

    @abstractmethod
    def all_providers(self) -> list[ProviderConfig]: ...

    @abstractmethod
    def get_provider(self, name: str) -> ProviderConfig | None: ...

    @abstractmethod
    def upsert_provider(self, cfg: ProviderConfig) -> None: ...

    @abstractmethod
    def delete_provider(self, name: str) -> bool: ...

    # ── Settings (UI-managed runtime config) ────────────────────────────────────

    @abstractmethod
    def get_setting(self, key: str) -> str | None:
        """Raw stored value for a setting key, or None if unset."""

    @abstractmethod
    def set_setting(self, key: str, value: str) -> None:
        """Persist a setting value (upsert)."""

    @abstractmethod
    def all_settings(self) -> dict[str, str]:
        """All persisted settings as a key→value map."""

    # ── API keys (per-user auth) ────────────────────────────────────────────────

    @abstractmethod
    def all_api_keys(self) -> list[ApiKey]: ...

    @abstractmethod
    def create_api_key(self, key: ApiKey) -> ApiKey: ...

    @abstractmethod
    def get_api_key_by_hash(self, key_hash: str) -> ApiKey | None: ...

    @abstractmethod
    def touch_api_key(self, key_hash: str) -> None:
        """Record that a key was just used (updates last_used_at)."""

    @abstractmethod
    def set_api_key_enabled(self, key_prefix: str, enabled: bool) -> bool:
        """Enable/disable a key by prefix. Returns False if no key matched."""

    @abstractmethod
    def delete_api_key(self, key_prefix: str) -> bool: ...

    @abstractmethod
    def recreate_api_key(
        self, key_prefix: str, *, new_hash: str, new_prefix: str
    ) -> ApiKey | None:
        """Rotate the secret of an existing key, keyed by its current prefix.

        Replaces key_hash + key_prefix with the new values and resets
        last_used_at, preserving user/scope/limits/enabled. Returns the updated
        record, or None if no key matched. The old secret stops working.
        """

    # ── Usage log ────────────────────────────────────────────────────────────

    @abstractmethod
    def record_usage(self, usage: UsageRecord) -> None: ...

    @abstractmethod
    def recent_usage(self, user: str, since_ts: str) -> list[UsageRecord]:
        """Usage rows for a user at/after an ISO timestamp (for quota checks)."""

    @abstractmethod
    def spend_since(self, *, user_prefix: str, since_ts: str) -> float:
        """Total $ charged to users whose name starts with `user_prefix`.

        Unlike every other aggregate here this counts **overhead rows too**: a
        classification the visitor's prompt triggered is money spent on their
        behalf, and a spend cap that ignored it would understate the bill it
        exists to cap. Prefix-matched because anonymous visitors are many users
        ("anon:<session>") sharing one budget.
        """

    @abstractmethod
    def usage_summary(
        self, *, user: str | None = None, since_ts: str = ""
    ) -> dict:
        """Aggregated usage for the dashboard (totals + group-bys).

        user=None aggregates all users and includes a by_user breakdown;
        a specific user scopes to their rows and omits by_user. since_ts
        ("" = unbounded) bounds the window.
        """

    @abstractmethod
    def usage_profiles(
        self, *, since_ts: str = "", limit: int = 200
    ) -> list[dict]:
        """Distinct prompt profiles actually routed, most frequent first.

        Each entry is {"profile": <normalize_profile-shaped dict>,
        "routed_model": str, "requests": int}. Rows with no recorded profile
        (pre-migration, or the legacy domain/complexity path) are omitted, so an
        empty list means "no profile traffic yet", not "no traffic".
        """

    # ── Files (uploads) ────────────────────────────────────────────────────────

    @abstractmethod
    def create_file(self, rec: FileRecord) -> FileRecord:
        """Persist file metadata (bytes are written to disk separately)."""

    @abstractmethod
    def get_file(self, file_id: str) -> FileRecord | None: ...

    @abstractmethod
    def list_files(self, user: str | None = None) -> list[FileRecord]:
        """All file metadata, optionally filtered to one owner."""

    @abstractmethod
    def delete_file(self, file_id: str) -> bool:
        """Delete file metadata by id. Returns False if nothing matched."""

    # ── Chat history (conversations) ─────────────────────────────────────────────

    @abstractmethod
    def create_conversation(self, conv: Conversation) -> Conversation:
        """Persist a new conversation (fills created_at/updated_at if empty)."""

    @abstractmethod
    def get_conversation(self, conversation_id: str) -> Conversation | None: ...

    @abstractmethod
    def list_conversations(
        self,
        user: str | None = None,
        *,
        tag: str | None = None,
        caller: str | None = None,
    ) -> list[Conversation]:
        """Conversations, newest-updated first, optionally filtered to one owner
        (`user`) and/or one grouping label (`tag`). Each record carries its tags.

        `caller` is the identity asking, and it gates privacy: a thread with
        shared=False is returned only to its own owner. caller=None yields shared
        threads only — the fail-safe direction, losing rows rather than leaking."""

    @abstractmethod
    def list_conversation_users(self, *, caller: str | None = None) -> list[str]:
        """Every distinct owner with at least one conversation the caller may see,
        sorted. Backs the admin's owner filter, so it lists who actually has visible
        chat history — an owner whose every thread is private is omitted, since
        appearing here would itself report that they exist."""

    @abstractmethod
    def update_conversation(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
        shared: bool | None = None,
    ) -> bool:
        """Rename, replace the tag set, and/or set admin visibility. Fields left
        None are untouched; `tags=[]` clears them. False if nothing matched."""

    @abstractmethod
    def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a conversation, its messages, and its tags. False if nothing
        matched."""

    @abstractmethod
    def add_chat_message(self, msg: ChatMessage) -> ChatMessage:
        """Append a message to a conversation (assigns ordinal + ts, bumps the
        conversation's updated_at)."""

    @abstractmethod
    def list_chat_messages(self, conversation_id: str) -> list[ChatMessage]:
        """All messages in a conversation, in send order (by ordinal)."""
