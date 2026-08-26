"""Tests for the SQLite chat-history store (conversations + messages)."""
from smart_ai_router.models import ChatMessage, Conversation, generate_conversation_id
from smart_ai_router.store.sqlite_store import SqliteStore


def _conv(user="alice", title="New chat"):
    return Conversation(id=generate_conversation_id(), user=user, title=title)


def test_create_stamps_timestamps_and_returns_record():
    store = SqliteStore(":memory:")
    conv = store.create_conversation(_conv())
    assert conv.id.startswith("conv-")
    assert conv.created_at  # stamped
    assert conv.updated_at == conv.created_at


def test_get_and_list_scoped_by_user():
    store = SqliteStore(":memory:")
    a = store.create_conversation(_conv(user="alice", title="A"))
    store.create_conversation(_conv(user="bob", title="B"))

    assert store.get_conversation(a.id).title == "A"
    assert [c.title for c in store.list_conversations("alice")] == ["A"]
    # None → admin view sees all.
    assert {c.title for c in store.list_conversations(None)} == {"A", "B"}


def test_get_unknown_returns_none():
    store = SqliteStore(":memory:")
    assert store.get_conversation("conv-nope") is None


def test_messages_get_sequential_ordinals_in_send_order():
    store = SqliteStore(":memory:")
    conv = store.create_conversation(_conv())
    store.add_chat_message(ChatMessage(conversation_id=conv.id, role="user", content="hi"))
    store.add_chat_message(ChatMessage(conversation_id=conv.id, role="assistant", content="hello"))
    store.add_chat_message(ChatMessage(conversation_id=conv.id, role="user", content="bye"))

    msgs = store.list_chat_messages(conv.id)
    assert [m.ordinal for m in msgs] == [0, 1, 2]
    assert [m.content for m in msgs] == ["hi", "hello", "bye"]
    assert [m.role for m in msgs] == ["user", "assistant", "user"]


def test_content_json_flag_round_trips():
    store = SqliteStore(":memory:")
    conv = store.create_conversation(_conv())
    store.add_chat_message(ChatMessage(
        conversation_id=conv.id, role="user",
        content='[{"type": "text", "text": "hi"}]', content_json=True,
    ))
    msg = store.list_chat_messages(conv.id)[0]
    assert msg.content_json is True
    assert msg.content == '[{"type": "text", "text": "hi"}]'


def test_adding_message_bumps_updated_at():
    store = SqliteStore(":memory:")
    conv = store.create_conversation(_conv())
    original = conv.updated_at
    msg = store.add_chat_message(
        ChatMessage(conversation_id=conv.id, role="user", content="x", ts="2099-01-01T00:00:00+00:00")
    )
    refreshed = store.get_conversation(conv.id)
    assert refreshed.updated_at == msg.ts == "2099-01-01T00:00:00+00:00"
    assert refreshed.updated_at != original


def test_update_conversation_renames():
    store = SqliteStore(":memory:")
    conv = store.create_conversation(_conv(title="old"))
    assert store.update_conversation(conv.id, title="new") is True
    assert store.get_conversation(conv.id).title == "new"


def test_update_unknown_returns_false():
    store = SqliteStore(":memory:")
    assert store.update_conversation("conv-nope", title="x") is False


# ── Tags (grouping) ────────────────────────────────────────────────────────────

def test_tags_round_trip_on_create_and_list():
    store = SqliteStore(":memory:")
    conv = store.create_conversation(Conversation(
        id=generate_conversation_id(), user="alice", title="A", tags=["work", "cost"]
    ))
    assert sorted(store.get_conversation(conv.id).tags) == ["cost", "work"]
    assert sorted(store.list_conversations("alice")[0].tags) == ["cost", "work"]


def test_update_replaces_tag_set_and_can_clear_it():
    store = SqliteStore(":memory:")
    conv = store.create_conversation(_conv())
    store.update_conversation(conv.id, tags=["work", "cost"])
    store.update_conversation(conv.id, tags=["spike"])
    assert store.get_conversation(conv.id).tags == ["spike"]

    store.update_conversation(conv.id, tags=[])
    assert store.get_conversation(conv.id).tags == []


def test_retagging_leaves_title_and_recency_alone():
    store = SqliteStore(":memory:")
    conv = store.create_conversation(_conv(title="keep me"))
    before = store.get_conversation(conv.id).updated_at

    store.update_conversation(conv.id, tags=["work"])
    after = store.get_conversation(conv.id)
    # Filing a thread isn't activity — the sidebar's order must not shuffle.
    assert (after.title, after.updated_at) == ("keep me", before)


def test_list_filters_by_tag_within_a_user():
    store = SqliteStore(":memory:")
    a = store.create_conversation(_conv(title="A"))
    store.create_conversation(_conv(title="B"))
    bob = store.create_conversation(_conv(user="bob", title="C"))
    store.update_conversation(a.id, tags=["work"])
    store.update_conversation(bob.id, tags=["work"])

    assert [c.title for c in store.list_conversations("alice", tag="work")] == ["A"]
    # Admin view (user=None) crosses owners.
    assert {c.title for c in store.list_conversations(None, tag="work")} == {"A", "C"}
    assert store.list_conversations("alice", tag="nope") == []


def test_list_conversation_users_is_distinct_and_sorted():
    store = SqliteStore(":memory:")
    store.create_conversation(_conv(user="bob"))
    store.create_conversation(_conv(user="alice"))
    store.create_conversation(_conv(user="alice"))
    assert store.list_conversation_users() == ["alice", "bob"]


def test_delete_cascades_tags():
    store = SqliteStore(":memory:")
    conv = store.create_conversation(_conv())
    store.update_conversation(conv.id, tags=["work"])
    store.delete_conversation(conv.id)

    # No orphan rows: a new conversation reusing that tag sees only its own.
    fresh = store.create_conversation(_conv(title="fresh"))
    assert store.get_conversation(fresh.id).tags == []
    assert store.list_conversations("alice", tag="work") == []


def test_delete_cascades_messages():
    store = SqliteStore(":memory:")
    conv = store.create_conversation(_conv())
    store.add_chat_message(ChatMessage(conversation_id=conv.id, role="user", content="a"))
    store.add_chat_message(ChatMessage(conversation_id=conv.id, role="user", content="b"))

    assert store.delete_conversation(conv.id) is True
    assert store.get_conversation(conv.id) is None
    # Messages are gone too — no orphans.
    assert store.list_chat_messages(conv.id) == []


def test_delete_unknown_returns_false():
    store = SqliteStore(":memory:")
    assert store.delete_conversation("conv-nope") is False


def test_list_orders_newest_updated_first():
    store = SqliteStore(":memory:")
    c1 = store.create_conversation(_conv(title="first"))
    c2 = store.create_conversation(_conv(title="second"))
    # Touch c1 with a far-future ts so it sorts ahead of c2.
    store.add_chat_message(
        ChatMessage(conversation_id=c1.id, role="user", content="x", ts="2099-01-01T00:00:00+00:00")
    )
    titles = [c.title for c in store.list_conversations("alice")]
    assert titles[0] == "first"
