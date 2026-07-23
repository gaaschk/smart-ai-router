"""First-run setup wizard: `smart-ai-router setup`."""
from __future__ import annotations

import getpass
import os
import platform
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ProviderConfig

PROJECT_ROOT = Path(__file__).parent.parent
PLIST_LABEL = "com.smart-ai-router"
DEFAULT_PORT = 8001


def _bold(text: str) -> str:
    return f"\033[1m{text}\033[0m"


def _green(text: str) -> str:
    return f"\033[32m{text}\033[0m"


def _yellow(text: str) -> str:
    return f"\033[33m{text}\033[0m"


def _ask(prompt: str, *, default: str = "", secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    full = f"  {prompt}{suffix}: "
    if secret:
        val = getpass.getpass(full)
    else:
        val = input(full)
    return val.strip() or default


def _ask_yn(prompt: str, *, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    val = input(f"  {prompt} [{hint}]: ").strip().lower()
    if not val:
        return default
    return val.startswith("y")


def _generate_plist(install_dir: Path, venv_python: Path, port: int) -> str:
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
          "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
          <key>Label</key>
          <string>{PLIST_LABEL}</string>

          <key>ProgramArguments</key>
          <array>
            <string>{venv_python}</string>
            <string>-m</string>
            <string>smart_ai_router</string>
          </array>

          <key>WorkingDirectory</key>
          <string>{install_dir}</string>

          <key>EnvironmentVariables</key>
          <dict>
            <key>HOME</key>
            <string>{Path.home()}</string>
            <key>PATH</key>
            <string>{Path.home() / ".local/bin"}:/usr/local/bin:/usr/bin:/bin</string>
            <key>SMART_ROUTER_PORT</key>
            <string>{port}</string>
            <key>SMART_ROUTER_LABEL</key>
            <string>{PLIST_LABEL}</string>
          </dict>

          <key>RunAtLoad</key>
          <true/>
          <key>KeepAlive</key>
          <true/>

          <key>StandardOutPath</key>
          <string>{install_dir / "logs/server.log"}</string>
          <key>StandardErrorPath</key>
          <string>{install_dir / "logs/server.err"}</string>
        </dict>
        </plist>
    """)


def _install_launchd(install_dir: Path, venv_python: Path, port: int) -> Path:
    launch_agents = Path.home() / "Library/LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / f"{PLIST_LABEL}.plist"

    (install_dir / "logs").mkdir(exist_ok=True)

    plist_path.write_text(_generate_plist(install_dir, venv_python, port))

    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        capture_output=True,
    )
    subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True,
        check=True,
    )
    return plist_path


def _install_claudish_smart(install_dir: Path) -> Path | None:
    script_src = install_dir / "scripts/claudish-smart"
    if not script_src.exists():
        return None

    bin_dir = Path.home() / ".local/bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    link = bin_dir / "claudish-smart"

    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(script_src)
    script_src.chmod(0o755)
    return link


def run_setup() -> None:
    print()
    print(_bold("smart-ai-router setup"))
    print("=" * 40)
    print()

    if platform.system() != "Darwin":
        print(_yellow("Warning: Only macOS is supported for now. Continuing anyway..."))
        print()

    # ── Providers ────────────────────────────────────────────────────────────
    print(_bold("Provider configuration"))
    print("  At least one provider is needed. You can configure multiple.")
    print()

    cr = CapabilityRouter()
    providers_configured = 0

    # OpenRouter
    if _ask_yn("Configure OpenRouter? (cloud models via openrouter.ai)"):
        key = _ask("OpenRouter API key", secret=True)
        if key:
            cr.upsert_provider(ProviderConfig(
                name="openrouter",
                kind="openrouter",
                api_key=key,
            ))
            providers_configured += 1
            print(_green("    Saved."))
        else:
            print("    Skipped (no key entered).")
    print()

    # Ollama
    if _ask_yn("Configure Ollama? (local models)"):
        url = _ask("Ollama base URL", default="http://localhost:11434")
        cr.upsert_provider(ProviderConfig(
            name="ollama",
            kind="ollama",
            base_url=url,
        ))
        providers_configured += 1
        print(_green("    Saved."))
    print()

    # Bedrock (for existing AWS users)
    if _ask_yn("Configure AWS Bedrock? (Claude models via AWS)", default=False):
        cr.upsert_provider(ProviderConfig(
            name="bedrock",
            kind="bedrock",
            api_key="aws",
        ))
        providers_configured += 1
        print(_green("    Saved. (Uses your existing AWS credentials/profile.)"))
    print()

    if providers_configured == 0:
        print(_yellow("No providers configured. You can add them later via the web UI."))
        print()

    # ── Initial sync ─────────────────────────────────────────────────────────
    if providers_configured > 0:
        print(_bold("Running initial model sync..."))
        result = cr.sync()
        if result.errors:
            for e in result.errors:
                print(_yellow(f"  Warning: {e}"))
        print(_green(f"  Synced {result.total} models ({result.added} new, {result.updated} updated)."))
        print()

    # ── Launchd service ──────────────────────────────────────────────────────
    install_dir = PROJECT_ROOT.resolve()
    venv_python = install_dir / ".venv/bin/python"

    if not venv_python.exists():
        print(_yellow(f"  Note: Expected venv at {venv_python}"))
        print("  The service will use whichever python ran this setup.")
        venv_python = Path(sys.executable)

    port = int(_ask("Service port", default=str(DEFAULT_PORT)))

    if platform.system() == "Darwin":
        print()
        print(_bold("Installing launchd service..."))
        plist_path = _install_launchd(install_dir, venv_python, port)
        print(_green(f"  Installed: {plist_path}"))
        print(f"  Service runs at boot (user domain, no sudo needed).")
    print()

    # ── claudish-smart symlink ───────────────────────────────────────────────
    print(_bold("Installing claudish-smart..."))
    link = _install_claudish_smart(install_dir)
    if link:
        print(_green(f"  Symlinked: {link}"))
        path_dirs = os.environ.get("PATH", "").split(":")
        if str(link.parent) not in path_dirs:
            print(_yellow(f"  Add {link.parent} to your PATH if not already there."))
    else:
        print(_yellow("  scripts/claudish-smart not found — skipped."))
    print()

    # ── Summary ──────────────────────────────────────────────────────────────
    hostname = platform.node() or "localhost"
    print("=" * 40)
    print(_bold("Setup complete!"))
    print()
    print(f"  Router URL:   http://{hostname}:{port}")
    print(f"  Web UI:       http://{hostname}:{port}/")
    print(f"  Models DB:    ~/.smart_ai_router.db")
    print()
    if link:
        print(f"  Launch Claude Code with the router:")
        print(f"    claudish-smart")
    print()
    print(f"  Manage service:")
    print(f"    launchctl kickstart -k gui/$(id -u)/{PLIST_LABEL}")
    print(f"    launchctl kill SIGTERM gui/$(id -u)/{PLIST_LABEL}")
    print()
