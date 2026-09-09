"""Prompt caching: mark the prefix the client keeps re-sending.

The largest cost lever this router has. Measured from the live usage log: 92% of
lifetime spend was one five-minute coding session — 52 requests, median 69,571
prompt tokens, the same growing prefix re-billed every time, `cached_tokens: 0`
throughout. Routing had nothing left to give (the pick was already the cheapest
Claude), so the money was never in *which* model answered.

Anthropic caches only what you mark, and a client's own breakpoints don't survive
the Anthropic→OpenAI translation layer in front of the router, so the router is
the last place that can set them. These tests pin where they go and — just as
important — every case where they must *not* be set, because a cache write costs
25% extra and a marker nobody reads back is a surcharge.
"""
import httpx
import pytest

from smart_ai_router.api.proxy import _billable_prompt, _cached_tokens
from tests.test_proxy_param_compat import _client, _spec

# Long enough to clear the 2048-token floor (est_tokens is chars // 4), and named
# for what it stands in for: the part of the request that doesn't change.
_FILLER = "context that repeats verbatim on every single turn. " * 200

_HAIKU = "openrouter/anthropic/claude-haiku-4.5"


@pytest.fixture
def claude(monkeypatch):
    return _client(monkeypatch, _spec(_HAIKU, cost=2, cost_input=1.0, cost_output=5.0))


def _turns(*, system=True, assistant=True, filler=True):
    """A conversation in the shape a client re-sends it: stable prefix, new tail."""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": "You are a coding assistant. "
                                                  + (_FILLER if filler else "")})
    msgs.append({"role": "user", "content": "First question. "
                                            + (_FILLER if filler else "")})
    if assistant:
        msgs.append({"role": "assistant", "content": "First answer."})
        msgs.append({"role": "user", "content": "Second question."})
    return msgs


def _chat(client, messages, **extra):
    return client.post("/v1/chat/completions", json={
        "model": "auto", "messages": messages, "stream": False, **extra,
    })


def _marked(msg) -> bool:
    content = msg.get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("cache_control") == {"type": "ephemeral"}
        for b in content
    )


# ── where the breakpoints go ──────────────────────────────────────────────────

def test_a_continued_conversation_is_marked_at_system_and_at_the_tail(claude):
    r = _chat(claude, _turns())
    assert r.status_code == 200
    assert r.headers["X-Cache-Breakpoints"] == "2"

    sent = claude.sent[-1]["messages"]
    # End of the system block, which in Anthropic's cache order also covers the
    # tool definitions — where the tokens actually are on a coding-agent request.
    assert _marked(sent[0])
    # And a rolling one at the end of the history: what this turn writes, the
    # next turn reads, because each turn's prefix contains the last one's.
    assert _marked(sent[-1])
    # Nothing in between, so the cached region is a prefix and not a patchwork.
    assert not any(_marked(m) for m in sent[1:-1])


def test_the_marked_text_is_the_caller_s_text_verbatim(claude):
    # A breakpoint must be additive. Rewriting content into a block array is how
    # `cache_control` is expressed at all, so the text has to survive it exactly.
    original = _turns()
    _chat(claude, original)
    sent = claude.sent[-1]["messages"]
    assert sent[0]["content"][0]["text"] == original[0]["content"]
    assert sent[-1]["content"][0]["text"] == original[-1]["content"]
    assert [m["role"] for m in sent] == [m["role"] for m in original]


def test_the_tail_marker_skips_a_message_with_nothing_to_mark(claude):
    # The final message of an agent round is often an assistant turn that is only
    # tool_calls, with content: null. There is no block to hang a marker on, so
    # the walk goes back to the turn before it rather than losing the breakpoint.
    msgs = [
        {"role": "system", "content": "You are a coding assistant."},
        {"role": "user", "content": "Fix it. " + _FILLER},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "read_file", "arguments": "{}"}}]},
    ]
    r = _chat(claude, msgs)
    assert r.headers["X-Cache-Breakpoints"] == "2"
    sent = claude.sent[-1]["messages"]
    assert _marked(sent[1])              # the user turn before it
    assert sent[2]["content"] is None    # the tool_calls turn is left alone


def test_a_conversation_with_no_system_turn_still_gets_the_rolling_marker(claude):
    r = _chat(claude, _turns(system=False))
    assert r.headers["X-Cache-Breakpoints"] == "1"
    assert _marked(claude.sent[-1]["messages"][-1])


# ── where they must not go ────────────────────────────────────────────────────

def test_a_first_turn_is_never_marked(claude):
    # A write is billed at 1.25×, a read at 0.10×. With no assistant turn in the
    # history there is no evidence the client re-sends its prefix, so a marker
    # here is a 25% surcharge on a prompt that never comes back.
    r = _chat(claude, _turns(assistant=False))
    assert r.headers["X-Cache-Breakpoints"] == "0"
    assert not any(_marked(m) for m in claude.sent[-1]["messages"])


def test_a_fresh_agent_request_is_marked_on_its_first_turn(claude, monkeypatch):
    # The one case where a first turn *is* proof of re-use: agent mode re-sends
    # the whole prefix on every round of its tool loop, so the write is read back
    # within this single request. It is also the exact traffic shape the burst was
    # made of, which makes it the last place to leave uncached.
    seen: list[dict] = []

    async def fake_loop(*, user, body, tool_schemas, stream_model, register_file):
        seen.append(body)
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr("smart_ai_router.api.proxy.run_agent_loop", fake_loop)
    r = _chat(claude, _turns(assistant=False), agent=True)
    assert r.headers["X-Cache-Breakpoints"] == "2"
    assert _marked(seen[0]["messages"][0])     # the loop is seeded with them


def test_a_short_conversation_is_never_marked(claude):
    # Below ~2k tokens Anthropic ignores the breakpoint anyway; writing one would
    # be noise in the body with no effect on the bill.
    r = _chat(claude, _turns(filler=False))
    assert r.headers["X-Cache-Breakpoints"] == "0"


def test_a_non_claude_model_is_never_marked(monkeypatch):
    # Claude is the family that *requires* explicit breakpoints. Every other one
    # either caches long prefixes automatically or has no cache at all, so a
    # marker is at best inert and at worst an unrecognized field.
    c = _client(monkeypatch, _spec("openrouter/openai/gpt-5", cost=2))
    r = _chat(c, _turns())
    assert r.headers["X-Cache-Breakpoints"] == "0"
    assert not any(_marked(m) for m in c.sent[-1]["messages"])


def test_a_caller_that_set_its_own_breakpoints_is_left_alone(claude):
    # It knows its prefix better than this heuristic does. The only reason to
    # guess is that its intent usually doesn't survive translation — when it does,
    # guessing on top of it would spend breakpoints against the caller's plan.
    msgs = _turns()
    msgs[1] = {"role": "user", "content": [
        {"type": "text", "text": "First question. " + _FILLER,
         "cache_control": {"type": "ephemeral"}},
    ]}
    r = _chat(claude, msgs)
    assert r.headers["X-Cache-Breakpoints"] == "0"
    sent = claude.sent[-1]["messages"]
    assert isinstance(sent[0]["content"], str)     # system untouched
    assert _marked(sent[1])                        # the caller's own, still there
    assert isinstance(sent[-1]["content"], str)    # tail untouched


def test_the_setting_turns_it_off(claude, monkeypatch):
    # The escape hatch a provider-side cache error needs, without a redeploy.
    monkeypatch.setenv("SMART_ROUTER_PROMPT_CACHING", "false")
    r = _chat(claude, _turns())
    assert r.headers["X-Cache-Breakpoints"] == "0"
    assert not any(_marked(m) for m in claude.sent[-1]["messages"])


# ── the bill has to reflect it ────────────────────────────────────────────────
# Billing a cache read at the full input rate looks exactly like caching not
# working: same cost for the same tokens, no way to tell the fix landed.

def test_cached_tokens_are_billed_at_a_tenth(claude, monkeypatch):
    async def cached_reply(self, url, **kwargs):
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "id": "cmpl-1",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100_000, "completion_tokens": 0,
                      "total_tokens": 100_000,
                      "prompt_tokens_details": {"cached_tokens": 90_000}},
        })

    monkeypatch.setattr(httpx.AsyncClient, "post", cached_reply)
    _chat(claude, _turns())

    rows = claude.store.recent_usage("", "1970-01-01T00:00:00+00:00")
    # 10k fresh + 90k at 10% = 19k billable, at $1.00/M.
    assert rows[0].cost_usd == pytest.approx(0.019, rel=1e-3)
    # The true token count is still what's recorded — the discount belongs in the
    # money, not in the accounting of what was sent.
    assert rows[0].prompt_tokens == 100_000


def test_cached_tokens_are_read_from_either_spelling():
    assert _cached_tokens({"prompt_tokens_details": {"cached_tokens": 7}}) == 7
    # Anthropic's own name, in case a provider leaks it through an OpenAI-shaped
    # reply. Missing or malformed means "the provider didn't say", never a crash.
    assert _cached_tokens({"cache_read_input_tokens": 5}) == 5
    assert _cached_tokens({"prompt_tokens_details": None}) == 0
    assert _cached_tokens(None) == 0


def test_billable_prompt_never_exceeds_what_was_sent():
    assert _billable_prompt(1000, 0) == 1000
    assert _billable_prompt(1000, 1000) == 100
    # A provider reporting more cached than prompt tokens is nonsense, but it must
    # not produce a negative charge.
    assert _billable_prompt(100, 1000) == 100
