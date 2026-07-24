"""Per-user model scope — which models a key is allowed to route to.

A key's scope has two dimensions, both optional:

  * allow/deny substrings (from the key's `scope_models` JSON) matched
    case-insensitively against a model's `value` and `provider`. `allow`
    is a whitelist (empty = allow all); `deny` is a blacklist that overrides.
  * a cost-tier ceiling (`max_tier`): models whose cost tier exceeds it are
    out of scope. 0 means "no ceiling".

Scope is enforced inside the router's eligibility filter, so it applies to the
fallback pick too — a scoped user gets the best model *within their scope*,
never a model outside it.

`scope_models` JSON shape (all fields optional):
    {"allow": ["openrouter/", "ollama/"], "deny": ["claude", "bedrock/"]}
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from smart_ai_router.models import ModelSpec


@dataclass(frozen=True)
class ModelScope:
    allow: tuple[str, ...] = ()      # lowercase substrings; empty = allow all
    deny: tuple[str, ...] = ()       # lowercase substrings; overrides allow
    max_tier: int = 0                # cost-tier ceiling; 0 = no ceiling

    @property
    def is_restricted(self) -> bool:
        return bool(self.allow or self.deny or self.max_tier)

    def permits(self, spec: ModelSpec) -> bool:
        """True if `spec` is within this scope."""
        hay = f"{spec.value} {spec.provider}".lower()
        if self.allow and not any(a in hay for a in self.allow):
            return False
        if self.deny and any(d in hay for d in self.deny):
            return False
        if self.max_tier and spec.cost > self.max_tier:
            return False
        return True


def _clean(items) -> tuple[str, ...]:
    """Normalize a JSON list into lowercase, non-empty substrings."""
    if not isinstance(items, list):
        return ()
    return tuple(str(x).strip().lower() for x in items if str(x).strip())


def parse_scope(scope_models: str = "", max_tier: int = 0) -> ModelScope:
    """Build a ModelScope from a key's stored `scope_models` JSON + `max_tier`.

    Tolerant of empty/malformed JSON — a bad value yields an unrestricted scope
    on the allow/deny axis rather than locking a user out. `max_tier` is applied
    regardless of whether the JSON parses.
    """
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    raw = (scope_models or "").strip()
    if raw:
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                allow = _clean(obj.get("allow"))
                deny = _clean(obj.get("deny"))
        except (ValueError, TypeError):
            pass  # malformed → no allow/deny restriction (max_tier still applies)
    return ModelScope(allow=allow, deny=deny, max_tier=max(0, int(max_tier or 0)))
