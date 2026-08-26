"""Runtime settings — UI-managed application behavior with env fallback.

The principle (Kevin, 2026-07): env vars are only for values intrinsic to a
specific machine/deployment (port, filesystem paths, launchd label, and the
bootstrap admin secret SMART_ROUTER_API_KEYS which gates the admin UI itself).
Everything that is application *behavior/policy* lives here — persisted in the
DB, editable from the Settings page, applied live with no restart.

Read precedence for every setting: **DB value → env var → default.** So an
existing deployment that set an env var keeps working unchanged, and a value
saved in the UI takes precedence the moment it lands (values are read uncached).

Tests that don't bind a store still get env → default resolution, so nothing
that relied on env vars breaks.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

from smart_ai_router.store.base import MatrixStore


@dataclass(frozen=True)
class SettingSpec:
    """One tunable: its canonical key, env fallback, type, default, and UI metadata."""

    key: str
    env: str
    type: str  # "str" | "int" | "float" | "bool"
    default: Any
    label: str
    group: str
    help: str = ""
    sensitive: bool = False  # flagged in the UI (e.g. toggles code execution)
    # Optional extra check on an incoming value, beyond its type. Raises
    # ValueError (→ 422) to reject. For values that are well-typed but wrong in a
    # way the type system can't see — a word that means something for every
    # *other* setting in the group but not this one.
    validate: Callable[[str], None] | None = None


# The word every *other* helper-model setting uses for "route it". Defined here
# rather than imported from helper_models to keep settings dependency-free.
AUTO_WORD = "auto"


def _reject_auto_triage(value: str) -> None:
    """Refuse `auto` for the triage classifier, with the reason.

    Every other model setting in the Classifier group takes `auto` to mean "route
    it like any other prompt", so typing it here is the obvious move and it would
    fail in the worst way: `auto` is not a model, so every request's profiling
    call would 404 and fall through to the keyword classifier, silently.

    Triage is excluded from routing on purpose, and the reason is measured rather
    than stylistic — see helper_models.py, "Why triage is not a HelperTask".
    """
    if value.strip().lower() == AUTO_WORD:
        raise ValueError(
            "classifier_model does not accept 'auto': routing picks the "
            "highest-scoring free local model, which is measured to be the worst "
            "triage model available (see helper_models.py). Name a model, or "
            "leave it empty to disable local triage."
        )


def _reject_negative(value: str) -> None:
    """Refuse a negative number for a cap/ceiling.

    Caps are the only thing standing between anonymous traffic and the operator's
    bill, and a negative one reads as "no limit" in a naive comparison. Rejecting
    it here means the failure is a 422 on the Settings page rather than a cap that
    silently never triggers.
    """
    try:
        if float(value) < 0:
            raise ValueError("must be zero or greater")
    except ValueError as exc:
        raise ValueError(f"expects a number of zero or greater ({exc})") from None


# The registry. Order here is the order the UI renders. Keep keys stable — they
# are the DB primary keys and the JSON field names in the settings API.
SPECS: tuple[SettingSpec, ...] = (
    SettingSpec(
        key="model_denylist",
        env="SMART_ROUTER_MODEL_DENYLIST",
        type="str",
        default="",
        label="Model denylist",
        group="Routing",
        help="Comma-separated substrings; any model whose id contains one is "
        "excluded from routing (case-insensitive). e.g. mxfp8",
    ),
    SettingSpec(
        key="agent_denylist",
        env="SMART_ROUTER_AGENT_DENYLIST",
        type="str",
        default="",
        label="Agent-mode denylist",
        group="Routing",
        help="Comma-separated substrings excluded specifically when agent mode "
        "is active (in addition to the model denylist).",
    ),
    SettingSpec(
        key="default_max_tokens",
        env="SMART_ROUTER_DEFAULT_MAX_TOKENS",
        type="int",
        default=4096,
        label="Default max output tokens",
        group="Routing",
        help="Output-token ceiling applied when a request omits max_tokens. "
        "Caps output only — for reasoning models this covers thinking plus the "
        "answer, so too low a value yields empty or truncated replies.",
        validate=_reject_negative,
    ),
    SettingSpec(
        key="long_form_max_tokens",
        env="SMART_ROUTER_LONG_FORM_MAX_TOKENS",
        type="int",
        default=32768,
        label="Long-form max output tokens",
        group="Routing",
        help="Output ceiling for a prompt whose answer is a document — a story, "
        "a guide, a lesson, a translation. The ordinary default is sized for a "
        "reply; asking for a short story and getting one paragraph, cut "
        "mid-sentence, is that default doing exactly what it was set to do. "
        "Generous on purpose: this is a ceiling, not a target, and you are billed "
        "for what the model actually writes. Automatically lowered to the chosen "
        "model's own output limit, so a large value here can't produce a request "
        "the provider rejects.",
        validate=_reject_negative,
    ),
    SettingSpec(
        key="long_form_min_model_output",
        env="SMART_ROUTER_LONG_FORM_MIN_MODEL_OUTPUT",
        type="int",
        default=8192,
        label="Long-form minimum model capacity",
        group="Routing",
        help="For a document request, prefer a model that can emit at least this "
        "many tokens. Some cheap models cap output at 2–4k no matter what we ask "
        "for, and cheapest-qualified-wins would hand them a story they physically "
        "cannot finish. A preference, not a filter: if no roomier model qualifies, "
        "the cheapest qualified one still answers rather than the request failing. "
        "Set to 0 to rank documents on price alone.",
        validate=_reject_negative,
    ),
    SettingSpec(
        key="chat_rich_output_prompt",
        env="SMART_ROUTER_CHAT_RICH_OUTPUT_PROMPT",
        type="str",
        default=(
            "You are answering in a chat UI that renders your reply as Markdown, "
            "and additionally renders fenced ```html and ```svg blocks as live, "
            "sandboxed previews the reader can open, download, or print. Use them "
            "when a visual genuinely helps — diagrams, charts, tables, timelines, "
            "styled documents — and write self-contained markup with inline CSS. "
            "Sandboxed previews cannot load anything over the network, so draw "
            "with inline SVG or CSS rather than linking to external images, "
            "fonts, or scripts. When asked for a document — a story, a guide, a "
            "lesson — write the whole thing, at the length the request implies, "
            "rather than an outline or an excerpt."
        ),
        label="Chat rich-output prompt",
        group="Routing",
        help="System note prepended to requests from the chat page only, so the "
        "model knows the page renders HTML and SVG previews. Never sent to /v1 API "
        "clients or tool-using requests, where an injected turn would change their "
        "output and then persist in their history. Blank it to send nothing.",
    ),
    SettingSpec(
        key="classifier_model",
        env="SMART_ROUTER_CLASSIFIER_MODEL",
        type="str",
        default="llama3.1:8b",
        label="Classifier model",
        group="Classifier",
        help="Local model used to profile each prompt (which fields it needs and "
        "how deep). Prefer a small non-reasoning instruct model: thinking models "
        "burn the classifier's tiny output budget before emitting the JSON. "
        "Pinned rather than routed (unlike the refine model below) on purpose — "
        "this runs on every request, so changing which model does triage needs a "
        "measured comparison first (scripts/bakeoff_classifier.py). Does not "
        "accept `auto`: routing would pick the highest-scoring free local model, "
        "which is measured to be the worst triage model available.",
        validate=_reject_auto_triage,
    ),
    SettingSpec(
        key="classifier_fallback",
        env="SMART_ROUTER_CLASSIFIER_FALLBACK",
        type="str",
        default="nvidia/nemotron-nano-9b-v2:free",
        label="Classifier fallback model",
        group="Classifier",
        help="Used when the primary classifier model is unavailable.",
    ),
    SettingSpec(
        key="classifier_refine_model",
        env="SMART_ROUTER_CLASSIFIER_REFINE_MODEL",
        type="str",
        default="auto",
        label="Classifier refine model",
        group="Classifier",
        help="Second-pass profiler, used only when the local classifier reports "
        "high stakes, two or more specialist-depth fields, or frontier depth — "
        "the judgments a small local model gets wrong expensively. Runs only on prompts "
        "already headed for a costly model. `auto` routes it like any other "
        "prompt — cheapest model that clears frontier depth and honors a JSON "
        "schema. A model name pins it instead. Empty disables the second pass.",
    ),
    SettingSpec(
        key="model_profiler_model",
        env="SMART_ROUTER_MODEL_PROFILER_MODEL",
        type="str",
        default="auto",
        label="Model profiler model",
        group="Model profiling",
        help="Model asked to judge what each catalog model is good and bad at, "
        "refining the deterministic profile sync computes from benchmarks. Runs "
        "when you press Refine on the Models page, and after a sync for models it "
        "just added — never on a request path. `auto` routes it like any other "
        "prompt — cheapest model that clears specialist depth and honors a JSON "
        "schema. A model name pins it instead. Empty disables refinement "
        "entirely.",
    ),
    SettingSpec(
        key="model_profiler_limit",
        env="SMART_ROUTER_MODEL_PROFILER_LIMIT",
        type="int",
        default=40,
        label="Models per refine run",
        group="Model profiling",
        help="Ceiling on how many models one Refine run profiles. The catalog "
        "holds hundreds, so runs are bounded and resumable: cheapest models are "
        "profiled first, because the router picks the cheapest qualifying model "
        "and an overstated cheap profile is what wins prompts it shouldn't.",
    ),
    SettingSpec(
        key="model_profiler_on_sync",
        env="SMART_ROUTER_MODEL_PROFILER_ON_SYNC",
        type="bool",
        default=True,
        label="Profile new models on sync",
        group="Model profiling",
        help="After a sync, profile the models it just added (and any whose "
        "vendor description was rewritten), so a new model never routes on a "
        "cue-table guess for long. Bounded by the per-run ceiling above and "
        "billed the same way — one short call per model. Models that only "
        "changed price or benchmark scores are not re-profiled: the stored "
        "judgment is relative, so it survives a new level for free.",
    ),
    SettingSpec(
        key="max_file_mb",
        env="SMART_ROUTER_MAX_FILE_MB",
        type="int",
        default=512,
        label="Max upload size (MB)",
        group="Files",
        help="Largest file accepted by the upload endpoint.",
    ),
    SettingSpec(
        key="ocr_max_pages",
        env="SMART_ROUTER_OCR_MAX_PAGES",
        type="int",
        default=10,
        label="OCR max pages",
        group="Files",
        help="Maximum PDF pages rasterized for OCR text extraction.",
    ),
    SettingSpec(
        key="ocr_dpi",
        env="SMART_ROUTER_OCR_DPI",
        type="int",
        default=150,
        label="OCR DPI",
        group="Files",
        help="Rasterization resolution for OCR; higher is slower but sharper.",
    ),
    SettingSpec(
        key="bash_timeout_s",
        env="SMART_ROUTER_BASH_TIMEOUT_S",
        type="int",
        default=30,
        label="Bash tool timeout (s)",
        group="Agent tools",
        help="Wall-clock limit for a single sandboxed bash command.",
    ),
    SettingSpec(
        key="enable_bash",
        env="SMART_ROUTER_ENABLE_BASH",
        type="bool",
        default=False,
        label="Enable sandboxed bash tool",
        group="Agent tools",
        help="Allow agent mode to execute shell commands in the sandbox. "
        "Security-sensitive: this turns on code execution.",
        sensitive=True,
    ),
    # ── Public access ───────────────────────────────────────────────────────────
    # An anonymous visitor spends the operator's money, so every setting here is a
    # ceiling and the feature ships off. See public_access.py for the policy these
    # values feed, and why a *degraded* ceiling beats refusing service.
    SettingSpec(
        key="public_chat_enabled",
        env="SMART_ROUTER_PUBLIC_CHAT",
        type="bool",
        default=False,
        label="Allow anonymous chat",
        group="Public access",
        help="Let visitors without an API key use the chat page. The OpenAI API "
        "(/v1) still requires a key — anonymous requests are accepted only from "
        "the chat UI itself (same-origin, with a session cookie). Anonymous "
        "users can never use agent mode, upload files, or see admin pages. "
        "Security-sensitive: this exposes your router, and your bill, to the "
        "public internet.",
        sensitive=True,
    ),
    SettingSpec(
        key="public_daily_budget_usd",
        env="SMART_ROUTER_PUBLIC_DAILY_BUDGET",
        type="float",
        default=1.00,
        label="Anonymous daily spend cap (USD)",
        group="Public access",
        help="Ceiling on what all anonymous traffic may cost per UTC day. Past "
        "it, anonymous users are limited to free and local models rather than "
        "cut off. Set 0 to allow no paid spend at all (free/local only).",
        validate=_reject_negative,
    ),
    SettingSpec(
        key="public_max_tier",
        env="SMART_ROUTER_PUBLIC_MAX_TIER",
        type="int",
        default=3,
        label="Anonymous max cost tier",
        group="Public access",
        help="Most expensive cost tier anonymous traffic may reach while budget "
        "remains (0 = local only, 1 = adds free models, 3 ≈ Haiku, 5 ≈ Sonnet, "
        "8 ≈ Opus). Kept low so no single conversation can drain the daily cap.",
        validate=_reject_negative,
    ),
    SettingSpec(
        key="public_degraded_max_tier",
        env="SMART_ROUTER_PUBLIC_DEGRADED_MAX_TIER",
        type="int",
        default=1,
        label="Anonymous max tier once budget is spent",
        group="Public access",
        help="Tier ceiling applied after the daily cap is reached. 1 keeps the "
        "site working on free and local models; 0 restricts it to local only.",
        validate=_reject_negative,
    ),
    SettingSpec(
        key="public_max_output_tokens",
        env="SMART_ROUTER_PUBLIC_MAX_OUTPUT_TOKENS",
        type="int",
        default=16384,
        label="Anonymous max output tokens",
        group="Public access",
        help="Hard ceiling on max_tokens for an anonymous request. This is what "
        "bounds how far concurrent requests can overshoot the daily cap, since a "
        "call's real cost is only known after it returns. Deliberately roomy — a "
        "stranger asking for a story should get a whole one, and what actually "
        "bounds the bill is the daily budget, the rate limit, and the tier ceiling "
        "rather than a ceiling that truncates every long answer. Lower it if you "
        "would rather cut replies off than risk a single expensive call.",
        validate=_reject_negative,
    ),
    SettingSpec(
        key="public_rl_max_req",
        env="SMART_ROUTER_PUBLIC_RL_MAX_REQ",
        type="int",
        default=30,
        label="Anonymous requests per window",
        group="Public access",
        help="Per-IP request cap inside the rate-limit window (0 = no cap). The "
        "IP is the real defense: a session cookie is trivially discarded.",
        validate=_reject_negative,
    ),
    SettingSpec(
        key="public_rl_window_s",
        env="SMART_ROUTER_PUBLIC_RL_WINDOW_S",
        type="int",
        default=3600,
        label="Anonymous rate-limit window (s)",
        group="Public access",
        help="Length of the rolling window for anonymous rate limits.",
        validate=_reject_negative,
    ),
    SettingSpec(
        key="public_max_concurrent",
        env="SMART_ROUTER_PUBLIC_MAX_CONCURRENT",
        type="int",
        default=4,
        label="Anonymous concurrent requests",
        group="Public access",
        help="How many anonymous requests may be in flight at once across the "
        "whole deployment (0 = unlimited). Protects the local GPU from being "
        "monopolized, and bounds budget overshoot.",
        validate=_reject_negative,
    ),
    # ── Self-serve accounts ─────────────────────────────────────────────────────
    # Keys anyone can mint for themselves, with no personal information collected.
    # Read the "pool" cap as the actual bill ceiling: signing up is free, so a
    # per-account cap alone buys an abuser N accounts × N caps. See
    # self_signup.py. Ships off.
    SettingSpec(
        key="self_signup_enabled",
        env="SMART_ROUTER_SELF_SIGNUP",
        type="bool",
        default=False,
        label="Let visitors create their own API keys",
        group="Self-serve accounts",
        help="Adds a button that mints an API key on the spot — no email, no "
        "name, nothing to verify. A self-issued key can never use agent mode or "
        "manage anything; it is capped by the two budgets below. Requires an "
        "admin key (SMART_ROUTER_API_KEYS) to be configured. "
        "Security-sensitive: this lets strangers spend your money.",
        sensitive=True,
    ),
    SettingSpec(
        key="self_signup_pool_daily_budget_usd",
        env="SMART_ROUTER_SIGNUP_POOL_DAILY_BUDGET",
        type="float",
        default=2.00,
        label="All self-serve accounts: daily spend cap (USD)",
        group="Self-serve accounts",
        help="THE bill ceiling — what every self-issued key together may cost per "
        "UTC day. Past it they are limited to free and local models rather than "
        "cut off. This is the number that matters: the per-account cap below does "
        "not bound your bill, because anyone can create more accounts.",
        validate=_reject_negative,
    ),
    SettingSpec(
        key="self_signup_daily_budget_usd",
        env="SMART_ROUTER_SIGNUP_DAILY_BUDGET",
        type="float",
        default=0.25,
        label="Per self-serve account: daily spend cap (USD)",
        group="Self-serve accounts",
        help="What one self-issued key may cost per UTC day, so a single heavy "
        "user can't drain the pool above and leave nothing for anyone else. "
        "Fairness between accounts, not protection from them.",
        validate=_reject_negative,
    ),
    SettingSpec(
        key="self_signup_max_tier",
        env="SMART_ROUTER_SIGNUP_MAX_TIER",
        type="int",
        default=3,
        label="Self-serve max cost tier",
        group="Self-serve accounts",
        help="Most expensive cost tier a self-issued key may reach while budget "
        "remains (0 = local only, 1 = adds free models, 3 ≈ Haiku, 5 ≈ Sonnet, "
        "8 ≈ Opus). Read live, so lowering it applies to keys that already exist.",
        validate=_reject_negative,
    ),
    SettingSpec(
        key="self_signup_degraded_max_tier",
        env="SMART_ROUTER_SIGNUP_DEGRADED_MAX_TIER",
        type="int",
        default=1,
        label="Self-serve max tier once budget is spent",
        group="Self-serve accounts",
        help="Tier ceiling applied once either daily cap above is reached. 1 keeps "
        "these accounts working on free and local models; 0 restricts them to local.",
        validate=_reject_negative,
    ),
    SettingSpec(
        key="self_signup_max_output_tokens",
        env="SMART_ROUTER_SIGNUP_MAX_OUTPUT_TOKENS",
        type="int",
        default=16384,
        label="Self-serve max output tokens",
        group="Self-serve accounts",
        help="Hard ceiling on max_tokens for a self-issued key's request. This is "
        "what bounds how far concurrent requests can overshoot the daily caps, "
        "since a call's real cost is only known after it returns. Roomy enough for "
        "a whole document, because truncating one to save a fraction of a cent is "
        "a bad trade; the daily caps are what bound the bill.",
        validate=_reject_negative,
    ),
    SettingSpec(
        key="self_signup_rl_max_req",
        env="SMART_ROUTER_SIGNUP_RL_MAX_REQ",
        type="int",
        default=60,
        label="Self-serve requests per window",
        group="Self-serve accounts",
        help="Request cap per key inside the window below (0 = no cap). Baked into "
        "each key when it is created, so changing it affects new keys only.",
        validate=_reject_negative,
    ),
    SettingSpec(
        key="self_signup_rl_window_s",
        env="SMART_ROUTER_SIGNUP_RL_WINDOW_S",
        type="int",
        default=3600,
        label="Self-serve rate-limit window (s)",
        group="Self-serve accounts",
        help="Length of the rolling window for a self-issued key's rate limit. "
        "0 disables its rate limit entirely.",
        validate=_reject_negative,
    ),
    SettingSpec(
        key="self_signup_max_accounts",
        env="SMART_ROUTER_SIGNUP_MAX_ACCOUNTS",
        type="int",
        default=100,
        label="Maximum self-serve accounts",
        group="Self-serve accounts",
        help="How many self-issued keys may exist at once (0 = unlimited). "
        "Creating one is free and scriptable, so this is how you say 'I'll take "
        "fifty users, not five thousand'.",
        validate=_reject_negative,
    ),
)

_BY_KEY: dict[str, SettingSpec] = {s.key: s for s in SPECS}

# The store is bound once at app startup. Unbound (e.g. in unit tests that only
# exercise env fallback) resolution skips the DB layer entirely.
_store: MatrixStore | None = None


def bind_store(store: MatrixStore | None) -> None:
    """Point the settings layer at the live store (called from create_app)."""
    global _store
    _store = store


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _coerce(spec: SettingSpec, raw: str) -> Any:
    """Parse a stored/env string into the spec's type, falling back to the
    default on malformed input rather than raising into a request path."""
    if spec.type == "bool":
        return raw.strip().lower() in _TRUE
    if spec.type == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return spec.default
    if spec.type == "float":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return spec.default
    return raw


def _raw(key: str) -> str | None:
    """Resolve the raw string for a key via DB → env, or None if neither set."""
    spec = _BY_KEY[key]
    if _store is not None:
        db_val = _store.get_setting(key)
        if db_val is not None:
            return db_val
    env_val = os.environ.get(spec.env)
    if env_val is not None:
        return env_val
    return None


def get(key: str) -> Any:
    """Typed value for a setting: DB → env → default."""
    spec = _BY_KEY[key]
    raw = _raw(key)
    if raw is None:
        return spec.default
    return _coerce(spec, raw)


def get_str(key: str) -> str:
    return str(get(key))


def get_int(key: str) -> int:
    return int(get(key))


def get_bool(key: str) -> bool:
    return bool(get(key))


def source(key: str) -> str:
    """Where the effective value comes from: 'db', 'env', or 'default'.

    Used by the settings API so the UI can show whether a value is overriding
    an env var or still falling back."""
    spec = _BY_KEY[key]
    if _store is not None and _store.get_setting(key) is not None:
        return "db"
    if os.environ.get(spec.env) is not None:
        return "env"
    return "default"


def effective() -> list[dict]:
    """All settings with their spec metadata + current value + source, for the
    UI form and the GET endpoint."""
    out: list[dict] = []
    for spec in SPECS:
        out.append(
            {
                "key": spec.key,
                "label": spec.label,
                "group": spec.group,
                "help": spec.help,
                "type": spec.type,
                "value": get(spec.key),
                "default": spec.default,
                "env": spec.env,
                "source": source(spec.key),
                "sensitive": spec.sensitive,
            }
        )
    return out


def normalize(key: str, value: Any) -> str:
    """Validate + serialize an incoming API value to its stored string form.

    Raises ValueError if the key is unknown or the value doesn't fit the type.
    """
    spec = _BY_KEY.get(key)
    if spec is None:
        raise ValueError(f"unknown setting {key!r}")
    if spec.type == "bool":
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
            return "true" if value.strip().lower() in _TRUE else "false"
        raise ValueError(f"{key} expects a boolean")
    if spec.type == "int":
        try:
            text = str(int(value))
        except (TypeError, ValueError):
            raise ValueError(f"{key} expects an integer")
    elif spec.type == "float":
        try:
            text = repr(float(value))
        except (TypeError, ValueError):
            raise ValueError(f"{key} expects a number")
    else:
        text = str(value)
    # Applied to every type, not just str: a value can be well-typed and still
    # wrong (a negative spend cap, `auto` for the triage model).
    if spec.validate is not None:
        spec.validate(text)
    return text


def apply(updates: dict[str, Any]) -> None:
    """Persist a batch of setting updates (validated). Requires a bound store."""
    if _store is None:
        raise RuntimeError("settings store not bound")
    for key, value in updates.items():
        _store.set_setting(key, normalize(key, value))
