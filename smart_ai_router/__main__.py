"""CLI entry point: `python -m smart_ai_router` or `smart-ai-router`."""
import os
import sys
from pathlib import Path

import uvicorn

from smart_ai_router.api import create_app


def _load_dotenv() -> None:
    """Load .env from the project root if present."""
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.is_file():
        env_path = Path.cwd() / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        from smart_ai_router.setup import run_setup
        run_setup()
        return

    _load_dotenv()
    port = int(os.environ.get("SMART_ROUTER_PORT", "8001"))
    uvicorn.run(create_app(), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
