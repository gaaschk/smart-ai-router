"""Tests for UI-managed runtime settings (DB → env → default precedence)."""
import sqlite3
import warnings

import pytest
from fastapi.testclient import TestClient

from smart_ai_router import settings as _settings
from smart_ai_router.api.app import create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.router import _denylisted
from smart_ai_router.store.sqlite_store import SqliteStore

_ADMIN = "admin-secret"


def _bound_router():
    cr = CapabilityRouter(store=SqliteStore(":memory:"))
    _settings.bind_store(cr)
    return cr


# ── Precedence: DB → env → default ──────────────────────────────────────────────

def test_default_when_unset(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_OCR_DPI", raising=False)
    _bound_router()
    assert _settings.get("ocr_dpi") == 150
    assert _settings.source("ocr_dpi") == "default"


def test_env_fallback(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_OCR_DPI", "200")
    _bound_router()
    assert _settings.get("ocr_dpi") == 200
    assert _settings.source("ocr_dpi") == "env"


def test_db_overrides_env(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_OCR_DPI", "200")
    cr = _bound_router()
    cr.set_setting("ocr_dpi", "300")
    assert _settings.get("ocr_dpi") == 300
    assert _settings.source("ocr_dpi") == "db"


def test_unbound_store_uses_env_then_default(monkeypatch):
    # No store bound (e.g. a plain unit test) → env → default, never crashes.
    # Asserts the *precedence*, not a particular model name — the shipped
    # classifier default is a tuning choice that changes independently.
    _settings.bind_store(None)
    shipped = next(s for s in _settings.SPECS if s.key == "classifier_model").default
    monkeypatch.delenv("SMART_ROUTER_CLASSIFIER_MODEL", raising=False)
    assert _settings.get("classifier_model") == shipped
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_MODEL", "custom:1b")
    assert _settings.get("classifier_model") == "custom:1b"


# ── Typing / validation ─────────────────────────────────────────────────────────

def test_bool_coercion(monkeypatch):
    cr = _bound_router()
    for raw, want in [("true", True), ("1", True), ("yes", True),
                      ("false", False), ("0", False), ("", False)]:
        cr.set_setting("enable_bash", raw)
        assert _settings.get("enable_bash") is want


def test_malformed_int_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_MAX_FILE_MB", raising=False)
    cr = _bound_router()
    cr.set_setting("max_file_mb", "not-a-number")
    # A garbage stored value must not raise into a request path.
    assert _settings.get("max_file_mb") == 512


def test_normalize_rejects_bad_values():
    _bound_router()
    with pytest.raises(ValueError):
        _settings.normalize("max_file_mb", "abc")
    with pytest.raises(ValueError):
        _settings.normalize("enable_bash", "maybe")
    with pytest.raises(ValueError):
        _settings.normalize("unknown_key", "x")


def test_normalize_serializes_types():
    _bound_router()
    assert _settings.normalize("enable_bash", True) == "true"
    assert _settings.normalize("enable_bash", False) == "false"
    assert _settings.normalize("max_file_mb", 256) == "256"
    assert _settings.normalize("model_denylist", "mxfp8") == "mxfp8"


def test_float_coercion_and_round_trip(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_PUBLIC_DAILY_BUDGET", raising=False)
    cr = _bound_router()
    assert _settings.get("public_daily_budget_usd") == 1.00
    cr.set_setting("public_daily_budget_usd", _settings.normalize(
        "public_daily_budget_usd", 2.50))
    assert _settings.get("public_daily_budget_usd") == 2.50
    # A dollar cap of zero is a legitimate choice ("no paid spend"), not garbage.
    assert _settings.normalize("public_daily_budget_usd", 0) == "0.0"


def test_malformed_float_falls_back_to_default():
    cr = _bound_router()
    cr.set_setting("public_daily_budget_usd", "$5")
    assert _settings.get("public_daily_budget_usd") == 1.00
    with pytest.raises(ValueError):
        _settings.normalize("public_daily_budget_usd", "$5")


def test_numeric_specs_reject_negatives():
    """Validation used to run for str specs only, so an int spec's validate was
    dead code — and a negative tier ceiling or window is nonsense that would be
    stored silently."""
    _bound_router()
    for key in ("public_max_tier", "public_rl_window_s", "public_max_output_tokens",
                "public_daily_budget_usd"):
        with pytest.raises(ValueError):
            _settings.normalize(key, -1)
    assert _settings.normalize("public_max_tier", 0) == "0"


def test_apply_requires_bound_store():
    _settings.bind_store(None)
    with pytest.raises(RuntimeError):
        _settings.apply({"ocr_dpi": 300})


def test_effective_covers_all_specs():
    _bound_router()
    keys = {e["key"] for e in _settings.effective()}
    assert keys == {s.key for s in _settings.SPECS}
    row = next(e for e in _settings.effective() if e["key"] == "enable_bash")
    assert row["type"] == "bool" and row["sensitive"] is True


# ── Store round-trip + migration ────────────────────────────────────────────────

def test_store_setting_roundtrip():
    store = SqliteStore(":memory:")
    assert store.get_setting("ocr_dpi") is None
    store.set_setting("ocr_dpi", "300")
    assert store.get_setting("ocr_dpi") == "300"
    store.set_setting("ocr_dpi", "72")  # upsert overwrites
    assert store.get_setting("ocr_dpi") == "72"
    assert store.all_settings()["ocr_dpi"] == "72"


def test_settings_migration_on_preexisting_db(tmp_path):
    # A DB created before the settings table existed must migrate cleanly.
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE models (value TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    store = SqliteStore(db)  # _migrate() runs on open
    store.set_setting("model_denylist", "mxfp8")
    assert store.get_setting("model_denylist") == "mxfp8"


# ── Live behavior: denylist reads DB over env ───────────────────────────────────

def test_denylist_reads_db_over_env(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_MODEL_DENYLIST", "from-env")
    cr = _bound_router()
    assert _denylisted() == ("from-env",)
    cr.set_setting("model_denylist", "mxfp8, qwen")
    assert _denylisted() == ("mxfp8", "qwen")


# ── Endpoint: admin-gating + persistence ────────────────────────────────────────

def _client(cr):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return TestClient(create_app(cr))


def test_get_settings_open_mode(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    cr = CapabilityRouter(store=SqliteStore(":memory:"))
    client = _client(cr)  # create_app binds the store
    r = client.get("/api/settings")
    assert r.status_code == 200
    keys = {s["key"] for s in r.json()["settings"]}
    assert "enable_bash" in keys


def test_settings_require_admin(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    cr = CapabilityRouter(store=SqliteStore(":memory:"))
    client = _client(cr)
    # No key → 401 from middleware.
    assert client.get("/api/settings").status_code == 401
    # Non-admin per-user key → 403 from _require_admin.
    from smart_ai_router.apikeys import generate_key, hash_key, display_prefix
    from smart_ai_router.models import ApiKey
    plaintext = generate_key()
    cr.create_api_key(ApiKey(key_hash=hash_key(plaintext), user="bob",
                             key_prefix=display_prefix(plaintext), enabled=True))
    r = client.get("/api/settings", headers={"Authorization": f"Bearer {plaintext}"})
    assert r.status_code == 403


def test_put_settings_persists_and_applies(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    cr = CapabilityRouter(store=SqliteStore(":memory:"))
    client = _client(cr)
    hdr = {"Authorization": f"Bearer {_ADMIN}"}
    r = client.put("/api/settings", headers=hdr,
                   json={"updates": {"enable_bash": True, "ocr_dpi": 300}})
    assert r.status_code == 200
    assert cr.get_setting("enable_bash") == "true"
    assert cr.get_setting("ocr_dpi") == "300"
    # Response reflects the new effective values + source.
    rows = {s["key"]: s for s in r.json()["settings"]}
    assert rows["enable_bash"]["value"] is True
    assert rows["enable_bash"]["source"] == "db"


def test_put_settings_rejects_bad_value(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    cr = CapabilityRouter(store=SqliteStore(":memory:"))
    client = _client(cr)
    hdr = {"Authorization": f"Bearer {_ADMIN}"}
    r = client.put("/api/settings", headers=hdr,
                   json={"updates": {"ocr_dpi": "not-a-number"}})
    assert r.status_code == 422


# ── The triage model refuses "auto" ─────────────────────────────────────────────

def test_classifier_model_rejects_auto():
    """Routing triage is a measured regression, not a preference — see
    helper_models' "Why triage is not a HelperTask". The word is rejected at the
    setting rather than quietly reinterpreted, so nobody types it and believes it
    took effect."""
    with pytest.raises(ValueError):
        _settings.normalize("classifier_model", "auto")
    with pytest.raises(ValueError):
        _settings.normalize("classifier_model", " AUTO ")


def test_classifier_model_accepts_a_name_and_the_off_switch():
    assert _settings.normalize("classifier_model", "llama3.1:8b") == "llama3.1:8b"
    assert _settings.normalize("classifier_model", "") == ""


def test_helper_settings_still_accept_auto():
    # The two calls that *are* routed keep the word; the guard is triage-only.
    assert _settings.normalize("classifier_refine_model", "auto") == "auto"
    assert _settings.normalize("model_profiler_model", "auto") == "auto"


def test_put_settings_rejects_auto_triage(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    cr = CapabilityRouter(store=SqliteStore(":memory:"))
    client = _client(cr)
    r = client.put("/api/settings", headers={"Authorization": f"Bearer {_ADMIN}"},
                   json={"updates": {"classifier_model": "auto"}})
    assert r.status_code == 422
    assert cr.get_setting("classifier_model") is None   # nothing persisted


# ── Advisories ride along with the settings payload ─────────────────────────────

def test_settings_response_carries_a_classifier_advisory(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_MODEL", "not-pulled:9b")
    cr = CapabilityRouter(store=SqliteStore(":memory:"))
    client = _client(cr)
    rows = {s["key"]: s for s in client.get(
        "/api/settings", headers={"Authorization": f"Bearer {_ADMIN}"}
    ).json()["settings"]}
    assert "not in the model catalog" in rows["classifier_model"]["warning"]
    # Every other setting stays quiet — an advisory is per-key, not a banner.
    assert rows["ocr_dpi"]["warning"] == ""


def test_a_healthy_classifier_pin_produces_no_advisory(monkeypatch):
    from smart_ai_router.models import ModelSpec

    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_MODEL", "llama3.1:8b")
    cr = CapabilityRouter(store=SqliteStore(":memory:"))
    cr.upsert_model(ModelSpec(value="ollama/llama3.1:8b", provider="ollama",
                              cost=0, reliability=1.0, structured_outputs=True))
    client = _client(cr)
    rows = {s["key"]: s for s in client.get(
        "/api/settings", headers={"Authorization": f"Bearer {_ADMIN}"}
    ).json()["settings"]}
    assert rows["classifier_model"]["warning"] == ""
