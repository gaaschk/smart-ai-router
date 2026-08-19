"""
Role-agnostic prompt classifier — the deterministic fallback.

classify(prompt)         -> (domain, complexity)      legacy label pair
classify_profile(prompt) -> taxonomy.PromptProfile    what route() matches on

No role knowledge — callers that need role-based priors should build their
own hint before calling CapabilityRouter.route(). This helper exists for
callers (e.g. a proxy) that only have raw prompt text.

This path runs only when every LLM classifier target fails (see
llm_classifier.py), so it is a floor, not the primary signal. Keyword counting
cannot judge how *deep* into a field an answer must go — that is exactly what it
gets wrong. What it can do reliably is recognize which professional field a
prompt is in, and whether the answer will have to name real statutes, standards,
or dosages. classify_profile() therefore takes its depth from the legacy
complexity heuristic and spends its effort on naming the right fields, which is
where a mis-route is most expensive.

Domain:     "coding" | "docs" | "reasoning" | "general"
Complexity: "trivial" | "moderate" | "hard"
"""
from __future__ import annotations

import re

from smart_ai_router.taxonomy import (
    DEPTH_RANK,
    DomainNeed,
    PromptProfile,
    profile_from_labels,
)

# ── Actionable-intent signals (agent-mode auto-detection) ─────────────────────
#
# is_actionable(prompt) decides whether a request should auto-enter agent mode:
# does the user want the assistant to *produce a file* or *do filesystem work*,
# rather than just answer? It is deliberately CONSERVATIVE — a false positive
# routes a plain question onto a pricier tool-capable model and runs the tool
# loop needlessly, undercutting the router's cost story. So it fires only on a
# strong signal: an action verb paired with a file/artifact noun, an explicit
# file extension, or an unambiguous filesystem verb phrase.

# Verbs that signal "produce/modify an artifact".
_ACTION_VERBS = frozenset({
    "create", "make", "generate", "build", "draft", "produce", "write",
    "save", "export", "download", "compile", "assemble", "put together",
    "prepare", "turn", "convert",
})

# Nouns naming a downloadable artifact. Paired with an action verb these mean
# "make me this file" — the core create_document use case.
_ARTIFACT_NOUNS = frozenset({
    "pdf", "document", "doc", "docx", "word doc", "resume", "cv",
    "cover letter", "spreadsheet", "excel", "xlsx", "workbook", "csv",
    "powerpoint", "pptx", "presentation", "slide deck", "slides", "slide",
    "report", "markdown file", "text file", "file", "handout", "worksheet",
})

# Unambiguous filesystem/agent phrases — actionable on their own (no noun pair
# needed) because they describe operating on the workspace directly.
_FS_PHRASES = (
    "list files", "list the files", "in my workspace", "read the file",
    "edit the file", "update the file", "open the file", "run this script",
    "run the script", "save it to", "save this to", "write it to a file",
    "write to a file", "save as a", "save as an",
)

# An explicit downloadable-file extension anywhere in the prompt.
_FILE_EXT_RE = re.compile(
    r"\.(pdf|docx?|pptx?|xlsx?|md|markdown|txt|csv)\b", re.IGNORECASE
)


def is_actionable(prompt: str) -> bool:
    """True if the prompt asks the assistant to produce a file or do filesystem
    work — i.e. it should auto-enter agent mode. Conservative by design."""
    if not prompt:
        return False
    lower = prompt.lower()

    if _FILE_EXT_RE.search(lower):
        return True
    if any(phrase in lower for phrase in _FS_PHRASES):
        return True
    has_verb = any(v in lower for v in _ACTION_VERBS)
    has_noun = any(n in lower for n in _ARTIFACT_NOUNS)
    return has_verb and has_noun


# ── Domain keyword signals ────────────────────────────────────────────────────

_CODING_HINTS = frozenset({
    "code", "implement", "function", "class", "bug", "fix", "refactor",
    "test", "unit test", "integration", "compile", "syntax", "script",
    "module", "import", "library", "dependency", "api", "endpoint",
    "database", "query", "schema", "migration", "debug",
})

_DOCS_HINTS = frozenset({
    "document", "documentation", "readme", "guide", "tutorial", "explain",
    "summarize", "summarise", "write up", "report", "spec", "specification",
    "writeup", "changelog", "release note", "diataxis", "how-to",
    "long doc", "article", "write about",
})

_REASONING_HINTS = frozenset({
    "architect", "threat model", "security", "design", "plan", "strategy",
    "analyse", "analyze", "evaluate", "compare", "trade-off", "tradeoff",
    "decide", "recommend", "review", "assess", "audit", "diagnose",
    "root cause", "investigate", "brainstorm", "proposal",
    # Math / science reasoning — derivations, proofs, and quantitative work.
    "derive", "derivation", "prove", "proof", "theorem", "formula",
    "equation", "calculate", "compute", "solve for", "integral",
    "differentiate", "wavefunction", "quantum", "physics",
})

# ── Complexity keyword signals ────────────────────────────────────────────────

_HARD_KEYWORDS = frozenset({
    "architect", "threat model", "refactor", "migrat",
    "algorithm", "root cause", "security", "audit",
    "system design", "large-scale", "distributed", "race condition",
    "production bug", "incident", "postmortem",
    # Multi-step math/science reasoning — derivations and formal proofs.
    "derive", "derivation", "prove", "proof", "theorem",
})

_MODERATE_KEYWORDS = frozenset({
    "design", "implement", "integrate", "debug", "optimise", "optimize",
    "analyse", "analyze", "test", "review", "plan", "spec",
})

_LEN_MODERATE = 300
_LEN_HARD = 800


def classify(prompt: str) -> tuple[str, str]:
    """Return (domain, complexity) from prompt text alone.

    Callers that need role-based domain priors should override the returned
    domain before calling CapabilityRouter.route().
    """
    lower = prompt.lower()

    # Domain: count keyword hits per domain, pick the clear winner
    coding_hits    = sum(1 for h in _CODING_HINTS    if h in lower)
    docs_hits      = sum(1 for h in _DOCS_HINTS      if h in lower)
    reasoning_hits = sum(1 for h in _REASONING_HINTS if h in lower)

    best = max(coding_hits, docs_hits, reasoning_hits)
    if best >= 2:
        if coding_hits == best > docs_hits and coding_hits > reasoning_hits:
            domain = "coding"
        elif docs_hits == best > coding_hits and docs_hits > reasoning_hits:
            domain = "docs"
        elif reasoning_hits == best > coding_hits and reasoning_hits > docs_hits:
            domain = "reasoning"
        else:
            domain = "general"
    else:
        domain = "general"

    # Complexity: keywords + length
    hard_hits     = sum(1 for kw in _HARD_KEYWORDS     if kw in lower)
    moderate_hits = sum(1 for kw in _MODERATE_KEYWORDS if kw in lower)
    length = len(prompt)

    if hard_hits >= 1 or length >= _LEN_HARD:
        complexity = "hard"
    elif moderate_hits >= 1 or length >= _LEN_MODERATE:
        complexity = "moderate"
    else:
        complexity = "trivial"

    return domain, complexity


# ── Professional-field cues (profile fallback) ─────────────────────────────────
#
# Regex fragments, matched with word boundaries, because substring matching on
# short domain words is actively wrong here: "tax" is inside "syntax" and
# "taxonomy", "dose" is inside "diagnose". Stems are written explicitly as
# `stem\w*` where a family of endings should match.
#
# Each list is deliberately narrow. A cue earns its place only if its presence
# makes the *field* near-certain; ambiguous words that appear in software prompts
# ("contract", "audit", "protocol") are either omitted or qualified into a phrase.
_FIELD_CUES: dict[str, tuple[str, ...]] = {
    "law_regulatory": (
        r"statut\w*", r"regulat\w*", r"jurisdiction\w*", r"complianc\w*",
        r"liabilit\w*", r"litigat\w*", r"case law", r"breach of contract",
        r"contract law", r"gdpr", r"hipaa", r"copyright", r"patent\w*",
    ),
    "medicine_health": (
        r"diagnos\w*", r"dosage", r"dosing", r"mg/kg", r"contraindicat\w*",
        r"patient\w*", r"clinical\w*", r"symptom\w*", r"prescrib\w*",
        r"treatment protocol",
    ),
    "finance_business": (
        r"gaap", r"ifrs", r"valuation", r"ebitda", r"amortiz\w*",
        r"balance sheet", r"cash flow", r"securities", r"tax\b", r"taxes",
    ),
    "natural_science": (
        r"reactor\w*", r"radiation", r"thermodynamic\w*", r"molecul\w*",
        r"chemistr\w*", r"biolog\w*", r"genom\w*", r"astrophys\w*",
        r"materials science", r"isotop\w*",
    ),
    "math_formal": (
        r"theorem\w*", r"lemma\w*", r"proof", r"prove that", r"integral\w*",
        r"differential equation\w*", r"topolog\w*", r"eigen\w*",
    ),
}

_FIELD_RES: dict[str, re.Pattern[str]] = {
    field: re.compile(r"\b(?:" + "|".join(cues) + r")", re.IGNORECASE)
    for field, cues in _FIELD_CUES.items()
}

# Fields where being confidently wrong can lead someone to real harm, versus
# fields where it mostly costs them work. Drives `stakes`, which raises every
# field's bar rather than adding one.
_HIGH_STAKES_FIELDS = frozenset({"law_regulatory", "medicine_health"})
_MEDIUM_STAKES_FIELDS = frozenset({"finance_business", "natural_science"})

# The answer will have to state real specifics exactly — the hallucination axis,
# and the single most useful demand to detect, since it is what the original
# fabricated-regulatory-answer failure was made of.
_PRECISION_RE = re.compile(
    r"\b(?:cite|citation\w*|cited|references?|statut\w*|regulat\w*|"
    r"jurisdiction\w*|standard\w*|complian\w*|\biso \d|\brfc \d|"
    r"section \d|according to)",
    re.IGNORECASE,
)

_QUANTITATIVE_RE = re.compile(
    r"\b(?:calculat\w*|comput\w*|estimat\w*|how many|how much|"
    r"derive|derivation|percentage|probabilit\w*)",
    re.IGNORECASE,
)

# Cap on fields named from cues, matching taxonomy's own limit on how many
# fields a profile may name.
_MAX_CUE_FIELDS = 2


def classify_profile(prompt: str) -> PromptProfile:
    """Build a PromptProfile from prompt text alone — the no-LLM fallback.

    The legacy (domain, complexity) heuristic supplies the primary field and the
    depth. On top of that, professional-field cues can add up to two more fields
    at the same depth, and set stakes/demands. That combination is what stops a
    cheap coding-specialist from winning a regulatory prompt: even though the
    depth estimate is crude, naming `law_regulatory` at all means the model has
    to clear a bar there too.
    """
    domain, complexity = classify(prompt)
    base = profile_from_labels(domain, complexity)
    primary = base.domains[0]

    # Depth for cue-detected fields: reuse the primary's depth, but never below
    # practitioner. A prompt that reaches into medicine or law at all is past the
    # point where "any well-informed generalist" is the right answer.
    depth = primary.depth
    if DEPTH_RANK.get(depth, 0) < DEPTH_RANK["practitioner"]:
        depth = "practitioner"

    lower = prompt.lower()
    needs = [primary]
    hit_fields: list[str] = []
    for field, pattern in _FIELD_RES.items():
        if len(hit_fields) >= _MAX_CUE_FIELDS:
            break
        if pattern.search(lower):
            hit_fields.append(field)
            if field != primary.field:
                needs.append(DomainNeed(field=field, depth=depth))

    demands: set[str] = set()
    if _PRECISION_RE.search(lower):
        demands.add("factual_precision")
    if _QUANTITATIVE_RE.search(lower):
        demands.add("quantitative")
    if len(prompt) >= _LEN_HARD:
        demands.add("long_synthesis")

    stakes = "low"
    if any(f in _HIGH_STAKES_FIELDS for f in hit_fields):
        stakes = "high"
    elif any(f in _MEDIUM_STAKES_FIELDS for f in hit_fields):
        stakes = "medium"

    return PromptProfile(
        domains=tuple(needs), demands=frozenset(demands), stakes=stakes
    )
