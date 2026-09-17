"""Tests for per-user GBrain source isolation.

Two things are pinned here:

1. `source_id_for_user` — the deterministic mapping from
   `request.state.user` onto a GBrain source id (`[a-z0-9-]{1,32}`). Admin and
   the empty/open-mode identity must keep reading/writing the brain's
   *default* source (the one holding this deployment's imported project
   docs — see docs/gbrain-deployment.md), while every other identity gets its
   own slug, deterministically and collision-resistantly.
2. `GBrainClient._call` — that a non-empty `source_id` is appended as a
   trailing `--source <id>` CLI argument, which is where GBrain's `call`
   subcommand actually accepts it (verified against a live instance: the flag
   must come *after* the JSON payload, not before).
"""
from unittest.mock import patch, MagicMock

from smart_ai_router.gbrain_client import GBrainClient, source_id_for_user


# ── source_id_for_user ──────────────────────────────────────────────────────

def test_admin_and_empty_user_map_to_the_default_source():
    assert source_id_for_user("admin") == ""
    assert source_id_for_user("") == ""
    assert source_id_for_user(None) == ""  # defensive: called with getattr(..., "") or ""


def test_anon_and_self_serve_identities_get_a_slugified_source():
    assert source_id_for_user("anon:abcDEF-123") == "anon-abcdef-123"
    assert source_id_for_user("u:9f3a2b17") == "u-9f3a2b17"


def test_a_plain_user_label_is_lowercased_and_slugified():
    assert source_id_for_user("Kevin Gaasch") == "kevin-gaasch"


def test_the_mapping_is_deterministic():
    # Same input -> same output every time, since isolation depends on this
    # being a pure function rather than a stored mapping.
    assert source_id_for_user("anon:xyz") == source_id_for_user("anon:xyz")


def test_result_never_exceeds_the_32_char_source_id_limit():
    long_user = "a" * 80
    result = source_id_for_user(long_user)
    assert len(result) <= 32
    assert result  # non-empty


def test_two_long_labels_sharing_a_prefix_do_not_collide():
    a = source_id_for_user("a" * 40 + "-one")
    b = source_id_for_user("a" * 40 + "-two")
    assert a != b


def test_punctuation_only_identity_still_gets_a_stable_nonempty_source():
    result = source_id_for_user("!!!")
    assert result
    assert result == source_id_for_user("!!!")


def test_result_only_contains_characters_gbrain_source_ids_allow():
    import re
    for user in ("Kevin Gaasch", "anon:abc-123", "u:xyz", "!!!", "a" * 80):
        result = source_id_for_user(user)
        assert result == "" or re.fullmatch(r"[a-z0-9-]{1,32}", result)


# ── GBrainClient._call --source plumbing ────────────────────────────────────

def _client_with_mocked_subprocess():
    """A GBrainClient with binary detection skipped and subprocess.run mocked."""
    client = GBrainClient.__new__(GBrainClient)  # bypass __init__'s _check_binary
    client.bin_path = "gbrain"
    client.timeout_s = 30.0
    return client


def test_call_appends_source_flag_after_the_json_payload_when_given():
    client = _client_with_mocked_subprocess()
    fake_result = MagicMock(returncode=0, stdout="null", stderr="")
    with patch("subprocess.run", return_value=fake_result) as mock_run:
        client._call("list_pages", {"limit": 5}, source_id="kevin-gaasch")
    cmd = mock_run.call_args[0][0]
    assert cmd == ["gbrain", "call", "list_pages", '{"limit": 5}', "--source", "kevin-gaasch"]


def test_call_rejects_a_malformed_source_id_before_it_reaches_argv():
    # Defense in depth against CodeQL's uncontrolled-command-line class of
    # finding: even though subprocess.run() here never uses a shell, an
    # unvalidated source_id could still be misread as a flag (e.g. "--help")
    # by gbrain's own arg parser. source_id_for_user() never produces this,
    # but _call() must refuse it anyway rather than trust the caller.
    client = _client_with_mocked_subprocess()
    with patch("subprocess.run") as mock_run:
        try:
            client._call("list_pages", {}, source_id="--help")
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "invalid" in str(e).lower()
    mock_run.assert_not_called()


def test_call_omits_source_flag_when_source_id_is_empty():
    client = _client_with_mocked_subprocess()
    fake_result = MagicMock(returncode=0, stdout="null", stderr="")
    with patch("subprocess.run", return_value=fake_result) as mock_run:
        client._call("list_pages", {"limit": 5})
    cmd = mock_run.call_args[0][0]
    assert "--source" not in cmd


def test_remember_sends_a_provenance_field():
    # remember's `provenance` param is required by the underlying GBrain tool
    # (a hard "Missing required parameter: provenance" error otherwise) --
    # this pins that the client always sends one rather than relying on a
    # server-side default that doesn't exist.
    client = _client_with_mocked_subprocess()
    fake_result = MagicMock(returncode=0, stdout='{"status": "inserted"}', stderr="")
    with patch("subprocess.run", return_value=fake_result) as mock_run:
        client.remember("title", "content", source_id="kevin-gaasch")
    cmd = mock_run.call_args[0][0]
    payload = cmd[3]
    assert '"provenance"' in payload
    assert cmd[-2:] == ["--source", "kevin-gaasch"]


def test_ensure_source_treats_already_registered_as_success():
    client = _client_with_mocked_subprocess()
    # sources_add on an existing id exits non-zero with an "already
    # registered" message -- ensure_source must swallow that, not raise.
    fake_result = MagicMock(
        returncode=1, stdout="", stderr='Source id "x" is already registered.'
    )
    with patch("subprocess.run", return_value=fake_result):
        assert client.ensure_source("x") is True


def test_ensure_source_is_a_noop_for_the_default_empty_source():
    client = _client_with_mocked_subprocess()
    with patch("subprocess.run") as mock_run:
        assert client.ensure_source("") is True
    mock_run.assert_not_called()
