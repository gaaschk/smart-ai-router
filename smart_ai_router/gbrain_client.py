"""
GBrain client — Python subprocess wrapper for the GBrain CLI.

GBrain exposes ~40 tools via `gbrain call <tool> '<json-args>'` (JSON-in/JSON-out).
This module wraps that subprocess interface for use by the proxy layer.

Unlike the Node backend, we don't need a call queue here because the Python
proxy is synchronous (FastAPI) and doesn't run concurrent requests — async
operations still serialize at the subprocess level via the event loop.
"""
import json
import logging
import os
import re
import subprocess
import shutil
from typing import Any, Optional

logger = logging.getLogger(__name__)

# GBrain source ids are constrained to [a-z0-9-]{1,32} (immutable citation key —
# see `gbrain sources --help`). request.state.user is not: "admin" is fine, but
# "anon:<session>" has a colon, a self-serve id is "u:<hex>", and an
# operator-chosen user label could be anything typed into the Keys page. This
# maps any of those deterministically onto a valid source id, so the same user
# always lands on the same brain and two different users can't collide.
_SOURCE_SLUG_RE = re.compile(r"[^a-z0-9-]+")

# _call()/_cli() append `source_id` as a literal argv element after `--source`.
# subprocess.run() with a list (never shell=True) already rules out shell
# injection, but an *unvalidated* value could still be misread as another CLI
# flag by gbrain's own arg parser (e.g. a source id of "--help" or "-x"), which
# is a real command-line-manipulation vector even without a shell involved --
# CodeQL's uncontrolled-command-line check flags exactly this. Every source_id
# reaching _call() is expected to already be either "" or the output of
# source_id_for_user() (which only ever emits this charset), so this is a
# defense-in-depth assertion, not the primary sanitizer -- see
# source_id_for_user() for where the untrusted string is actually shaped.
#
# Must start AND end with an alphanumeric character: a bare `[a-z0-9-]{1,32}`
# class would still accept "--help" or "-x" (every one of those characters is
# individually allowed), which is exactly the flag-injection shape this check
# exists to block. Requiring alnum on both ends rules that out.
_VALID_SOURCE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")


def source_id_for_user(user: str) -> str:
    """The per-user GBrain source id for `user`, or "" for the shared/global brain.

    "" is returned for admin and for the empty/open-mode identity -- both keep
    the pre-isolation behavior of reading/writing the brain's default source,
    which is what already holds the project's own imported documentation (see
    docs/gbrain-deployment.md). Giving admin a *second*, empty, per-admin source
    would silently break the existing RAG-over-project-docs behavior for the
    identity most likely to be testing it.

    Every other identity (a per-user key, `anon:<session>`, `u:<self-serve>`)
    gets its own source, slugified from the raw identity string. Deterministic
    (same input -> same output) so isolation doesn't depend on storing a mapping
    anywhere -- the identity string itself *is* the lookup key.
    """
    user = (user or "").strip()
    if not user or user == "admin":
        return ""
    slug = _SOURCE_SLUG_RE.sub("-", user.lower()).strip("-")
    if not slug:
        # Nothing alnum survived (e.g. a user made entirely of punctuation) --
        # fall back to a stable hash so the identity still gets *a* source
        # rather than silently sharing the global one.
        import hashlib
        slug = hashlib.sha1(user.encode()).hexdigest()[:16]
    if len(slug) > 32:
        # Truncate, but keep a hash suffix so two long labels that agree on
        # their first 23 characters don't collide onto the same source.
        import hashlib
        h = hashlib.sha1(user.encode()).hexdigest()[:8]
        slug = f"{slug[:23]}-{h}"
    return slug

# Built-in Minions job types safe to submit on demand from a web UI.
# `shell` is deliberately excluded to match the Node dashboard's policy --
# the MCP layer itself rejects it, and we don't want an arbitrary-command
# trigger reachable from a browser anyway.
RUNNABLE_JOBS: dict[str, str] = {
    "sync": "Incrementally sync a git repo into the brain",
    "embed": "Generate/refresh embeddings for semantic search",
    "lint": "Catch LLM artifacts, placeholder dates, and bad frontmatter",
    "import": "Import a markdown directory into the brain",
    "extract": "Extract links and/or timeline entries from page content",
    "backlinks": "Find and fix missing back-links across the brain",
    "autopilot-cycle": "Run one overnight-maintenance enrichment cycle now",
}


class GBrainClient:
    """Subprocess-based client for GBrain CLI."""

    def __init__(self, bin_path: str = "gbrain", timeout_ms: int = 30000):
        """
        Initialize GBrain client.

        Args:
            bin_path: Path to gbrain binary (or just 'gbrain' if on PATH)
            timeout_ms: Timeout in milliseconds for CLI calls
        """
        self.bin_path = bin_path
        self.timeout_s = timeout_ms / 1000.0
        self._check_binary()

    def _check_binary(self):
        """Verify gbrain binary exists and is executable."""
        # Check the default location first (in PATH)
        if shutil.which(self.bin_path):
            return

        # Check common Bun installation path
        home = os.path.expanduser("~")
        bun_path = os.path.join(home, ".bun", "bin", "gbrain")
        if os.path.isfile(bun_path):
            # gbrain is a /usr/bin/env bun shebang script, so we need to call
            # bun explicitly with the script path when bun isn't in PATH.
            # Store as a tuple: (runner, script) for use in _call() and _cli()
            bun_bin = shutil.which("bun") or os.path.join(home, ".bun", "bin", "bun")
            if not os.path.isfile(bun_bin):
                raise RuntimeError(f"bun binary not found (required to run {bun_path})")
            self.bin_path = (bun_bin, bun_path)
            return

        # Not found
        raise RuntimeError(
            f"gbrain binary not found at {self.bin_path} or {bun_path}. "
            "Install via: bun install -g github:garrytan/gbrain"
        )

    def _call(self, tool: str, args: dict[str, Any], source_id: str = "") -> Any:
        """
        Execute a GBrain CLI call via `gbrain call <tool> '<json>' [--source <id>]`.

        Args:
            tool: Tool name (e.g., 'query', 'search', 'remember')
            args: JSON-serializable arguments dict
            source_id: Optional GBrain source to scope this call to (per-user
                isolation -- see source_id_for_user()). "" scopes to the brain's
                default source, matching pre-isolation behavior. Must come
                *after* the JSON payload on the command line -- gbrain rejects
                it before the payload for `call` (unlike `sources` subcommands).

        Returns:
            Parsed JSON response from GBrain

        Raises:
            RuntimeError: If the CLI call fails
        """
        try:
            # Handle bin_path as either a string (in PATH) or a tuple (bun, script)
            if isinstance(self.bin_path, tuple):
                cmd = [self.bin_path[0], self.bin_path[1], "call", tool, json.dumps(args)]
            else:
                cmd = [self.bin_path, "call", tool, json.dumps(args)]
            if source_id:
                # Reject anything that isn't a well-formed source id *before* it
                # reaches argv -- see _VALID_SOURCE_ID_RE for why this matters
                # even with subprocess.run()'s list form (no shell involved).
                if not _VALID_SOURCE_ID_RE.match(source_id):
                    raise RuntimeError(f"invalid GBrain source id: {source_id!r}")
                cmd += ["--source", source_id]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )

            if result.returncode != 0:
                stderr_msg = (result.stderr or result.stdout or "").strip()
                raise RuntimeError(
                    f"gbrain {tool} failed: {stderr_msg or 'unknown error'}"
                )

            output = result.stdout.strip()
            if not output:
                return None

            # Try to parse as JSON; if not JSON, return the string
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                return output

        except subprocess.TimeoutExpired:
            raise RuntimeError(f"gbrain {tool} timed out after {self.timeout_s}s")
        except FileNotFoundError as e:
            bin_display = self.bin_path[1] if isinstance(self.bin_path, tuple) else self.bin_path
            raise RuntimeError(f"gbrain binary not found: {bin_display} ({e})")

    def _cli(self, args: list[str]) -> Any:
        """
        Execute a GBrain CLI command directly (e.g., integrations list --json).

        Args:
            args: Full command args after 'gbrain' (e.g., ['integrations', 'list', '--json'])

        Returns:
            Parsed JSON response from GBrain

        Raises:
            RuntimeError: If the CLI call fails
        """
        try:
            # Handle bin_path as either a string (in PATH) or a tuple (bun, script)
            if isinstance(self.bin_path, tuple):
                cmd = [self.bin_path[0], self.bin_path[1]] + args
            else:
                cmd = [self.bin_path] + args
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )

            if result.returncode != 0:
                stderr_msg = (result.stderr or result.stdout or "").strip()
                raise RuntimeError(
                    f"gbrain {' '.join(args)} failed: {stderr_msg or 'unknown error'}"
                )

            output = result.stdout.strip()
            if not output:
                return None

            # Try to parse as JSON; if not JSON, return the string
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                return output

        except subprocess.TimeoutExpired:
            raise RuntimeError(f"gbrain {' '.join(args)} timed out after {self.timeout_s}s")
        except FileNotFoundError as e:
            bin_display = self.bin_path[1] if isinstance(self.bin_path, tuple) else self.bin_path
            raise RuntimeError(f"gbrain binary not found: {bin_display} ({e})")

    # ===== Public API =====

    def ensure_source(self, source_id: str, name: str = "") -> bool:
        """Register `source_id` as a GBrain source if it doesn't exist yet.

        Idempotent: `sources_add` on an existing id returns a "already
        registered" error (not an exception from gbrain -- it's printed to
        stdout/stderr and gbrain still exits 0 for it in some paths, but we
        treat any RuntimeError here as "probably already exists" and move on),
        so callers can call this on every request without checking first.
        `federated: false` keeps a per-user source out of the default
        cross-source search -- see gbrain_client.py module docs and
        docs/gbrain-deployment.md for why that matters for isolation.
        """
        if not source_id:
            return True
        try:
            self._call(
                "sources_add",
                {"id": source_id, "name": name or source_id, "federated": False},
            )
            return True
        except RuntimeError as e:
            # Idempotent by design: "already registered" is the expected path
            # on every request after the first for a given user. Only log at
            # debug so this isn't noisy on the hot path.
            logger.debug(f"GBrain ensure_source({source_id}): {e}")
            return True

    def search(
        self, query: str, limit: int = 10, source_id: str = ""
    ) -> list[dict[str, Any]]:
        """Keyword (full-text) search — fast, no LLM involved."""
        try:
            results = self._call(
                "search", {"query": query, "limit": limit}, source_id=source_id
            )
            return results if isinstance(results, list) else []
        except RuntimeError as e:
            logger.warning(f"GBrain search failed: {e}")
            return []

    def hybrid_query(
        self,
        query: str,
        limit: int = 10,
        expand: bool = True,
        source_id: str = "",
    ) -> list[dict[str, Any]]:
        """Hybrid vector + keyword search with query expansion — the 'smart' search."""
        try:
            results = self._call(
                "query",
                {"query": query, "limit": limit, "expand": expand},
                source_id=source_id,
            )
            return results if isinstance(results, list) else []
        except RuntimeError as e:
            logger.warning(f"GBrain hybrid search failed: {e}")
            return []

    def remember(
        self,
        title: str,
        content: str,
        entity: str = "chat-learnings",
        source_id: str = "",
        provenance: str = "smart-ai-router chat",
    ) -> Optional[dict[str, Any]]:
        """Save a fact/learning to the brain.

        `provenance` is a required field on the underlying `remember` tool
        (free text describing where the fact came from) -- omitting it is a
        hard error, not a default-filled optional, so a caller-overridable
        value with a sane default is passed on every call.
        """
        try:
            result = self._call(
                "remember",
                {
                    "fact": f"{title}\n\n{content}",
                    "visibility": "private",
                    "entity": entity,
                    "provenance": provenance,
                },
                source_id=source_id,
            )
            return result
        except RuntimeError as e:
            logger.warning(f"GBrain remember failed: {e}")
            return None

    def get_stats(self) -> dict[str, Any]:
        """Brain-wide stats: page/chunk/link/tag counts, pages by type.

        Not source-scoped -- GBrain's get_stats always reports across the
        whole brain regardless of --source (verified against a live instance;
        see docs/gbrain-deployment.md).
        """
        try:
            result = self._call("get_stats", {})
            return result if isinstance(result, dict) else {}
        except RuntimeError as e:
            logger.warning(f"GBrain get_stats failed: {e}")
            return {}

    def get_health(self) -> dict[str, Any]:
        """Brain health score: embed coverage, stale/orphan pages, dead links, etc.

        Not source-scoped, same as get_stats.
        """
        try:
            result = self._call("get_health", {})
            return result if isinstance(result, dict) else {}
        except RuntimeError as e:
            logger.warning(f"GBrain get_health failed: {e}")
            return {}

    def list_pages(
        self, type: str = "", tag: str = "", limit: int = 50, source_id: str = ""
    ) -> list[dict[str, Any]]:
        """List pages, optionally filtered by type/tag -- the browsable page index."""
        try:
            params: dict[str, Any] = {"limit": limit}
            if type:
                params["type"] = type
            if tag:
                params["tag"] = tag
            result = self._call("list_pages", params, source_id=source_id)
            return result if isinstance(result, list) else []
        except RuntimeError as e:
            logger.warning(f"GBrain list_pages failed: {e}")
            return []

    def get_page(
        self, slug: str, fuzzy: bool = False, source_id: str = ""
    ) -> Optional[dict[str, Any]]:
        """Fetch a single page by slug."""
        try:
            result = self._call(
                "get_page", {"slug": slug, "fuzzy": fuzzy}, source_id=source_id
            )
            return result if isinstance(result, dict) else None
        except RuntimeError as e:
            logger.warning(f"GBrain get_page failed: {e}")
            return None

    def get_tags(self, slug: str, source_id: str = "") -> list[str]:
        """Tags attached to a page."""
        try:
            result = self._call("get_tags", {"slug": slug}, source_id=source_id)
            return result if isinstance(result, list) else []
        except RuntimeError as e:
            logger.warning(f"GBrain get_tags failed: {e}")
            return []

    def get_links(self, slug: str, source_id: str = "") -> list[Any]:
        """Outgoing links from a page."""
        try:
            result = self._call("get_links", {"slug": slug}, source_id=source_id)
            return result if isinstance(result, list) else []
        except RuntimeError as e:
            logger.warning(f"GBrain get_links failed: {e}")
            return []

    def get_backlinks(self, slug: str, source_id: str = "") -> list[Any]:
        """Pages that link to this page."""
        try:
            result = self._call(
                "get_backlinks", {"slug": slug}, source_id=source_id
            )
            return result if isinstance(result, list) else []
        except RuntimeError as e:
            logger.warning(f"GBrain get_backlinks failed: {e}")
            return []

    def list_integrations(self) -> dict[str, Any]:
        """List available GBrain integrations (infra, senses, reflexes)."""
        try:
            result = self._cli(["integrations", "list", "--json"])
            # Result is a dict with 'infra', 'senses', 'reflexes' keys
            return result if isinstance(result, dict) else {}
        except RuntimeError as e:
            logger.warning(f"GBrain integrations list failed: {e}")
            return {}

    def get_integration_status(self, integration_id: str) -> dict[str, Any]:
        """Status + secrets + heartbeat for one integration."""
        try:
            result = self._cli(["integrations", "status", integration_id, "--json"])
            return result if isinstance(result, dict) else {}
        except RuntimeError as e:
            logger.warning(f"GBrain integration status failed: {e}")
            return {}

    def list_jobs(
        self,
        status: str = "",
        queue: str = "",
        name: str = "",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """List background jobs (Minions queue)."""
        try:
            params: dict[str, Any] = {"limit": limit}
            if status:
                params["status"] = status
            if queue:
                params["queue"] = queue
            if name:
                params["name"] = name
            result = self._call("list_jobs", params)
            return result if isinstance(result, list) else []
        except RuntimeError as e:
            logger.warning(f"GBrain list_jobs failed: {e}")
            return []

    def get_job(self, job_id: int) -> Optional[dict[str, Any]]:
        """Fetch a single job's status/result by id."""
        try:
            result = self._call("get_job", {"id": job_id})
            return result if isinstance(result, dict) else None
        except RuntimeError as e:
            logger.warning(f"GBrain get_job failed: {e}")
            return None

    def submit_job(
        self,
        name: str,
        data: Optional[dict[str, Any]] = None,
        queue: str = "",
        priority: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """
        Submit a background job to the Minions queue.

        Only a fixed allow-list of built-in job types is exposed to callers
        (see RUNNABLE_JOBS) -- `shell` is deliberately excluded, matching the
        Node dashboard's policy: we don't want an arbitrary-command trigger
        reachable from a web UI.
        """
        try:
            args: dict[str, Any] = {"name": name, "data": data or {}}
            if queue:
                args["queue"] = queue
            if priority is not None:
                args["priority"] = priority
            result = self._call("submit_job", args)
            return result if isinstance(result, dict) else None
        except RuntimeError as e:
            logger.warning(f"GBrain submit_job failed: {e}")
            return None


# Singleton instance
_client: Optional[GBrainClient] = None


def get_client(bin_path: str = "gbrain", timeout_ms: int = 30000) -> GBrainClient:
    """Get or create the global GBrain client."""
    global _client
    if _client is None:
        try:
            _client = GBrainClient(bin_path=bin_path, timeout_ms=timeout_ms)
        except RuntimeError as e:
            logger.warning(f"GBrain not available: {e}")
            # Return a dummy client that always returns empty results
            return _DummyGBrainClient()
    return _client


class _DummyGBrainClient:
    """Fallback client when GBrain is not available. Always returns empty results."""

    def ensure_source(self, source_id: str, name: str = "") -> bool:
        return True

    def search(
        self, query: str, limit: int = 10, source_id: str = ""
    ) -> list[dict[str, Any]]:
        return []

    def hybrid_query(
        self,
        query: str,
        limit: int = 10,
        expand: bool = True,
        source_id: str = "",
    ) -> list[dict[str, Any]]:
        return []

    def remember(
        self,
        title: str,
        content: str,
        entity: str = "chat-learnings",
        source_id: str = "",
        provenance: str = "smart-ai-router chat",
    ) -> Optional[dict[str, Any]]:
        return None

    def get_stats(self) -> dict[str, Any]:
        return {}

    def get_health(self) -> dict[str, Any]:
        return {}

    def list_pages(
        self, type: str = "", tag: str = "", limit: int = 50, source_id: str = ""
    ) -> list[dict[str, Any]]:
        return []

    def get_page(
        self, slug: str, fuzzy: bool = False, source_id: str = ""
    ) -> Optional[dict[str, Any]]:
        return None

    def get_tags(self, slug: str, source_id: str = "") -> list[str]:
        return []

    def get_links(self, slug: str, source_id: str = "") -> list[Any]:
        return []

    def get_backlinks(self, slug: str, source_id: str = "") -> list[Any]:
        return []

    def list_integrations(self) -> dict[str, Any]:
        return {}

    def get_integration_status(self, integration_id: str) -> dict[str, Any]:
        return {}

    def list_jobs(
        self, status: str = "", queue: str = "", name: str = "", limit: int = 10
    ) -> list[dict[str, Any]]:
        return []

    def get_job(self, job_id: int) -> Optional[dict[str, Any]]:
        return None

    def submit_job(
        self,
        name: str,
        data: Optional[dict[str, Any]] = None,
        queue: str = "",
        priority: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        return None
