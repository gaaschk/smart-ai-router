"""Tool schema + execution tests (read/write/edit against a jailed workspace)."""
import pytest

from smart_ai_router import tools


@pytest.fixture(autouse=True)
def _root(tmp_path, monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_WORKSPACE_DIR", str(tmp_path / "ws"))
    # Bash off by default in tests unless a test opts in.
    monkeypatch.delenv("SMART_ROUTER_ENABLE_BASH", raising=False)


# ── schemas ─────────────────────────────────────────────────────────────────

def test_read_only_schema_excludes_write_and_bash():
    names = tools.tool_names(allow_write=False, allow_bash=False)
    assert names == {"list_dir", "read_file"}


def test_write_schema_includes_write_tools():
    names = tools.tool_names(allow_write=True, allow_bash=False)
    assert "write_file" in names and "edit_file" in names
    assert "run_bash" not in names


def test_bash_excluded_when_sandbox_unavailable(monkeypatch):
    # Even asking for bash, it's dropped if the sandbox isn't enabled/available.
    names = tools.tool_names(allow_write=True, allow_bash=True)
    assert "run_bash" not in names


def test_schemas_are_openai_function_shaped():
    for schema in tools.tool_schemas():
        assert schema["type"] == "function"
        assert "name" in schema["function"]
        assert schema["function"]["parameters"]["type"] == "object"


# ── execution ─────────────────────────────────────────────────────────────────

def test_write_then_read_roundtrip():
    out = tools.execute_tool("alice", "write_file", {"path": "notes/todo.txt", "content": "hello"})
    assert "Wrote" in out
    got = tools.execute_tool("alice", "read_file", {"path": "notes/todo.txt"})
    assert got == "hello"


@pytest.mark.parametrize("path", [
    "resume.docx", "report.pdf", "deck.pptx", "data.xlsx",
    "old.doc", "old.ppt", "old.xls", "sub/dir/thing.PDF",
])
def test_write_file_refuses_binary_doc_extensions(path):
    # write_file must never write text bytes into a binary document format —
    # that yields a corrupt file. It returns an error steering to create_document.
    out = tools.execute_tool("alice", "write_file", {"path": path, "content": "plain text"})
    assert out.startswith("Error")
    assert "create_document" in out
    # And it must NOT have written anything to the workspace.
    listing = tools.execute_tool("alice", "list_dir", {"path": ""})
    assert path.split("/")[0] not in listing.split("\n") if "/" not in path else True


def test_write_file_still_allows_text_extensions():
    for path in ("notes.txt", "data.csv", "config.json", "README.md", "script.py"):
        out = tools.execute_tool("alice", "write_file", {"path": path, "content": "x"})
        assert "Wrote" in out


def test_list_dir_shows_written_files():
    tools.execute_tool("alice", "write_file", {"path": "a.txt", "content": "x"})
    tools.execute_tool("alice", "write_file", {"path": "sub/b.txt", "content": "y"})
    listing = tools.execute_tool("alice", "list_dir", {"path": ""})
    assert "a.txt" in listing
    assert "sub/" in listing  # directory marker


def test_read_missing_file_returns_error_not_raise():
    out = tools.execute_tool("alice", "read_file", {"path": "nope.txt"})
    assert out.startswith("Error")


def test_edit_file_replaces_substring():
    tools.execute_tool("alice", "write_file", {"path": "f.txt", "content": "foo bar foo"})
    out = tools.execute_tool("alice", "edit_file", {"path": "f.txt", "old_text": "foo", "new_text": "baz"})
    assert "Edited" in out
    assert tools.execute_tool("alice", "read_file", {"path": "f.txt"}) == "baz bar foo"


def test_edit_missing_text_makes_no_change():
    tools.execute_tool("alice", "write_file", {"path": "f.txt", "content": "abc"})
    out = tools.execute_tool("alice", "edit_file", {"path": "f.txt", "old_text": "zzz", "new_text": "q"})
    assert "not found" in out
    assert tools.execute_tool("alice", "read_file", {"path": "f.txt"}) == "abc"


def test_traversal_attempt_returns_error():
    out = tools.execute_tool("alice", "read_file", {"path": "../../../etc/passwd"})
    assert out.startswith("Error")


def test_users_are_isolated():
    tools.execute_tool("alice", "write_file", {"path": "secret.txt", "content": "alice-only"})
    # bob listing his own (empty) root must not see alice's file
    listing = tools.execute_tool("bob", "list_dir", {"path": ""})
    assert "secret.txt" not in listing


def test_unknown_tool_returns_error():
    assert tools.execute_tool("alice", "frobnicate", {}).startswith("Error: unknown tool")


# ── bash (only where the sandbox is really available) ─────────────────────────

def test_bash_runs_in_workspace_when_enabled(monkeypatch):
    from smart_ai_router import sandbox
    monkeypatch.setenv("SMART_ROUTER_ENABLE_BASH", "1")
    if not sandbox.available():
        pytest.skip("sandbox-exec not available on this host")
    tools.execute_tool("alice", "write_file", {"path": "hi.txt", "content": "sandboxed"})
    out = tools.execute_tool("alice", "run_bash", {"command": "cat hi.txt"})
    assert "sandboxed" in out
    assert "[exit code: 0]" in out


def test_bash_cannot_read_another_users_workspace(monkeypatch):
    from smart_ai_router import sandbox
    monkeypatch.setenv("SMART_ROUTER_ENABLE_BASH", "1")
    if not sandbox.available():
        pytest.skip("sandbox-exec not available on this host")
    # bob writes a secret in his own workspace...
    tools.execute_tool("bob", "write_file", {"path": "secret.txt", "content": "bob-only-secret"})
    bob_secret = workspace_module().user_workspace("bob") / "secret.txt"
    # ...alice's sandboxed shell must not be able to read it.
    out = tools.execute_tool("alice", "run_bash", {"command": f"cat '{bob_secret}'"})
    assert "bob-only-secret" not in out
    assert "[exit code: 0]" not in out


def test_bash_network_is_denied(monkeypatch):
    from smart_ai_router import sandbox
    monkeypatch.setenv("SMART_ROUTER_ENABLE_BASH", "1")
    if not sandbox.available():
        pytest.skip("sandbox-exec not available on this host")
    out = tools.execute_tool(
        "alice", "run_bash",
        {"command": "curl -s --max-time 3 http://example.com >/dev/null 2>&1 && echo NET-OK || echo net-blocked"},
    )
    assert "NET-OK" not in out


def workspace_module():
    from smart_ai_router import workspace
    return workspace


# ── create_document ─────────────────────────────────────────────────────────

def test_create_document_in_schema():
    assert "create_document" in tools.tool_names(allow_write=True, allow_bash=False)
    assert "create_document" not in tools.tool_names(allow_write=False, allow_bash=False)


def test_create_document_writes_to_workspace():
    out = tools.execute_tool(
        "alice", "create_document",
        {"path": "resume.pdf", "content": "# Kevin\n\nSummary text."},
    )
    assert "Created resume.pdf" in out
    ws = workspace_module().user_workspace("alice")
    data = (ws / "resume.pdf").read_bytes()
    assert data[:4] == b"%PDF"


def test_create_document_registers_and_links():
    captured = {}

    def register(data, filename, mime):
        captured.update(data=data, filename=filename, mime=mime)
        return "file-xyz"

    out = tools.execute_tool(
        "alice", "create_document",
        {"path": "report.docx", "content": "# Report\n\nBody."},
        register_file=register,
    )
    assert captured["filename"] == "report.docx"
    assert "wordprocessingml" in captured["mime"]
    assert captured["data"][:2] == b"PK"
    assert "/v1/files/file-xyz/content" in out


def test_create_document_rejects_unsupported_type():
    out = tools.execute_tool(
        "alice", "create_document",
        {"path": "page.html", "content": "<h1>hi</h1>"},
    )
    assert "unsupported document type" in out.lower()


def test_create_document_requires_path():
    out = tools.execute_tool("alice", "create_document", {"content": "body"})
    assert "requires a 'path'" in out


def test_create_document_path_escape_refused():
    out = tools.execute_tool(
        "alice", "create_document",
        {"path": "../escape.pdf", "content": "# x"},
    )
    assert out.lower().startswith("error")


def test_create_document_registration_failure_is_soft():
    def boom(data, filename, mime):
        raise RuntimeError("db down")

    out = tools.execute_tool(
        "alice", "create_document",
        {"path": "a.md", "content": "# hi"},
        register_file=boom,
    )
    # File still created; link replaced by a soft note.
    assert "Created a.md" in out
    assert "Could not register" in out
