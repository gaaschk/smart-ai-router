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
    type: str  # "str" | "int" | "bool"
    default: Any
    label: str
    group: str
    help: str = ""
    sensitive: bool = False  # flagged in the UI (e.g. toggles code execution)


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
        key="classifier_model",
        env="SMART_ROUTER_CLASSIFIER_MODEL",
        type="str",
        default="llama3.1:8b",
        label="Classifier model",
        group="Classifier",
        help="Local model used to classify prompt domain/complexity.",
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
            return str(int(value))
        except (TypeError, ValueError):
            raise ValueError(f"{key} expects an integer")
    # str
    return str(value)


def apply(updates: dict[str, Any]) -> None:
    """Persist a batch of setting updates (validated). Requires a bound store."""
    if _store is None:
        raise RuntimeError("settings store not bound")
    for key, value in updates.items():
        _store.set_setting(key, normalize(key, value))
