"""Output budget, the rich-output note, and truncation being visible.

The bug behind all three: a user asked for a short story and got one paragraph,
cut off mid-sentence, with no visuals and nothing on screen saying it had been
cut. Three separate causes, so three groups of tests:

  * the output ceiling was sized for a chat reply, and nothing distinguished a
    reply from a document
  * the model was never told the page renders anything but prose
  * the reply was truncated and the transcript recorded it as if complete

Each group pins the property, not the implementation: the numbers come from
settings, so a test asserting "16384" would pass while asserting nothing about
whether the *profile* is what chose it.
"""
import json
import warnings
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient

from smart_ai_router import public_access as _public
from smart_ai_router import settings as _settings
from smart_ai_router import sync as sync_mod
from smart_ai_router.api import proxy as _proxy
from smart_ai_router.api.app import create_app
from smart_ai_router.classifier import classify_profile
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ChatMessage, Conversation, ModelSpec
from smart_ai_router.store.sqlite_store import SqliteStore
from smart_ai_router.sync import sync_from_providers
from smart_ai_router.taxonomy import DomainNeed, PromptProfile

_REPLY = {
    "id": "cmpl-1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "answer."},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

# A document request and a one-line question. The whole point is that these two
# must not get the same ceiling.
_STORY = "Write me a short story about a bounty hunter who meets a Sith lord."
_FACT = "What is the capital of France?"

_BROWSER = {"sec-fetch-site": "same-origin"}
_UI = {"X-Smart-Router-Client": "ui"}


def _profile(score=0.95):
    return {"software_engineering": score, "law_regulatory": score,
            "medicine_health": score, "general_knowledge": score,
            "creative_writing": score, "technical_writing": score}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    monkeypatch.delenv("SMART_ROUTER_MODEL_DENYLIST", raising=False)
    # Keyword profiler only — no network classifier in these tests.
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_MODEL", "")
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_FALLBACK", "")
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_REFINE_MODEL", "")
    monkeypatch.setenv("SMART_ROUTER_DEFAULT_MAX_TOKENS", "4096")
    monkeypatch.setenv("SMART_ROUTER_LONG_FORM_MAX_TOKENS", "16384")

    sent: list[dict] = []

    async def fake_post(self, url, **kwargs):
        sent.append(kwargs.get("json") or {})
        return httpx.Response(200, json=_REPLY, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    store = SqliteStore(":memory:")
    store.upsert_model(ModelSpec(
        value="openrouter/generalist", provider="openrouter", cost=1, ctx_k=200,
        reliability=1.0, tools=True, profile=_profile(),
        competence={"coding": 0.95, "reasoning": 0.95, "docs": 0.95, "general": 0.95},
    ))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        c = TestClient(create_app(CapabilityRouter(store=store)))
    c.sent = sent    # forwarded bodies, for asserting what the provider saw
    c.store = store
    return c


def _chat(client, prompt, *, headers=None, **body):
    payload = {"model": "auto", "messages": [{"role": "user", "content": prompt}],
               "stream": False}
    payload.update(body)
    return client.post("/v1/chat/completions", json=payload, headers=headers or {})


# ── The output budget ───────────────────────────────────────────────────────────

def test_a_document_request_gets_a_bigger_ceiling_than_a_question(client):
    """The reported bug, at the wire level.

    Not "the story got 16384" — that number is a setting. What must hold is that
    the two requests get *different* ceilings, and that the document gets the
    larger one, because a single ceiling is exactly what produced a paragraph.
    """
    r_story = _chat(client, _STORY)
    story_limit = client.sent[-1]["max_tokens"]
    _chat(client, _FACT)
    fact_limit = client.sent[-1]["max_tokens"]

    assert story_limit > fact_limit
    assert fact_limit == _settings.get_int("default_max_tokens")
    assert story_limit == _settings.get_int("long_form_max_tokens")
    # And the caller is told which ceiling bound the reply.
    assert r_story.headers["X-Output-Limit"] == str(story_limit)


def test_the_profile_is_what_earns_the_bigger_ceiling(client):
    """The budget must follow the classification, not a keyword list of its own.

    If these two ever disagree, the reason a request got a document budget stops
    being inspectable: X-Prompt-Profile would say one thing and the ceiling
    another, and there would be no way to tell which one to fix.
    """
    assert classify_profile(_STORY).is_long_form() is True
    assert classify_profile(_FACT).is_long_form() is False


def test_a_caller_supplied_ceiling_is_never_overridden(client):
    """A program that names max_tokens has a reason — usually its own budget."""
    _chat(client, _STORY, max_tokens=100)
    assert client.sent[-1]["max_tokens"] == 100


def test_the_budget_never_lowers_a_raised_default(monkeypatch):
    """An operator who raised default_max_tokens meant it.

    Long-form is a floor for documents, not a cap on everything else: if the
    default were higher, taking min() here would quietly undo the operator's
    setting on exactly the requests that need room most.
    """
    monkeypatch.setenv("SMART_ROUTER_DEFAULT_MAX_TOKENS", "32000")
    monkeypatch.setenv("SMART_ROUTER_LONG_FORM_MAX_TOKENS", "16384")
    long_form = PromptProfile(
        domains=(DomainNeed(field="creative_writing", depth="practitioner"),)
    )
    assert _proxy._output_budget(long_form) == 32000
    assert _proxy._output_budget(None) == 32000


def test_a_secondary_long_form_field_counts(client):
    """"Write the API guide for this service" is a document even though the
    classifier names the subject matter first — so is_long_form() looks at every
    named field, not just the primary one."""
    profile = classify_profile("Write the API guide for this service")
    assert profile.primary_field() != "technical_writing"
    assert profile.is_long_form() is True


# ── The one ceiling we can't raise: the model's own ─────────────────────────────

def _long_form():
    return PromptProfile(
        domains=(DomainNeed(field="creative_writing", depth="practitioner"),)
    )


def test_the_budget_never_exceeds_what_the_model_can_emit(monkeypatch):
    """Asking for more than a model allows is not a harmless overshoot.

    Several providers reject the call outright, so an ambitious setting would turn
    a long answer into no answer — and output limits on the live catalog run from
    2048 to 1.8M, which is why this has to come from the model rather than a guess.
    """
    monkeypatch.setenv("SMART_ROUTER_DEFAULT_MAX_TOKENS", "4096")
    monkeypatch.setenv("SMART_ROUTER_LONG_FORM_MAX_TOKENS", "32768")
    cramped = ModelSpec(value="openrouter/cramped", max_output=2048)
    roomy = ModelSpec(value="openrouter/roomy", max_output=64000)

    assert _proxy._output_budget(_long_form(), roomy) == 32768
    assert _proxy._output_budget(_long_form(), cramped) == 2048
    # And on an ordinary reply too: the default alone would be rejected here.
    assert _proxy._output_budget(None, cramped) == 2048


def test_an_unknown_model_ceiling_does_not_clamp(monkeypatch):
    """0 means the catalog didn't say, not "zero tokens".

    Every local model is in that position. Clamping on an unknown would silence
    them; sending the budget is right, because Ollama treats max_tokens as
    num_predict and simply stops there.
    """
    monkeypatch.setenv("SMART_ROUTER_LONG_FORM_MAX_TOKENS", "32768")
    assert _proxy._output_budget(
        _long_form(), ModelSpec(value="ollama/local", max_output=0)
    ) == 32768
    assert _proxy._output_budget(_long_form(), None) == 32768


def test_a_models_output_ceiling_round_trips_through_the_store():
    store = SqliteStore(":memory:")
    store.upsert_model(ModelSpec(value="openrouter/x", max_output=65536))
    assert store.get("openrouter/x").max_output == 65536
    # Unknown stays unknown rather than becoming a number we made up.
    store.upsert_model(ModelSpec(value="ollama/y"))
    assert store.get("ollama/y").max_output == 0


def test_sync_reads_each_models_output_ceiling_from_the_catalog(monkeypatch):
    """OpenRouter publishes it per model; nothing else in the row implies it.

    The second model here is the case that matters: a 200k context window and a
    2048-token output ceiling. Reading it off context_length — the obvious guess —
    would ask for 100x what it can give.
    """
    catalog = {"data": [
        {"id": "vendor/roomy", "context_length": 200000,
         "architecture": {"modality": "text->text"},
         "top_provider": {"max_completion_tokens": 64000}},
        {"id": "vendor/cramped", "context_length": 200000,
         "architecture": {"modality": "text->text"},
         "top_provider": {"max_completion_tokens": 2048}},
        # No top_provider block at all — a handful of real rows look like this.
        {"id": "vendor/silent", "context_length": 200000,
         "architecture": {"modality": "text->text"}},
    ]}

    def _open(req, timeout=0):
        class _Resp(BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _Resp(json.dumps(catalog).encode())

    monkeypatch.setattr(sync_mod.urllib.request, "urlopen", _open)
    store = SqliteStore(":memory:")
    sync_from_providers(store, openrouter_key="k")

    ceilings = {s.value: s.max_output for s in store.all_models()}
    assert ceilings["openrouter/vendor/roomy"] == 64000
    assert ceilings["openrouter/vendor/cramped"] == 2048
    # Unknown, not guessed from the 200k context window.
    assert ceilings["openrouter/vendor/silent"] == 0


def test_the_curated_bedrock_rows_carry_an_output_ceiling():
    """Bedrock's catalog isn't fetched, so these numbers are ours to get right.

    Anthropic rejects a max_tokens above the model's limit, so a missing ceiling
    here would mean every Claude row silently accepts whatever long_form_max_tokens
    is set to — and errors once someone raises it past 32k.
    """
    store = SqliteStore(":memory:")
    sync_from_providers(store, bedrock_key="x")
    assert all(s.max_output > 0 for s in store.all_models())


@pytest.fixture
def mixed_client(monkeypatch, client):
    """Two qualified models: the cheap one can't finish a document, the dear one can.

    This is the shape the catalog actually has — low output ceilings cluster at the
    cheap end — and it is the case where "cheapest qualified wins" and "don't
    truncate the story" pull in opposite directions.
    """
    monkeypatch.setenv("SMART_ROUTER_LONG_FORM_MIN_MODEL_OUTPUT", "8192")
    client.store.delete_model("openrouter/generalist")
    client.store.upsert_model(ModelSpec(
        value="openrouter/cheap-cramped", provider="openrouter", cost=1, ctx_k=200,
        max_output=2048, reliability=1.0, tools=True, profile=_profile(),
        competence={"coding": 0.95, "reasoning": 0.95, "docs": 0.95, "general": 0.95},
    ))
    client.store.upsert_model(ModelSpec(
        value="openrouter/dear-roomy", provider="openrouter", cost=5, ctx_k=200,
        max_output=64000, reliability=1.0, tools=True, profile=_profile(),
        competence={"coding": 0.95, "reasoning": 0.95, "docs": 0.95, "general": 0.95},
    ))
    return client


def test_a_document_avoids_a_model_that_cannot_finish_one(mixed_client):
    """Room to write is worth more than the price difference, for a document.

    The user's call, stated plainly: a clean answer beats saving a few cents. So
    the cheapest-qualified rule yields on long-form requests only — and the
    explanation says so, because otherwise the pick looks like the router quietly
    ignoring its own rule.
    """
    r = _chat(mixed_client, _STORY)
    assert r.headers["X-Routed-Model"] == "openrouter/dear-roomy"
    assert "capped too low to finish a document" in r.headers["X-Routing-Why"]
    assert mixed_client.sent[-1]["max_tokens"] > 2048


def test_an_ordinary_question_still_takes_the_cheapest(mixed_client):
    """The preference is scoped to documents. A 2048-token ceiling is plenty for
    "what is the capital of France", and paying five times as much for it would be
    the router failing at its whole purpose."""
    r = _chat(mixed_client, _FACT)
    assert r.headers["X-Routed-Model"] == "openrouter/cheap-cramped"
    assert "capped too low" not in r.headers["X-Routing-Why"]


def test_capacity_is_a_preference_not_a_veto(monkeypatch, client):
    """When nothing roomy qualifies, the request is still answered.

    A hard filter here would turn "no model can write a long story" into an error
    on a request that a cramped model would have answered imperfectly — trading a
    truncated story for no story, which is the wrong direction.
    """
    monkeypatch.setenv("SMART_ROUTER_LONG_FORM_MIN_MODEL_OUTPUT", "8192")
    client.store.delete_model("openrouter/generalist")
    client.store.upsert_model(ModelSpec(
        value="openrouter/only-option", provider="openrouter", cost=1, ctx_k=200,
        max_output=2048, reliability=1.0, tools=True, profile=_profile(),
        competence={"coding": 0.95, "reasoning": 0.95, "docs": 0.95, "general": 0.95},
    ))
    r = _chat(client, _STORY)
    assert r.status_code == 200
    assert r.headers["X-Routed-Model"] == "openrouter/only-option"
    # Nothing was passed over, so nothing is claimed to have been.
    assert "capped too low" not in r.headers["X-Routing-Why"]
    # And we ask for exactly what it can give rather than a number it would reject.
    assert client.sent[-1]["max_tokens"] == 2048


@pytest.mark.parametrize("prompt", [
    "Explain this error message",              # "explain" alone is not a lesson
    "How do you say hello in Spanish?",        # a phrase, not a translation job
    "add a docstring to this function",        # writing, but not a document
    "refactor this god class",
])
def test_ordinary_requests_keep_the_ordinary_ceiling(prompt):
    """The cues have to be narrow or the budget means nothing.

    Every prompt here sits next to a long-form cue — explain, Spanish, writing —
    and none of them is a document. If they start classifying long-form, the
    distinction between a reply and a document collapses and the ceiling is back
    to being one number for everything.

    Not airtight: the legacy domain heuristic already calls "add documentation to
    this function" a docs prompt, and that inherited verdict now buys a document
    ceiling it won't use. A ceiling is not a target, so the cost is bounded — and
    it is not worth re-tuning the routing heuristic to fix.
    """
    assert classify_profile(prompt).is_long_form() is False


@pytest.mark.parametrize("prompt", [
    "Write me a short story about a lighthouse keeper",
    "Write a poem about the sea",
    "Write the migration guide for v2",
    "Write a lesson plan on the water cycle",
    "Translate this into Japanese",
])
def test_document_requests_are_recognized_without_an_llm(prompt):
    """These run through the keyword fallback, which is the path that had no
    creative-writing cue at all — so the reported bug reproduced even when the
    LLM classifier was down, and this is the floor that had to be raised."""
    assert classify_profile(prompt).is_long_form() is True


# ── The caps that bound cost still win ──────────────────────────────────────────

@pytest.fixture
def anon_client(monkeypatch, client):
    """Same router, with anonymous browser access on and a tight output cap."""
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", "admin-secret")
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_CHAT", "true")
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_DAILY_BUDGET", "5.00")
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_MAX_TIER", "3")
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_MAX_OUTPUT_TOKENS", "512")
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_RL_MAX_REQ", "100")
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_RL_WINDOW_S", "3600")
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_MAX_CONCURRENT", "0")
    _public.reset_rate_limits()
    yield client
    _public.reset_rate_limits()


def test_an_anonymous_visitors_cap_still_beats_the_document_budget(anon_client):
    """Room to write a story is not permission to spend the operator's money.

    The per-identity ceiling is applied after the budget and over anything the
    caller asked for, so a long-form profile can raise the *default* without
    raising what an unvetted visitor can cost. A story request from a stranger
    gets a truncated story, and the truncation notice is what makes that honest
    rather than baffling.
    """
    r = _chat(anon_client, _STORY, headers=_BROWSER)
    assert r.status_code == 200
    cap = _settings.get_int("public_max_output_tokens")
    assert anon_client.sent[-1]["max_tokens"] == cap
    assert cap < _settings.get_int("long_form_max_tokens")
    assert r.headers["X-Output-Limit"] == str(cap)


# ── The rich-output note ────────────────────────────────────────────────────────

def _system_turns(body):
    return [m for m in body["messages"] if m.get("role") == "system"]


def test_the_chat_page_gets_told_what_it_can_render(client):
    r = _chat(client, _STORY, headers=_UI)
    assert r.status_code == 200
    notes = _system_turns(client.sent[-1])
    assert len(notes) == 1
    assert notes[0]["content"] == _settings.get("chat_rich_output_prompt").strip()
    # Prepended, so the caller's own turns still read in their original order and
    # anything they said later wins a disagreement.
    forwarded = client.sent[-1]["messages"]
    assert forwarded[0]["role"] == "system"
    assert forwarded[1:] == [{"role": "user", "content": _STORY}]


def test_an_api_client_gets_exactly_the_messages_it_sent(client):
    """A program driving /v1 wants its own prompt and nothing else — an injected
    system turn changes its output, and once it's history it keeps changing it."""
    _chat(client, _STORY)
    assert _system_turns(client.sent[-1]) == []


def test_a_tool_using_client_gets_no_note_even_from_the_page(client):
    """Tools mean an agent loop, where prose about rendering is noise at best."""
    _chat(client, _STORY, headers=_UI, tools=[{
        "type": "function",
        "function": {"name": "noop", "parameters": {"type": "object", "properties": {}}},
    }])
    assert _system_turns(client.sent[-1]) == []


def test_an_operator_can_switch_the_note_off(monkeypatch, client):
    """Blankable, because "let the model answer unprompted" is a legitimate
    preference and shouldn't require a code change to express."""
    monkeypatch.setenv("SMART_ROUTER_CHAT_RICH_OUTPUT_PROMPT", "")
    _chat(client, _STORY, headers=_UI)
    assert _system_turns(client.sent[-1]) == []


def test_the_note_does_not_change_how_the_prompt_is_classified(client):
    """It's injected after routing, so it cannot leak into the profile.

    If it could, the note's own words ("diagram", "document", "markdown") would
    start steering the model choice — the note would be classifying prompts.
    """
    plain = _chat(client, _FACT)
    with_note = _chat(client, _FACT, headers=_UI)
    assert with_note.headers["X-Prompt-Profile"] == plain.headers["X-Prompt-Profile"]
    assert with_note.headers["X-Routed-Model"] == plain.headers["X-Routed-Model"]
    assert with_note.headers["X-Output-Limit"] == plain.headers["X-Output-Limit"]


# ── Truncation survives a reload ────────────────────────────────────────────────

def test_a_truncated_message_is_still_truncated_after_a_reload():
    """A cut-off reply that reopens looking complete is the worst version of this
    bug: the reader blames the model for stopping mid-sentence, and the ceiling
    that actually stopped it is invisible. So the flag is stored, not derived."""
    store = SqliteStore(":memory:")
    conv = store.create_conversation(Conversation(id="conv-t1", user="alice"))
    store.add_chat_message(ChatMessage(
        conversation_id=conv.id, role="assistant", content="The bounty was",
        truncated=True,
    ))
    store.add_chat_message(ChatMessage(
        conversation_id=conv.id, role="assistant", content="Done.",
    ))
    assert [m.truncated for m in store.list_chat_messages(conv.id)] == [True, False]


def test_truncation_round_trips_through_the_api(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        c = TestClient(create_app(CapabilityRouter(store=SqliteStore(":memory:"))))
    conv = c.post("/api/conversations", json={"title": "Story"}).json()
    c.post(f"/api/conversations/{conv['id']}/messages",
           json={"role": "assistant", "content": "The bounty was", "truncated": True})
    c.post(f"/api/conversations/{conv['id']}/messages",
           json={"role": "assistant", "content": "Done."})

    msgs = c.get(f"/api/conversations/{conv['id']}").json()["messages"]
    assert [m["truncated"] for m in msgs] == [True, False]
