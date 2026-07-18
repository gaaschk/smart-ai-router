"""CLI entry point: `python -m smart_ai_router` or `smart-ai-router`."""
import uvicorn
from smart_ai_router.api import create_app


def main():
    uvicorn.run(create_app(), host="0.0.0.0", port=8001)


if __name__ == "__main__":
    main()
