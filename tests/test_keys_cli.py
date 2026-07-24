"""Tests for the `smart-ai-router keys` CLI command handlers."""
from smart_ai_router import keys_cli
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.store.sqlite_store import SqliteStore


def _cr():
    return CapabilityRouter(store=SqliteStore(":memory:"))


def _args(**kw):
    # Minimal stand-in for argparse.Namespace with add's defaults.
    defaults = dict(scope="", max_tier=0, window_s=0, max_req=0, max_tokens=0)
    defaults.update(kw)
    return type("Args", (), defaults)()


def test_add_mints_key_and_prints_plaintext_once(capsys):
    cr = _cr()
    rc = keys_cli._cmd_add(cr, _args(user="alice"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "sk-smart-" in out                # plaintext shown
    keys = cr.all_api_keys()
    assert len(keys) == 1 and keys[0].user == "alice"
    # Stored value is the hash, never the printed plaintext.
    printed = [w for w in out.split() if w.startswith("sk-smart-")][0]
    assert keys[0].key_hash != printed


def test_add_with_scope_and_limits_persists(capsys):
    cr = _cr()
    keys_cli._cmd_add(cr, _args(
        user="bob", scope='{"allow":["ollama/"]}', max_tier=5,
        window_s=3600, max_req=100, max_tokens=50000,
    ))
    k = cr.all_api_keys()[0]
    assert k.scope_models == '{"allow":["ollama/"]}'
    assert k.max_tier == 5
    assert (k.rl_window_s, k.rl_max_req, k.rl_max_tokens) == (3600, 100, 50000)


def test_add_rejects_empty_user():
    assert keys_cli._cmd_add(_cr(), _args(user="   ")) == 2


def test_list_empty(capsys):
    keys_cli._cmd_list(_cr(), _args())
    assert "No API keys" in capsys.readouterr().out


def test_disable_enable_delete_roundtrip(capsys):
    cr = _cr()
    keys_cli._cmd_add(cr, _args(user="carol"))
    prefix = cr.all_api_keys()[0].key_prefix

    assert keys_cli._cmd_set_enabled(cr, prefix, False) == 0
    assert cr.all_api_keys()[0].enabled is False
    assert keys_cli._cmd_set_enabled(cr, prefix, True) == 0
    assert cr.all_api_keys()[0].enabled is True

    assert keys_cli._cmd_delete(cr, _args(prefix=prefix)) == 0
    assert cr.all_api_keys() == []


def test_operations_on_unknown_prefix_report_error():
    cr = _cr()
    assert keys_cli._cmd_set_enabled(cr, "sk-smart-nope", False) == 1
    assert keys_cli._cmd_delete(cr, _args(prefix="sk-smart-nope")) == 1


def test_parser_requires_subcommand():
    import pytest
    with pytest.raises(SystemExit):
        keys_cli.build_parser().parse_args([])
