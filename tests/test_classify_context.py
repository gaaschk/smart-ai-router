"""What the classifier reads: the conversation, not just the newest line.

Profiling the last user message alone is correct for a first turn and wrong for
every turn after it. The observed failure, from a real session:

    ts        model                 domain   complexity  prompt_tokens  completion
    03:49:56  ollama/qwen3:30b-a3b  general  trivial     32232          372
    03:31:20  ollama/qwen3:30b-a3b  general  trivial     30955          720

    why: cheapest of 263 qualified; binding constraint
         general_knowledge needed 0.45, has 0.82

Thirty-one thousand tokens of work on a database migration and API endpoints,
profiled `general_knowledge @ surface / trivial`, because the last user turn was a
throwaway acknowledgement. At a 0.45 bar 263 models qualify and the cheapest free
local one wins — and it answered by inventing both the migration file and a
`python -m smart_ai_router.migrate` command that does not exist.

Measured against the deployment's own triage model (llama3.1:8b), profiling the
same follow-ups alone versus with the conversation ahead of them:

    follow-up                            ALONE                  WITH CONTEXT
    "yes"                                ('general', 'trivial') ('coding', 'hard')
    "ok"                                 ('coding', 'moderate') ('coding', 'hard')
    "you tell me what to execute first"  ('coding', 'moderate') ('coding', 'hard')
    "you do it" / "continue" / "next"    ('coding', 'hard')     ('coding', 'hard')

`"yes"` reproduces the logged failure exactly. The point is not that every wording
is wrong — it is that the profile tracks *which throwaway word the user typed*
instead of what the work is, so the same conversation lands on three different
tiers. With context all ten follow-ups tested agree, and the `agentic` demand goes
from never set (0/8) to always set (8/8).

Note this fixes the *bar*, not the collapse to free local models on its own: at
`software_engineering @ practitioner` around 204 models still qualify and
cheapest-first still prefers a free one. The cost:quality bias and the tool-health
floor are what address that; this makes sure they are at least being applied to
the right tier.
"""
from __future__ import annotations

import pytest

from smart_ai_router.api.proxy import (
    _CLASSIFY_CONTEXT_HEADING,
    _CLASSIFY_REQUEST_HEADING,
    _classify_text,
    _extract_prompt,
)


@pytest.fixture(autouse=True)
def _budget(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_CONTEXT_CHARS", "4000")


def _convo(*turns):
    """turns are (role, text), oldest first."""
    return [{"role": r, "content": t} for r, t in turns]


_PLAN = (
    "create a plan to introduce a project concept where a user can create a "
    "project and invite other users to collaborate. consider the impact to "
    "architecture, security, user experience"
)


# ── The regression ────────────────────────────────────────────────────────────

def test_a_short_follow_up_carries_the_conversations_work():
    convo = _convo(
        ("user", _PLAN),
        ("assistant", "Here is the plan..."),
        ("user", "you do it"),
    )
    text = _classify_text(convo)
    # The three words the router used to profile on, and the work they refer to.
    assert "you do it" in text
    assert "project" in text and "architecture" in text


def test_the_newest_turn_is_last_so_recency_still_reads():
    convo = _convo(("user", "first thing"), ("user", "second thing"))
    assert _classify_text(convo).endswith("second thing")


def test_context_is_labelled_not_just_concatenated():
    """The load-bearing detail, measured on the deployment's own triage model.

    Handed one undifferentiated blob the classifier profiles all of it, so a change
    of subject inherits the old topic's difficulty: "unrelated: what's the capital
    of France?" after a planning conversation profiled ('general','trivial') alone
    and ('coding','hard') with the conversation merely prepended. With the two
    headings it is ('general','trivial') 5/5, while "you do it" after the same
    conversation stays ('coding','hard') 5/5.
    """
    convo = _convo(("user", _PLAN), ("user", "you do it"))
    text = _classify_text(convo)
    assert _CLASSIFY_CONTEXT_HEADING in text
    assert _CLASSIFY_REQUEST_HEADING in text
    # The request has to come after its heading, or the label means nothing.
    assert text.index(_CLASSIFY_REQUEST_HEADING) < text.index("you do it")
    assert text.index(_CLASSIFY_CONTEXT_HEADING) < text.index(_PLAN)


def test_assistant_replies_are_never_included():
    # Otherwise the classifier profiles its own answer, and a reply full of code
    # asserts a domain the user never asked for.
    convo = _convo(
        ("user", "what is this"),
        ("assistant", "def migrate(): ...  SQL ALTER TABLE projects"),
        ("user", "ok"),
    )
    text = _classify_text(convo)
    assert "ALTER TABLE" not in text
    assert "def migrate" not in text


# ── What must not change ──────────────────────────────────────────────────────

def test_a_first_turn_classifies_exactly_as_before():
    convo = _convo(("user", _PLAN))
    assert _classify_text(convo) == _PLAN == _extract_prompt(convo)


def test_a_long_first_turn_is_not_truncated():
    # The budget governs added context, not the caller's own message — cutting
    # that would be a silent regression in what a single prompt is judged on.
    long_prompt = "x" * 50_000
    convo = _convo(("user", long_prompt))
    assert _classify_text(convo) == long_prompt


def test_zero_budget_restores_last_message_only(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_CONTEXT_CHARS", "0")
    convo = _convo(("user", _PLAN), ("user", "you do it"))
    assert _classify_text(convo) == "you do it"


def test_no_user_message_yields_nothing():
    assert _classify_text(_convo(("system", "be helpful"))) == ""


# ── The budget ────────────────────────────────────────────────────────────────

def test_context_is_capped(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_CONTEXT_CHARS", "100")
    convo = _convo(*[("user", "a" * 500) for _ in range(20)], ("user", "go"))
    text = _classify_text(convo)
    # The last turn in full, plus at most the budget of earlier context — not the
    # whole history, which would put 10k chars through a local 8B every request.
    assert text.endswith("go")
    # Measured on the history section alone: the budget governs what we send of the
    # conversation, and the two fixed headings are not conversation.
    history = text.split(_CLASSIFY_CONTEXT_HEADING)[1]
    history = history.split(_CLASSIFY_REQUEST_HEADING)[0]
    assert len(history.strip()) <= 100


def test_the_tail_of_a_cut_turn_is_kept(monkeypatch):
    # When a turn has to be cut, its end is the part that led to what came next.
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_CONTEXT_CHARS", "20")
    convo = _convo(("user", "irrelevant preamble " + "THE ACTUAL ASK"),
                   ("user", "continue"))
    assert "THE ACTUAL ASK" in _classify_text(convo)


def test_content_parts_are_read_like_plain_strings():
    # A client sending OpenAI content parts must not lose its context silently.
    convo = [
        {"role": "user", "content": [
            {"type": "text", "text": _PLAN},
            {"type": "image_url", "image_url": {"url": "data:..."}},
        ]},
        {"role": "user", "content": "you do it"},
    ]
    text = _classify_text(convo)
    assert "architecture" in text and text.endswith("you do it")
