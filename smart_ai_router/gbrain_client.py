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
import subprocess
import shutil
from typing import Any, Optional

logger = logging.getLogger(__name__)

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

    def _call(self, tool: str, args: dict[str, Any]) -> Any:
        """
        Execute a GBrain CLI call via `gbrain call <tool> '<json>'`.

        Args:
            tool: Tool name (e.g., 'query', 'search', 'remember')
            args: JSON-serializable arguments dict

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

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Keyword (full-text) search — fast, no LLM involved."""
        try:
            results = self._call("search", {"query": query, "limit": limit})
            return results if isinstance(results, list) else []
        except RuntimeError as e:
            logger.warning(f"GBrain search failed: {e}")
            return []

    def hybrid_query(
        self, query: str, limit: int = 10, expand: bool = True
    ) -> list[dict[str, Any]]:
        """Hybrid vector + keyword search with query expansion — the 'smart' search."""
        try:
            results = self._call("query", {"query": query, "limit": limit, "expand": expand})
            return results if isinstance(results, list) else []
        except RuntimeError as e:
            logger.warning(f"GBrain hybrid search failed: {e}")
            return []

    def remember(
        self, title: str, content: str, entity: str = "chat-learnings"
    ) -> Optional[dict[str, Any]]:
        """Save a fact/learning to the brain."""
        try:
            result = self._call(
                "remember",
                {
                    "fact": f"{title}\n\n{content}",
                    "visibility": "private",
                    "entity": entity,
                },
            )
            return result
        except RuntimeError as e:
            logger.warning(f"GBrain remember failed: {e}")
            return None

    def get_stats(self) -> dict[str, Any]:
        """Brain-wide stats: page/chunk/link/tag counts, pages by type."""
        try:
            result = self._call("get_stats", {})
            return result if isinstance(result, dict) else {}
        except RuntimeError as e:
            logger.warning(f"GBrain get_stats failed: {e}")
            return {}

    def get_health(self) -> dict[str, Any]:
        """Brain health score: embed coverage, stale/orphan pages, dead links, etc."""
        try:
            result = self._call("get_health", {})
            return result if isinstance(result, dict) else {}
        except RuntimeError as e:
            logger.warning(f"GBrain get_health failed: {e}")
            return {}

    def list_pages(
        self, type: str = "", tag: str = "", limit: int = 50
    ) -> list[dict[str, Any]]:
        """List pages, optionally filtered by type/tag -- the browsable page index."""
        try:
            params: dict[str, Any] = {"limit": limit}
            if type:
                params["type"] = type
            if tag:
                params["tag"] = tag
            result = self._call("list_pages", params)
            return result if isinstance(result, list) else []
        except RuntimeError as e:
            logger.warning(f"GBrain list_pages failed: {e}")
            return []

    def get_page(self, slug: str, fuzzy: bool = False) -> Optional[dict[str, Any]]:
        """Fetch a single page by slug."""
        try:
            result = self._call("get_page", {"slug": slug, "fuzzy": fuzzy})
            return result if isinstance(result, dict) else None
        except RuntimeError as e:
            logger.warning(f"GBrain get_page failed: {e}")
            return None

    def get_tags(self, slug: str) -> list[str]:
        """Tags attached to a page."""
        try:
            result = self._call("get_tags", {"slug": slug})
            return result if isinstance(result, list) else []
        except RuntimeError as e:
            logger.warning(f"GBrain get_tags failed: {e}")
            return []

    def get_links(self, slug: str) -> list[Any]:
        """Outgoing links from a page."""
        try:
            result = self._call("get_links", {"slug": slug})
            return result if isinstance(result, list) else []
        except RuntimeError as e:
            logger.warning(f"GBrain get_links failed: {e}")
            return []

    def get_backlinks(self, slug: str) -> list[Any]:
        """Pages that link to this page."""
        try:
            result = self._call("get_backlinks", {"slug": slug})
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

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return []

    def hybrid_query(
        self, query: str, limit: int = 10, expand: bool = True
    ) -> list[dict[str, Any]]:
        return []

    def remember(
        self, title: str, content: str, entity: str = "chat-learnings"
    ) -> Optional[dict[str, Any]]:
        return None

    def get_stats(self) -> dict[str, Any]:
        return {}

    def get_health(self) -> dict[str, Any]:
        return {}

    def list_pages(self, type: str = "", tag: str = "", limit: int = 50) -> list[dict[str, Any]]:
        return []

    def get_page(self, slug: str, fuzzy: bool = False) -> Optional[dict[str, Any]]:
        return None

    def get_tags(self, slug: str) -> list[str]:
        return []

    def get_links(self, slug: str) -> list[Any]:
        return []

    def get_backlinks(self, slug: str) -> list[Any]:
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
