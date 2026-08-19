"""Persistence of LLM profile ratings — the round-trip and re-sync invariants.

These are the tests that keep the two-column design honest. The store composes
ratings onto the rules baseline on *read* and writes only the baseline back, so
a profile must never drift by being read and written repeatedly, and a sync must
never silently discard a judgment someone paid for.
"""
from __future__ import annotations

import pytest

from smart_ai_router.models import ModelSpec, UsageRecord
from smart_ai_router.profiler import baseline_profile, legacy_competence
from smart_ai_router.store.sqlite_store import SqliteStore
from smart_ai_router.sync import SyncResult, _apply_spec, _carry_profile_shape
from smart_ai_router.taxonomy import FIELD_KEYS


def _flat(score: float = 0.80) -> dict[str, float]:
    return {f: score for f in FIELD_KEYS}


def _store() -> SqliteStore:
    return SqliteStore(":memory:")


# ── Compose on read, baseline on write ────────────────────────────────────────

def test_unrated_model_round_trips_unchanged():
    store = _store()
    spec = ModelSpec(value="m", profile=_flat(), competence={"general": 0.8})
    store.upsert_model(spec)
    got = store.get("m")
    assert got.profile == _flat()
    assert got.profile_rules == {}        # empty means "profile IS the baseline"
    assert got.profile_ratings == {}


def test_ratings_are_composed_on_read():
    store = _store()
    store.upsert_model(ModelSpec(
        value="m", profile=_flat(),
        profile_ratings={"law_regulatory": "unsuited"},
        profile_note="coder",
    ))
    got = store.get("m")
    assert got.profile["law_regulatory"] == pytest.approx(0.60)
    assert got.profile["software_engineering"] == pytest.approx(0.80)
    assert got.profile_rules["law_regulatory"] == pytest.approx(0.80)
    assert got.profile_note == "coder"


def test_repeated_read_write_does_not_drift_the_profile():
    """The hazard the two columns exist to prevent: if the write path stored the
    *composed* profile, every round trip would re-apply the offsets and a
    -0.10 rating would walk a score to the floor."""
    store = _store()
    store.upsert_model(ModelSpec(
        value="m", profile=_flat(), profile_ratings={"law_regulatory": "weak"},
    ))
    for _ in range(5):
        store.upsert_model(store.get("m"))
    got = store.get("m")
    assert got.profile["law_regulatory"] == pytest.approx(0.70)
    assert baseline_profile(got)["law_regulatory"] == pytest.approx(0.80)


def test_all_models_composes_too():
    """The router reads via all_models(), so composition has to happen there —
    not only in get()."""
    store = _store()
    store.upsert_model(ModelSpec(
        value="m", profile=_flat(), profile_ratings={"medicine_health": "unsuited"},
    ))
    got = store.all_models()[0]
    assert got.profile["medicine_health"] == pytest.approx(0.60)


def test_a_corrupt_ratings_blob_degrades_to_the_rules_profile():
    store = _store()
    store.upsert_model(ModelSpec(value="m", profile=_flat()))
    store._conn.execute(
        "UPDATE models SET profile_ratings_json='not json' WHERE value='m'"
    )
    got = store.get("m")
    assert got.profile == _flat()
    assert got.profile_ratings == {}


def test_pre_migration_rows_still_read():
    """A DB written before these columns existed must keep routing."""
    store = _store()
    store.upsert_model(ModelSpec(value="m", profile=_flat()))
    store._conn.execute(
        "UPDATE models SET profile_ratings_json=NULL, profile_note=NULL "
        "WHERE value='m'"
    )
    got = store.get("m")
    assert got.profile == _flat()
    assert got.profile_note == ""


# ── Re-sync carry-over ────────────────────────────────────────────────────────

def test_sync_carries_ratings_onto_a_fresh_catalog_spec():
    """Sync rebuilds specs from the catalog and so never carries ratings. Without
    the carry-over, one sync would erase every judgment in the DB."""
    prior = ModelSpec(
        value="m", profile=_flat(0.80),
        profile_ratings={"law_regulatory": "unsuited"}, profile_note="coder",
    )
    fresh = ModelSpec(value="m", profile=_flat(0.90))  # benchmarks moved up
    carried = _carry_profile_shape(fresh, prior)
    # The level came from the new benchmarks; the shape from the stored judgment.
    assert carried.profile["software_engineering"] == pytest.approx(0.90)
    assert carried.profile["law_regulatory"] == pytest.approx(0.70)
    assert carried.profile_ratings == {"law_regulatory": "unsuited"}
    assert carried.profile_note == "coder"


def test_carry_over_re_derives_the_legacy_vector():
    """The 4-value competence summary must describe the profile the router
    matches on, or the dashboard reports a decision nobody made."""
    prior = ModelSpec(value="m", profile=_flat(),
                      profile_ratings={"law_regulatory": "unsuited"})
    fresh = ModelSpec(value="m", profile=_flat(),
                      competence=legacy_competence(_flat()))
    carried = _carry_profile_shape(fresh, prior)
    assert carried.competence == legacy_competence(carried.profile)
    assert carried.competence["reasoning"] < fresh.competence["reasoning"]


def test_carry_over_is_a_no_op_for_unrated_models():
    fresh = ModelSpec(value="m", profile=_flat())
    assert _carry_profile_shape(fresh, ModelSpec(value="m")) is fresh


def _seed_enriched(store: SqliteStore, value: str, base: dict[str, float],
                   ratings: dict[str, str], note: str = "") -> None:
    """Seed a row exactly the way the enrichment endpoint writes one, so the
    stored legacy vector matches the composed profile as it does in production."""
    from smart_ai_router.llm_profiler import _rated_spec

    store.upsert_model(_rated_spec(
        ModelSpec(value=value, profile=base, competence=legacy_competence(base)),
        ratings, note,
    ))


def test_resync_of_an_unchanged_catalog_reports_a_rated_model_unchanged():
    """The reason the carry-over must run *before* the equality check: otherwise
    every enriched model differs from its fresh spec on every sync, so the whole
    catalog reports as "updated" and the ratings get blanked by the upsert."""
    store = _store()
    _seed_enriched(store, "m", _flat(), {"law_regulatory": "unsuited"}, "coder")
    existing = {s.value: s for s in store.all_models()}
    fresh = ModelSpec(value="m", profile=_flat(),
                      competence=legacy_competence(_flat()))

    result = SyncResult()
    _apply_spec(store, fresh, existing, result)

    assert (result.added, result.updated, result.unchanged) == (0, 0, 1)
    assert store.get("m").profile_ratings == {"law_regulatory": "unsuited"}


def test_resync_with_new_benchmarks_updates_and_keeps_the_shape():
    store = _store()
    _seed_enriched(store, "m", _flat(0.80), {"law_regulatory": "unsuited"})
    existing = {s.value: s for s in store.all_models()}
    fresh = ModelSpec(value="m", profile=_flat(0.90),
                      competence=legacy_competence(_flat(0.90)))

    result = SyncResult()
    _apply_spec(store, fresh, existing, result)

    assert result.updated == 1
    got = store.get("m")
    assert got.profile["software_engineering"] == pytest.approx(0.90)
    assert got.profile["law_regulatory"] == pytest.approx(0.70)
    assert got.profile_ratings == {"law_regulatory": "unsuited"}


def test_a_new_model_is_added_without_a_prior():
    store = _store()
    result = SyncResult()
    _apply_spec(store, ModelSpec(value="new", profile=_flat()), {}, result)
    assert (result.added, result.updated) == (1, 0)


# ── Usage-log profile column ──────────────────────────────────────────────────

def test_usage_records_and_returns_the_prompt_profile():
    store = _store()
    profile = {
        "domains": [{"field": "law_regulatory", "depth": "specialist"}],
        "demands": ["factual_precision"],
        "stakes": "high",
    }
    store.record_usage(UsageRecord(
        user="kevin", routed_model="m", domain="reasoning", complexity="hard",
        profile=profile, ts="2026-08-19T00:00:00+00:00",
    ))
    got = store.recent_usage("kevin", "")[0]
    assert got.profile == profile


def test_usage_without_a_profile_reads_back_as_none():
    store = _store()
    store.record_usage(UsageRecord(user="kevin", routed_model="m", ts="2026-08-19T00:00:00+00:00"))
    assert store.recent_usage("kevin", "")[0].profile is None


def test_usage_profiles_groups_and_counts():
    store = _store()
    a = {"domains": [{"field": "law_regulatory", "depth": "specialist"}],
         "demands": [], "stakes": "low"}
    b = {"domains": [{"field": "software_engineering", "depth": "practitioner"}],
         "demands": [], "stakes": "low"}
    for _ in range(3):
        store.record_usage(UsageRecord(user="u", routed_model="big", profile=a,
                                       ts="2026-08-19T00:00:00+00:00"))
    store.record_usage(UsageRecord(user="u", routed_model="cheap", profile=b,
                                   ts="2026-08-19T00:00:00+00:00"))
    store.record_usage(UsageRecord(user="u", routed_model="cheap", profile=None,
                                   ts="2026-08-19T00:00:00+00:00"))

    rows = store.usage_profiles()
    assert len(rows) == 2                      # the profile-less row is skipped
    assert rows[0] == {"profile": a, "routed_model": "big", "requests": 3}
    assert rows[1]["requests"] == 1


def test_usage_profiles_respects_the_window_and_limit():
    store = _store()
    p = {"domains": [{"field": "math_formal", "depth": "surface"}],
         "demands": [], "stakes": "low"}
    store.record_usage(UsageRecord(user="u", routed_model="m", profile=p,
                                   ts="2026-01-01T00:00:00+00:00"))
    assert store.usage_profiles(since_ts="2026-06-01T00:00:00+00:00") == []
    assert len(store.usage_profiles(since_ts="2025-01-01T00:00:00+00:00")) == 1
    assert len(store.usage_profiles(limit=0)) == 1  # limit floors at 1, never 0 rows


def test_prompt_profile_to_dict_round_trips():
    from smart_ai_router.taxonomy import PromptProfile, normalize_profile

    original = normalize_profile({
        "domains": [
            {"field": "law_regulatory", "depth": "frontier"},
            {"field": "natural_science", "depth": "specialist"},
        ],
        "demands": ["factual_precision", "long_synthesis"],
        "stakes": "high",
    })
    assert isinstance(original, PromptProfile)
    again = normalize_profile(original.to_dict())
    assert again == original
    # And the requirements — the thing routing actually consumes — are identical.
    assert again.requirements() == original.requirements()
