"""Tests for the role-agnostic classifier."""
import pytest

from smart_ai_router.classifier import classify, is_actionable


def test_coding_prompt():
    domain, _ = classify("implement a function to parse JSON and fix the bug")
    assert domain == "coding"


def test_docs_prompt():
    domain, _ = classify("write documentation and a readme guide for this module")
    assert domain == "docs"


def test_short_prompt_is_trivial():
    _, complexity = classify("hello")
    assert complexity == "trivial"


def test_long_prompt_is_hard():
    _, complexity = classify("x " * 450)  # > 800 chars → hard
    assert complexity == "hard"


def test_derivation_is_reasoning_and_hard():
    # A math/physics derivation should not fall through to general/trivial.
    domain, complexity = classify(
        "Derive the formula for the electronic orbitals about a hydrogen atom"
    )
    assert domain == "reasoning"
    assert complexity == "hard"


def test_no_roles():
    # Confirm no agent-name parameter exists
    import inspect
    sig = inspect.signature(classify)
    assert list(sig.parameters.keys()) == ["prompt"]


# ── is_actionable (agent-mode auto-detection) ─────────────────────────────────

@pytest.mark.parametrize("prompt", [
    "make me a resume PDF",
    "create a report.docx summarizing this",
    "generate a PowerPoint from these notes",
    "build me a spreadsheet of the results",
    "turn this into a Word document",
    "export the data as a CSV",
    "draft a cover letter and save it as a PDF",
    "put together a slide deck about our roadmap",
    "write this up as a markdown file",
    "save it to notes.txt",
    "list the files in my workspace",
    "read the file config.yaml and explain it",
])
def test_actionable_prompts_trigger_agent(prompt):
    assert is_actionable(prompt) is True


@pytest.mark.parametrize("prompt", [
    "what is the capital of France?",
    "explain how TCP handshakes work",
    "summarize this article for me",
    "what's the difference between a list and a tuple?",
    "who wrote War and Peace?",
    "help me understand recursion",
    "write a haiku about autumn",     # 'write' verb but no artifact noun
    "make it more concise",           # 'make' verb but no artifact noun
    "",
])
def test_non_actionable_prompts_stay_plain(prompt):
    assert is_actionable(prompt) is False


def test_action_verb_without_artifact_noun_is_not_actionable():
    # A bare verb shouldn't trip it — needs an artifact noun (or extension/phrase).
    assert is_actionable("generate some ideas for a birthday party") is False


def test_artifact_noun_without_action_verb_is_not_actionable():
    # Mentioning a PDF in passing isn't a request to make one.
    assert is_actionable("this PDF is hard to read") is False
