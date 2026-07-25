"""Path-jail tests: the security boundary for the agent's filesystem tools."""
import pytest

from smart_ai_router import workspace


@pytest.fixture(autouse=True)
def _root(tmp_path, monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_WORKSPACE_DIR", str(tmp_path / "ws"))


def test_each_user_gets_a_distinct_workspace():
    a = workspace.user_workspace("alice")
    b = workspace.user_workspace("bob")
    assert a != b
    assert a.is_dir() and b.is_dir()


def test_admin_and_empty_share_one_workspace():
    assert workspace.user_workspace("admin") == workspace.user_workspace("")


def test_resolve_normal_relative_path_stays_inside():
    p = workspace.resolve_in_workspace("alice", "sub/dir/file.txt")
    assert str(p).startswith(str(workspace.user_workspace("alice").resolve()))


def test_empty_path_is_the_workspace_root():
    assert workspace.resolve_in_workspace("alice", "") == workspace.user_workspace("alice").resolve()


def test_dotdot_traversal_is_blocked():
    with pytest.raises(workspace.WorkspaceError):
        workspace.resolve_in_workspace("alice", "../../etc/passwd")


def test_absolute_path_is_rerooted_not_escaped():
    # A leading "/" means workspace root, not the real filesystem root.
    p = workspace.resolve_in_workspace("alice", "/etc/passwd")
    assert str(p).startswith(str(workspace.user_workspace("alice").resolve()))
    assert "etc/passwd" in str(p)


def test_one_user_cannot_reach_anothers_workspace():
    # bob's workspace exists as a sibling; alice must not be able to path into it.
    workspace.user_workspace("bob")
    with pytest.raises(workspace.WorkspaceError):
        workspace.resolve_in_workspace("alice", "../bob/secret.txt")


def test_symlink_escape_is_blocked(tmp_path):
    ws = workspace.user_workspace("alice")
    outside = tmp_path / "outside_secret"
    outside.write_text("top secret")
    (ws / "link").symlink_to(outside)
    with pytest.raises(workspace.WorkspaceError):
        workspace.resolve_in_workspace("alice", "link")


def test_sibling_prefix_name_is_not_inside():
    # ".../alice" must not treat ".../alice-evil" as inside it.
    workspace.user_workspace("alice")
    with pytest.raises(workspace.WorkspaceError):
        workspace.resolve_in_workspace("alice", "../alice-evil/x")


def test_malicious_identity_cannot_traverse():
    # A crafted username must not become a traversal payload.
    p = workspace.user_workspace("../../etc")
    assert str(p.resolve()).startswith(str(workspace.workspace_root().resolve()))
