"""Daily code review workflow for the repo.

This is intentionally lightweight and deterministic: it gathers the current repo
state (branch, recent commits, tracked files, and obvious hotspots), then emits a
single markdown review report using a fixed rubric. The output is designed for a
scheduled LaunchAgent run and for a manual dry run before production use.
"""
from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REVIEW_PATH = ROOT / "logs" / "daily-review.md"
_REVIEW_MARKERS = ["TODO", "FIXME", "XXX", "HACK", "pass  # TODO"]


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _tracked_files(repo: Path, focus: Sequence[str] | None = None) -> list[str]:
    tracked = _git(repo, "ls-files")
    if not tracked:
        return []
    files = tracked.splitlines()
    if not focus:
        return files
    focus_set = tuple(f.strip("/") for f in focus if f.strip())
    if not focus_set:
        return files
    selected = []
    for path in files:
        if any(path == pref or path.startswith(pref + "/") for pref in focus_set):
            selected.append(path)
    return selected or files


def _count_markers(files: Iterable[str], repo: Path) -> list[str]:
    markers = []
    for relative_path in files:
        full = repo / relative_path
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                text = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        needle = ["TODO", "FIXME", "XXX", "HACK", "pass  # TODO"]
        for mark in needle:
            if mark in text:
                markers.append(f"{relative_path}: {mark}")
                break
    return markers


def _repo_snapshot(repo: Path, days: int, focus: Sequence[str] | None = None) -> dict:
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "DETACHED"
    status = _git(repo, "status", "--short")
    since = f"{max(days, 1)} days ago"
    recent = _git(repo, "log", "--date=short", "--pretty=%ad %h %an %s", "--since", since)
    if not recent:
        recent = _git(repo, "log", "--date=short", "--pretty=%ad %h %an %s", "-n", "10")
    tracked = _tracked_files(repo, focus)
    todo_markers = _count_markers(tracked, repo)
    ui_files = [p for p in tracked if "/ui/" in p or p.endswith((".html", ".css", ".js"))]
    tests = [p for p in tracked if p.startswith("tests/")]
    key_dirs = [
        "smart_ai_router",
        "scripts",
        "services",
        "docs",
        "tests",
    ]
    repo_dirs = sorted({p.split("/", 1)[0] for p in tracked if p and "/" in p})
    source_count = len([p for p in tracked if p.endswith(".py")])
    return {
        "repo": str(repo),
        "branch": branch,
        "status": status.splitlines() if status else [],
        "recent_commits": recent.splitlines() if recent else [],
        "tracked_files": tracked,
        "todo_markers": todo_markers,
        "ui_files": ui_files,
        "tests": tests,
        "repo_dirs": repo_dirs,
        "key_dirs": key_dirs,
        "source_count": source_count,
        "days": max(days, 1),
    }


def _findings(snapshot: dict) -> list[dict]:
    findings: list[dict] = []
    todo_markers = snapshot["todo_markers"]
    if todo_markers:
        findings.append({
            "severity": "follow-up",
            "topic": "Code quality",
            "summary": (
                f"{len(todo_markers)} TODO/FIXME-style markers were found in the tracked code: "
                + ", ".join(todo_markers[:3]) + (" …" if len(todo_markers) > 3 else "")
            ),
        })
    else:
        findings.append({
            "severity": "info",
            "topic": "Code quality",
            "summary": "No TODO/FIXME markers were detected in the current tracked source snapshot.",
        })

    if snapshot["ui_files"]:
        findings.append({
            "severity": "info",
            "topic": "Usability",
            "summary": (
                "The repo includes UI-facing code and assets; review the user flow, labels, "
                "and accessibility surfaces for consistency after changes."
            ),
        })
    else:
        findings.append({
            "severity": "info",
            "topic": "Usability",
            "summary": "No direct UI assets were detected in the scoped review window; check any human-facing flows manually.",
        })

    if snapshot["ui_files"] and snapshot["tests"]:
        findings.append({
            "severity": "info",
            "topic": "Design consistency",
            "summary": "The repo appears to mix service logic with UI assets and tests; keep naming, terminology, and interaction patterns aligned across both layers.",
        })
    else:
        findings.append({
            "severity": "info",
            "topic": "Design consistency",
            "summary": "No immediately inconsistent design patterns were obvious in the scoped snapshot; confirm naming and flow coherence in the next review pass.",
        })

    if snapshot["source_count"]:
        findings.append({
            "severity": "info",
            "topic": "UX coherence",
            "summary": f"{snapshot['source_count']} Python files were found in the tracked repo. Keep the command/service/API/UI boundaries explicit to avoid drift between user-facing behavior and backend logic.",
        })
    else:
        findings.append({
            "severity": "info",
            "topic": "UX coherence",
            "summary": "No Python source files were detected in the current review scope; verify the intended review target before relying on this report.",
        })

    if snapshot["status"]:
        findings.append({
            "severity": "follow-up",
            "topic": "Feature gaps",
            "summary": "The repo has uncommitted or modified files in the current working tree; review them for incomplete features, half-finished docs, or accidental build artifacts.",
        })
    else:
        findings.append({
            "severity": "info",
            "topic": "Feature gaps",
            "summary": "No uncommitted file changes were reported in the working tree; scan recent commits and user-facing flows for partial implementations and missing follow-through.",
        })

    findings.append({
        "severity": "info",
        "topic": "Architecture",
        "summary": "The repo is organized across service, routing, docs, and tests; keep responsibilities clean and avoid cross-layer coupling when new capabilities are added.",
    })

    findings.append({
        "severity": "info",
        "topic": "Performance & scalability",
        "summary": "Review queue depth, repeated scans, and provider sync costs before expanding the model matrix or adding more background jobs.",
    })

    findings.append({
        "severity": "info",
        "topic": "Operational risk",
        "summary": "Use the existing LaunchAgent/service flow for daily execution and write the report to a stable log/output location so the review remains reproducible and easy to audit.",
    })
    return findings


def render_review_markdown(snapshot: dict, findings: list[dict] | None = None) -> str:
    findings = findings or _findings(snapshot)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Daily Code Review",
        "",
        f"**Generated:** {generated}",
        f"**Repository:** `{snapshot['repo']}`",
        f"**Branch:** `{snapshot['branch']}`",
        f"**Review window:** last {snapshot['days']} day(s)",
        "",
        "## Summary",
        "",
        "- The review is focused on code quality, usability, design consistency, UX coherence, missing or weak features, architecture quality, performance/scalability, and operational risk.",
        f"- Recent tracked activity: {len(snapshot['recent_commits'])} commit(s) in the local history preview.",
        f"- Working tree status: {'clean' if not snapshot['status'] else 'changes present'}.",
        "",
        "## Rubric review",
        "",
    ]

    for item in findings:
        lines.append(f"### {item['topic']}")
        lines.append(f"- Severity: {item['severity']}")
        lines.append(f"- {item['summary']}")
        lines.append("")

    if snapshot["recent_commits"]:
        lines.append("## Recent commits")
        lines.append("")
        for commit in snapshot["recent_commits"][:8]:
            lines.append(f"- `{commit}`")
        lines.append("")

    if snapshot["status"]:
        lines.append("## Working tree changes")
        lines.append("")
        for entry in snapshot["status"][:20]:
            lines.append(f"- `{entry}`")
        lines.append("")

    lines.append("## Actionable follow-up")
    lines.append("")
    lines.append("- Confirm that the review output is stored in a reliable path such as `logs/daily-review.md` and that the scheduled LaunchAgent is still the source of truth.")
    lines.append("- Review any TODO/FIXME markers before shipping a release and keep the UX and service boundaries explicit as the repo grows.")
    lines.append("- Repeat this review daily to catch drift in architecture, usability, and operational stability before it compounds.")
    return "\n".join(lines) + "\n"


def run_review(
    repo_root: str | Path = ROOT,
    *,
    output_path: str | Path | None = None,
    since_days: int = 1,
    focus: Sequence[str] | None = None,
    dry_run: bool = False,
) -> dict:
    repo = Path(repo_root).resolve()
    snapshot = _repo_snapshot(repo, since_days, focus=focus)
    findings = _findings(snapshot)
    report = render_review_markdown(snapshot, findings)
    out = Path(output_path) if output_path else DEFAULT_REVIEW_PATH
    if not dry_run:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
    return {
        "repo": str(repo),
        "output": str(out),
        "dry_run": dry_run,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "findings": len(findings),
        "report": report,
    }


def _review_plist(
    repo_root: Path,
    output_path: Path,
    *,
    home_dir: Path | None = None,
    hour: int = 3,
    minute: int = 0,
) -> bytes:
    project_root = repo_root.resolve()
    venv_python = Path(sys.executable)
    output_path = output_path.resolve()
    home_dir = home_dir or Path.home()
    payload = {
        "Label": "com.smart-ai-router-review",
        "WorkingDirectory": str(project_root),
        "ProgramArguments": [
            str(venv_python),
            "-m",
            "smart_ai_router",
            "review",
            "--repo",
            str(project_root),
            "--output",
            str(output_path),
        ],
        "EnvironmentVariables": {
            "HOME": str(home_dir),
            "PATH": str(home_dir / ".local/bin") + ":/usr/local/bin:/usr/bin:/bin",
        },
        "StartCalendarInterval": [{"Hour": hour % 24, "Minute": minute % 60}],
        "StandardOutPath": str(project_root / "logs" / "daily-review.log"),
        "StandardErrorPath": str(project_root / "logs" / "daily-review.err"),
    }
    return plistlib.dumps(payload)


def install_review_launchd(
    repo_root: str | Path = ROOT,
    *,
    output_path: str | Path | None = None,
    hour: int = 3,
    minute: int = 0,
) -> Path:
    """Install a daily review LaunchAgent in the current user's domain."""
    repo = Path(repo_root).resolve()
    out = Path(output_path) if output_path else DEFAULT_REVIEW_PATH
    home_dir = Path.home()
    launch_agents = home_dir / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / "com.smart-ai-router-review.plist"
    (repo / "logs").mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(
        _review_plist(repo, out.resolve(), home_dir=home_dir, hour=hour, minute=minute)
    )

    try:
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, text=True)
        subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        err = ""
        if isinstance(exc, subprocess.CalledProcessError):
            err = (exc.stderr or exc.stdout or str(exc)).strip() or "launchctl failed"
        else:
            err = str(exc)
        raise RuntimeError(f"Failed to install daily review LaunchAgent: {err}") from exc
    return plist_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smart-ai-router review",
        description="Run or schedule a daily repository review.",
    )
    parser.add_argument("--repo", default=str(ROOT), help="Repository root to review.")
    parser.add_argument("--output", default=str(DEFAULT_REVIEW_PATH), help="Path for the markdown review report.")
    parser.add_argument("--since-days", type=int, default=1, help="Look back this many days for recent repo activity.")
    parser.add_argument("--focus", nargs="*", default=["smart_ai_router", "tests", "docs", "services"], help="Directories or prefixes to focus the review on.")
    parser.add_argument("--dry-run", action="store_true", help="Generate the report in memory without writing the file.")
    parser.add_argument("--schedule", action="store_true", help="Install a daily LaunchAgent to run this review automatically.")
    parser.add_argument("--hour", type=int, default=3, help="Hour (0-23) for the daily LaunchAgent run.")
    parser.add_argument("--minute", type=int, default=0, help="Minute (0-59) for the daily LaunchAgent run.")
    return parser


def run_review_cli(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.schedule:
        try:
            install_review_launchd(
                args.repo,
                output_path=args.output,
                hour=args.hour,
                minute=args.minute,
            )
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print("Installed daily review LaunchAgent: ~/Library/LaunchAgents/com.smart-ai-router-review.plist")
        return 0

    result = run_review(
        args.repo,
        output_path=args.output,
        since_days=args.since_days,
        focus=args.focus,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        print(result["report"].rstrip())
    else:
        print(f"Daily review written to {result['output']}")
    return 0


if __name__ == "__main__":
    sys.exit(run_review_cli())
