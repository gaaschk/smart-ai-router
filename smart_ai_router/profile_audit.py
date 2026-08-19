"""
Profile audit — replay real routing decisions against a proposed set of model
profiles and report which ones would change.

Why replay instead of eyeballing scores
───────────────────────────────────────
A model profile is not interesting on its own; only its effect on routing is. A
rating that drops qwen3-coder's law score by 0.10 is worth nothing if no prompt
ever asked it a legal question, and is a significant change if it re-routes a
tenth of traffic. Reading a diff of 16 floats per model cannot tell those apart,
and neither can a synthetic test prompt someone wrote to make a change look good.

So the audit takes the prompt profiles this deployment has *actually routed*
(usage_log.profile_json, recorded by the proxy), re-runs the real selection
function against both the current and the proposed model profiles, and reports
the flips — weighted by how often each prompt profile occurred.

That makes "does this enrichment help?" an answerable question: it shows which
prompts move, to which model, and whether each move buys qualification (the
router previously had to admit no model cleared the bar) or spends money.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from smart_ai_router import router as _router
from smart_ai_router.models import ModelSpec
from smart_ai_router.taxonomy import normalize_profile


@dataclass
class Flip:
    """One prompt profile that routes differently under the proposed profiles."""

    described_as: str        # PromptProfile.describe() — human-readable demand
    requests: int            # how many logged requests carried this profile
    before_model: str
    after_model: str
    before_qualified: bool
    after_qualified: bool
    before_cost: int         # cost tier of each pick, so a flip can be read as
    after_cost: int          # cheaper / pricier at a glance

    def direction(self) -> str:
        """What this flip buys, in one word.

        `qualifies` and `unqualifies` come first because they are the changes that
        matter: the router going from "no model clears this bar" to a genuine
        match is the entire point of better profiles, and the reverse is the
        regression to watch for. Cost is only the story when qualification is
        unchanged.
        """
        if self.after_qualified and not self.before_qualified:
            return "qualifies"
        if self.before_qualified and not self.after_qualified:
            return "unqualifies"
        if self.after_cost > self.before_cost:
            return "pricier"
        if self.after_cost < self.before_cost:
            return "cheaper"
        return "lateral"


@dataclass
class AuditResult:
    """Aggregate effect of a profile change on real traffic."""

    profiles: int = 0        # distinct prompt profiles replayed
    requests: int = 0        # logged requests they represent
    flipped: int = 0         # distinct profiles whose pick changed
    flipped_requests: int = 0  # requests those represent — the number that matters
    flips: list[Flip] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "profiles": self.profiles,
            "requests": self.requests,
            "flipped": self.flipped,
            "flipped_requests": self.flipped_requests,
            "flips": [
                {
                    "profile": f.described_as,
                    "requests": f.requests,
                    "before": {
                        "model": f.before_model,
                        "qualified": f.before_qualified,
                        "cost": f.before_cost,
                    },
                    "after": {
                        "model": f.after_model,
                        "qualified": f.after_qualified,
                        "cost": f.after_cost,
                    },
                    "direction": f.direction(),
                }
                for f in self.flips
            ],
            "errors": list(self.errors),
        }


def audit_profiles(
    *,
    recorded: list[dict],
    before: list[ModelSpec],
    after: list[ModelSpec],
) -> AuditResult:
    """Compare routing under `before` vs `after` model profiles.

    Args:
        recorded: Rows from MatrixStore.usage_profiles() — each
                  {"profile": <dict>, "routed_model": str, "requests": int}.
        before:   The model set as it stands today.
        after:    The proposed model set (same models, refined profiles).

    Both sides go through router.select_from(), the same function that serves
    live requests, so a flip reported here is a flip that would really happen —
    including every filter and tiebreak. Requests recorded with a profile the
    current taxonomy no longer understands are skipped and counted in `errors`,
    not silently dropped.

    Deliberately does not reconstruct needs_tools / needs_vision / est_tokens: the
    usage log doesn't record them, and inventing values would produce flips that
    depend on a guess. This measures the capability decision, which is the one the
    profiles affect.
    """
    result = AuditResult()
    costs_before = {s.value: s.cost for s in before}
    costs_after = {s.value: s.cost for s in after}

    # Distinct profiles, not distinct (profile, model) rows: the same demand may
    # appear under several past picks (a model came or went, an exclude fired),
    # and replaying it once per row would over-count the traffic it represents.
    weights: dict[str, int] = {}
    profiles: dict[str, dict] = {}
    for row in recorded:
        raw = row.get("profile")
        if not isinstance(raw, dict):
            continue
        key = repr(sorted(raw.items(), key=lambda kv: kv[0]))
        profiles[key] = raw
        weights[key] = weights.get(key, 0) + int(row.get("requests") or 0)

    for key, raw in profiles.items():
        prompt_profile = normalize_profile(raw)
        if prompt_profile is None:
            result.errors.append(f"unroutable recorded profile: {raw}")
            continue
        requests = weights.get(key, 0)
        result.profiles += 1
        result.requests += requests
        try:
            d_before = _router.select_from(before, profile=prompt_profile)
            d_after = _router.select_from(after, profile=prompt_profile)
        except RuntimeError as exc:
            # No eligible model at all on one side — a matrix problem, not a
            # profile problem, and not something to report as a flip.
            result.errors.append(f"{prompt_profile.describe()}: {exc}")
            continue
        if d_before.model == d_after.model and d_before.qualified == d_after.qualified:
            continue
        result.flipped += 1
        result.flipped_requests += requests
        result.flips.append(
            Flip(
                described_as=prompt_profile.describe(),
                requests=requests,
                before_model=d_before.model,
                after_model=d_after.model,
                before_qualified=d_before.qualified,
                after_qualified=d_after.qualified,
                before_cost=costs_before.get(d_before.model, 0),
                after_cost=costs_after.get(d_after.model, 0),
            )
        )

    # Most-trafficked flips first: with hundreds of distinct profiles, the ones
    # worth a human's attention are the ones that happen a lot.
    result.flips.sort(key=lambda f: (-f.requests, f.described_as))
    return result
