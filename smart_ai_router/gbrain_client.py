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
            self.bin_path = bun_path
            return

        # Not found
        raise RuntimeError(
            f"gbrain binary not found at {self.bin_path} or {bun_path}. "
            "Install via: bun install -g github:garrytan/gbrain"
        )

    def _call(self, tool: str, args: dict[str, Any]) -> Any:
        """
        Execute a GBrain CLI call.

        Args:
            tool: Tool name (e.g., 'query', 'search', 'remember')
            args: JSON-serializable arguments dict

        Returns:
            Parsed JSON response from GBrain

        Raises:
            RuntimeError: If the CLI call fails
        """
        try:
            result = subprocess.run(
                [self.bin_path, "call", tool, json.dumps(args)],
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
        except FileNotFoundError:
            raise RuntimeError(f"gbrain binary not found: {self.bin_path}")

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
