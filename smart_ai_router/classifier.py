"""
Role-agnostic prompt classifier.

classify(prompt) -> (domain, complexity)

No role knowledge — callers that need role-based priors should build their
own hint before calling CapabilityRouter.route(). This helper exists for
callers (e.g. a proxy) that only have raw prompt text.

Domain:     "coding" | "docs" | "reasoning" | "general"
Complexity: "trivial" | "moderate" | "hard"
"""
from __future__ import annotations

import re

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
