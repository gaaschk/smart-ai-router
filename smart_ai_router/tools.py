"""Agent filesystem tools: schemas + execution against a per-user workspace.

These are OpenAI function-calling tools the proxy advertises to a tool-capable
model when the client asks for agent mode. The model emits tool_calls; the
proxy executes them here and feeds results back until the model stops calling
(the loop lives in agent_loop.py).

Every tool operates strictly inside the caller's workspace (workspace.py):
  list_dir(path)                 — list a directory
  read_file(path)                — read a text file
  write_file(path, content)      — create/overwrite a text file
  edit_file(path, old, new)      — replace an exact substring
  create_document(path, content) — render Markdown into a downloadable
                                   PDF/Word/PowerPoint/Excel/Markdown file
  run_bash(command)              — run a shell command (sandboxed; opt-in)

Execution never raises for user-caused errors (missing file, bad path, non-zero
exit); it returns an error string the model can read and react to. That keeps
the agent loop robust — a tool error is a turn, not a crash.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from smart_ai_router import docgen as _docgen
from smart_ai_router import sandbox as _sandbox
from smart_ai_router.workspace import WorkspaceError, resolve_in_workspace

# A callback the proxy supplies so a created file is also registered in the
# Files API (downloadable from the chat and listed in the Files tab). Signature:
# (data, filename, mime) -> file_id.  None when unavailable (e.g. unit tests),
# in which case create_document still writes to the workspace.
RegisterFile = Callable[[bytes, str, str], str]

# Extension → MIME for files we generate, so a registered file downloads with
# the right type.
_EXT_MIME = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

# Cap on bytes returned by read_file / a listing, so one tool result can't blow
# the model's context or the response size.
_MAX_READ_BYTES = 100_000
_MAX_LIST_ENTRIES = 1000

# Binary document formats write_file must refuse: writing UTF-8 text bytes to
# one of these always yields a corrupt file. The model must use create_document
# (which renders real binary) for these instead.
_BINARY_DOC_EXTS = frozenset({".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls"})


# ── schemas ─────────────────────────────────────────────────────────────────

def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


_READ_TOOLS = [
    _fn(
        "list_dir",
        "List the contents of a directory in your workspace. Use '' or '.' for "
        "the workspace root. Returns names with a trailing '/' for directories.",
        {"path": {"type": "string", "description": "Directory path relative to the workspace root."}},
        [],
    ),
    _fn(
        "read_file",
        "Read a UTF-8 text file from your workspace and return its contents.",
        {"path": {"type": "string", "description": "File path relative to the workspace root."}},
        ["path"],
    ),
]

_WRITE_TOOLS = [
    _fn(
        "write_file",
        "Create or overwrite a plain-text file (source code, .txt, .json, .csv, "
        "config, etc.) in your workspace. Parent directories are created as "
        "needed. Do NOT use this for documents the user wants to open or "
        "download — a .pdf, .docx, .pptx, or .xlsx written this way is a corrupt "
        "file, because those are binary formats. Use create_document for those.",
        {
            "path": {"type": "string", "description": "File path relative to the workspace root."},
            "content": {"type": "string", "description": "Full file contents to write."},
        },
        ["path", "content"],
    ),
    _fn(
        "edit_file",
        "Replace the first exact occurrence of old_text with new_text in a file. "
        "old_text must match exactly, including whitespace.",
        {
            "path": {"type": "string", "description": "File path relative to the workspace root."},
            "old_text": {"type": "string", "description": "Exact text to find."},
            "new_text": {"type": "string", "description": "Text to replace it with."},
        },
        ["path", "old_text", "new_text"],
    ),
    _fn(
        "create_document",
        "Create a downloadable document (PDF, Word .docx, PowerPoint .pptx, "
        "Excel .xlsx, or Markdown/text) from Markdown content, and give the "
        "user a link to download it. Use this whenever the user asks for a file "
        "they can save or open — a resume, report, slide deck, spreadsheet, etc. "
        "Write the body as Markdown: '# ' headings, '- ' bullets, '**bold**', "
        "and pipe tables. For .pptx each '# ' heading starts a new slide; for "
        ".xlsx use a pipe table or comma-separated lines for rows.",
        {
            "path": {
                "type": "string",
                "description": "Output file name/path relative to the workspace root; "
                               "the extension picks the format (.pdf/.docx/.pptx/.xlsx/.md/.txt).",
            },
            "content": {
                "type": "string",
                "description": "The document body as Markdown.",
            },
        },
        ["path", "content"],
    ),
]

_BASH_TOOL = _fn(
    "run_bash",
    "Run a shell command inside your sandboxed workspace. Network is disabled "
    "and the command can only access files in your workspace. Returns stdout, "
    "stderr, and the exit code.",
    {"command": {"type": "string", "description": "The shell command to run."}},
    ["command"],
)


def tool_schemas(*, allow_write: bool = True, allow_bash: bool | None = None) -> list[dict]:
    """The tool definitions to advertise to the model for this request.

    Read tools are always included. Write tools follow `allow_write`. Bash is
    included only if requested *and* the OS sandbox is actually available
    (defaults to sandbox availability when `allow_bash` is None).
    """
    schemas = list(_READ_TOOLS)
    if allow_write:
        schemas.extend(_WRITE_TOOLS)
    bash_ok = _sandbox.available() if allow_bash is None else (allow_bash and _sandbox.available())
    if bash_ok:
        schemas.append(_BASH_TOOL)
    return schemas


def tool_names(*, allow_write: bool = True, allow_bash: bool | None = None) -> set[str]:
    return {t["function"]["name"] for t in tool_schemas(allow_write=allow_write, allow_bash=allow_bash)}


# ── execution ─────────────────────────────────────────────────────────────────

def _truncate(text: str, limit: int = _MAX_READ_BYTES) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... truncated, {len(text) - limit} more characters]"


def _do_list_dir(user: str, args: dict) -> str:
    target = resolve_in_workspace(user, args.get("path", ""))
    if not target.exists():
        return f"Error: no such directory: {args.get('path', '')!r}"
    if not target.is_dir():
        return f"Error: not a directory: {args.get('path', '')!r}"
    entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name))
    names = [f"{p.name}/" if p.is_dir() else p.name for p in entries[:_MAX_LIST_ENTRIES]]
    if not names:
        return "(empty directory)"
    suffix = "" if len(entries) <= _MAX_LIST_ENTRIES else f"\n[... {len(entries) - _MAX_LIST_ENTRIES} more]"
    return "\n".join(names) + suffix


def _do_read_file(user: str, args: dict) -> str:
    target = resolve_in_workspace(user, args.get("path", ""))
    if not target.exists() or not target.is_file():
        return f"Error: no such file: {args.get('path', '')!r}"
    try:
        return _truncate(target.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        return f"Error reading file: {exc}"


def _do_write_file(user: str, args: dict) -> str:
    path = args.get("path", "")
    # Guard: refuse to write text bytes to a binary document format — that
    # always produces a corrupt file. Steer the model to create_document, whose
    # result the loop feeds back so the model can retry with the right tool.
    ext = path[path.rfind("."):].lower() if "." in path else ""
    if ext in _BINARY_DOC_EXTS:
        return (
            f"Error: {ext} is a binary document format — writing text to it with "
            f"write_file produces a corrupt, unopenable file. Use create_document "
            f"with path {path!r} instead: it renders a real {ext} the user can "
            f"download, and returns a download link."
        )
    target = resolve_in_workspace(user, path)
    content = args.get("content", "")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"Error writing file: {exc}"
    return f"Wrote {len(content)} characters to {args.get('path', '')}"


def _do_edit_file(user: str, args: dict) -> str:
    target = resolve_in_workspace(user, args.get("path", ""))
    if not target.exists() or not target.is_file():
        return f"Error: no such file: {args.get('path', '')!r}"
    old = args.get("old_text", "")
    new = args.get("new_text", "")
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Error reading file: {exc}"
    if old not in text:
        return "Error: old_text not found in file; no changes made."
    target.write_text(text.replace(old, new, 1), encoding="utf-8")
    return f"Edited {args.get('path', '')} (1 replacement)"


def _do_create_document(user: str, args: dict, register_file: RegisterFile | None) -> str:
    path = args.get("path", "")
    content = args.get("content", "")
    if not path:
        return "Error: create_document requires a 'path' (with an extension)."
    if not _docgen.is_supported(path):
        return (
            f"Error: unsupported document type for {path!r}. Supported extensions: "
            + ", ".join(sorted(_docgen.SUPPORTED_EXTS))
        )
    # Resolve inside the jail first so a bad path is refused before rendering.
    target = resolve_in_workspace(user, path)
    try:
        data = _docgen.render(path, content)
    except _docgen.DocGenError as exc:
        return f"Error: {exc}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except OSError as exc:
        return f"Error writing file: {exc}"

    filename = target.name
    ext = filename[filename.rfind("."):].lower() if "." in filename else ""
    mime = _EXT_MIME.get(ext, "application/octet-stream")
    result = f"Created {filename} ({len(data)} bytes) in your workspace."
    if register_file is not None:
        try:
            file_id = register_file(data, filename, mime)
            # A relative link the chat UI resolves against the router origin;
            # the Files API serves it as a download.
            result += (
                f" Download it here: [{filename}](/v1/files/{file_id}/content) "
                f"(also available in the Files tab)."
            )
        except Exception as exc:  # noqa: BLE001 — registration is best-effort
            result += f" (Could not register for download: {exc})"
    return result


def _do_run_bash(user: str, args: dict) -> str:
    from smart_ai_router.workspace import user_workspace
    try:
        result = _sandbox.run_bash(args.get("command", ""), user_workspace(user))
    except RuntimeError as exc:
        return f"Error: {exc}"
    parts = []
    if result["stdout"]:
        parts.append(_truncate(result["stdout"]))
    if result["stderr"]:
        parts.append(f"[stderr]\n{_truncate(result['stderr'])}")
    parts.append(f"[exit code: {result['exit_code']}]")
    return "\n".join(parts)


_DISPATCH = {
    "list_dir": _do_list_dir,
    "read_file": _do_read_file,
    "write_file": _do_write_file,
    "edit_file": _do_edit_file,
    "run_bash": _do_run_bash,
}


def execute_tool(
    user: str,
    name: str,
    args: dict,
    *,
    register_file: RegisterFile | None = None,
) -> str:
    """Run one tool call for `user` and return a text result for the model.

    All user-caused failures (bad path, missing file, escape attempt, non-zero
    exit) return an error string rather than raising — the model reads it and
    adapts. Only an unknown tool name is treated as a hard error string too.

    `register_file`, when provided, lets create_document register its output in
    the Files API so the user can download it.
    """
    try:
        if name == "create_document":
            return _do_create_document(user, args or {}, register_file)
        handler = _DISPATCH.get(name)
        if handler is None:
            return f"Error: unknown tool {name!r}"
        return handler(user, args or {})
    except WorkspaceError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001 — a tool bug must not kill the loop
        return f"Error: {type(exc).__name__}: {exc}"
