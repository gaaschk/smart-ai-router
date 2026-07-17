"""Tests for the role-agnostic classifier."""
from capability_router.classifier import classify


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


def test_no_roles():
    # Confirm no agent-name parameter exists
    import inspect
    sig = inspect.signature(classify)
    assert list(sig.parameters.keys()) == ["prompt"]
