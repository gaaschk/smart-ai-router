"""Tests for the role-agnostic classifier."""
import pytest

from smart_ai_router.classifier import classify, classify_profile, is_actionable


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


# ── classify_profile: the no-LLM profile fallback ─────────────────────────────
#
# This path runs only when every LLM classifier target fails, so it is a floor.
# What it must get right is *which fields* a prompt is in — depth from keywords
# is unavoidably crude, and the tests below assert the field/stakes behavior
# rather than pretending otherwise.

def _fields(profile):
    return {n.field for n in profile.domains}


def test_profile_falls_back_to_the_legacy_field():
    profile = classify_profile("implement a function to parse JSON and fix the bug")
    assert profile.primary_field() == "software_engineering"


def test_profile_names_a_regulatory_field_from_cues():
    # The headline case: even with a crude depth estimate, naming law_regulatory
    # at all means a cheap coding specialist has to clear a law bar too.
    profile = classify_profile(
        "Analyze the regulatory implications of this design across 48 jurisdictions, "
        "citing the specific statutes it would violate."
    )
    assert "law_regulatory" in _fields(profile)
    assert profile.stakes == "high"
    assert "factual_precision" in profile.demands


def test_profile_names_a_medical_field_and_high_stakes():
    profile = classify_profile(
        "What acetaminophen dosage is safe for a patient weighing 16kg?"
    )
    assert "medicine_health" in _fields(profile)
    assert profile.stakes == "high"


def test_profile_medium_stakes_for_professional_but_not_harmful_fields():
    profile = classify_profile(
        "Walk me through the thermodynamics of this heat exchanger design."
    )
    assert "natural_science" in _fields(profile)
    assert profile.stakes == "medium"


def test_profile_of_casual_prompt_stays_cheap():
    profile = classify_profile("hi")
    assert profile.stakes == "low"
    assert profile.demands == frozenset()
    assert profile.peak_requirement() <= 0.5


@pytest.mark.parametrize("prompt", [
    "explain the syntax of this taxonomy file",   # 'syntax'/'taxonomy' contain 'tax'
    "add a contract test for the parser",         # 'contract' alone isn't law
    "diagnose why this build fails",              # 'diagnos' needs a medical frame
])
def test_profile_does_not_misfire_on_software_vocabulary(prompt):
    # Substring matching on short domain words is actively wrong here — "tax" is
    # inside "syntax" and "taxonomy". These prompts must stay out of the
    # professional fields (except where a real cue legitimately fires).
    fields = _fields(classify_profile(prompt))
    assert "finance_business" not in fields
    assert "law_regulatory" not in fields


def test_profile_never_names_more_than_three_fields():
    # More fields only inflates the bar; taxonomy caps it and so must this.
    profile = classify_profile(
        "Given the statute, the patient's dosage, the tax treatment, the reactor "
        "radiation limits, and the theorem we proved, what should we do?"
    )
    assert len(profile.domains) <= 3


def test_profile_legacy_labels_match_classify():
    # The two entry points must never disagree about the primary domain: the
    # usage log and the dashboard read the labels while the router reads the
    # profile.
    for prompt in [
        "implement a function to parse JSON and fix the bug",
        "write documentation and a readme guide for this module",
        "hello",
    ]:
        assert classify_profile(prompt).legacy_labels()[0] == classify(prompt)[0]


def test_profile_depth_is_never_below_practitioner_for_professional_fields():
    # A prompt that reaches into medicine at all is past "any well-informed
    # generalist will do".
    profile = classify_profile("patient dosage?")
    med = next(n for n in profile.domains if n.field == "medicine_health")
    assert med.depth in {"practitioner", "specialist", "frontier"}
