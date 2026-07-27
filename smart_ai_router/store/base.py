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
    def usage_summary(
        self, *, user: str | None = None, since_ts: str = ""
    ) -> dict:
        """Aggregated usage for the dashboard (totals + group-bys).

        user=None aggregates all users and includes a by_user breakdown;
        a specific user scopes to their rows and omits by_user. since_ts
        ("" = unbounded) bounds the window.
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
    def list_conversations(self, user: str | None = None) -> list[Conversation]:
        """Conversations, optionally filtered to one owner, newest-updated first."""

    @abstractmethod
    def update_conversation(self, conversation_id: str, *, title: str) -> bool:
        """Rename a conversation. Returns False if nothing matched."""

    @abstractmethod
    def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a conversation and all its messages. False if nothing matched."""

    @abstractmethod
    def add_chat_message(self, msg: ChatMessage) -> ChatMessage:
        """Append a message to a conversation (assigns ordinal + ts, bumps the
        conversation's updated_at)."""

    @abstractmethod
    def list_chat_messages(self, conversation_id: str) -> list[ChatMessage]:
        """All messages in a conversation, in send order (by ordinal)."""
