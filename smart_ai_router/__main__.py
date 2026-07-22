"""CLI entry point: `python -m smart_ai_router` or `smart-ai-router`."""
import os
import sys

import uvicorn

from smart_ai_router.api import create_app


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        from smart_ai_router.setup import run_setup
        run_setup()
        return

    port = int(os.environ.get("SMART_ROUTER_PORT", "8001"))
    uvicorn.run(create_app(), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
