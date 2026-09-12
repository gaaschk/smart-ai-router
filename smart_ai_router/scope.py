"""Per-user model scope — which models a key is allowed to route to.

A key's scope has two dimensions, both optional:

  * allow/deny substrings (from the key's `scope_models` JSON) matched
    case-insensitively against a model's `value` and `provider`. `allow`
    is a whitelist (empty = allow all); `deny` is a blacklist that overrides.
  * a cost-tier ceiling (`max_tier`): models whose cost tier exceeds it are
    out of scope. **None means "no ceiling"; 0 is a real ceiling** that admits
    only tier-0 (local) models.

That distinction exists because a ceiling of zero is a coherent, and desirable,
policy — "answer from my own hardware, spend nothing" — while a sentinel that
reads 0 as *unlimited* turns the most cautious-looking configuration into the
most expensive one. Stored `ApiKey.max_tier` keeps its original meaning (0 =
unset = no ceiling, which is what every existing key has); `parse_scope` is the
one place that translation happens.

Scope is enforced inside the router's eligibility filter, so it applies to the
fallback pick too — a scoped user gets the best model *within their scope*,
never a model outside it.

`scope_models` JSON shape (all fields optional):
    {"allow": ["openrouter/", "ollama/"], "deny": ["claude", "bedrock/"]}
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace

from smart_ai_router.models import ModelSpec


@dataclass(frozen=True)
class ModelScope:
    allow: tuple[str, ...] = ()      # lowercase substrings; empty = allow all
    deny: tuple[str, ...] = ()       # lowercase substrings; overrides allow
    max_tier: int | None = None      # cost-tier ceiling; None = no ceiling, 0 = local only

    @property
    def is_restricted(self) -> bool:
        return bool(self.allow or self.deny) or self.max_tier is not None

    def capped_at(self, max_tier: int | None) -> "ModelScope":
        """This scope with its tier ceiling tightened to the stricter of the two.

        Only the tier axis, deliberately. allow/deny are substrings, and there is
        no honest way to intersect two substring lists — the intersection of
        ["claude"] and ["opus"] as *sets of models* is not expressible as
        substrings at all, so any answer would either invent a restriction or drop
        one. A tier is a number, so "stricter of the two" is unambiguous, and it
        is the axis that costs money.

        None means "no ceiling from that side", so the other one wins outright.
        Never loosens: the result admits no model that either input excluded.
        """
        if max_tier is None:
            return self
        ceiling = max_tier if self.max_tier is None else min(self.max_tier, max_tier)
        return replace(self, max_tier=ceiling)

    def permits(self, spec: ModelSpec) -> bool:
        """True if `spec` is within this scope."""
        hay = f"{spec.value} {spec.provider}".lower()
        if self.allow and not any(a in hay for a in self.allow):
            return False
        if self.deny and any(d in hay for d in self.deny):
            return False
        if self.max_tier is not None and spec.cost > self.max_tier:
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

    Stored keys use 0 (the column default) for "no ceiling", so a non-positive
    value becomes None here. A caller wanting a real tier-0 ceiling builds the
    ModelScope directly — see public_access.anon_scope.
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
    try:
        tier = int(max_tier or 0)
    except (TypeError, ValueError):
        tier = 0
    return ModelScope(allow=allow, deny=deny, max_tier=tier if tier > 0 else None)
