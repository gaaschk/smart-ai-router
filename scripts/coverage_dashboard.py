"""Generate offline coverage badge, trend dashboard and changed-line evidence."""

import argparse
import html
import json
import os
from pathlib import Path
import re
import subprocess


def changed_lines(diff, path):
    """Read hunk coordinates for a path supplied independently by Git -z."""
    lines = set()
    for line in diff.splitlines():
        if match := re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line):
            start, count = int(match[1]), int(match[2] or 1)
            lines.update(range(start, start + count))
    return {path: lines}


def git_changed_lines(base):
    # Display patch headers are quoted/delimited for humans, not lossless paths.
    # NUL-delimited names preserve spaces, tabs, newlines and literal quotes.
    raw_names = subprocess.check_output(
        [
            "git",
            "diff",
            "--no-ext-diff",
            "--name-status",
            "-z",
            "--find-renames",
            "--diff-filter=AMRT",
            base,
            "HEAD",
            "--",
        ],
    )
    # Detect renames repository-wide before filtering destinations: a move into
    # our scope must retain its old blob even when the source was outside it.
    names = iter(os.fsdecode(raw_names).split("\0"))
    changed = {}
    for status in names:
        if not status:
            continue
        old = next(names)
        path = next(names) if status.startswith("R") else old
        if not path.startswith(("smart_ai_router/", "scripts/")) or not path.endswith(
            ".py"
        ):
            continue
        if status.startswith("R"):
            # Blob-to-blob comparison avoids destination-only pathspecs hiding
            # the rename source, and maps hunk coordinates to current coverage.
            targets = [f"{base}:{old}", f"HEAD:{path}", "--"]
        else:
            targets = [base, "HEAD", "--", path]
        diff = subprocess.check_output(
            [
                "git",
                "--literal-pathspecs",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--unified=0",
                *targets,
            ],
            text=True,
        )
        lines = changed_lines(diff, path)[path]
        if lines:
            changed[path] = lines
    return changed


def diff_summary(files, changed):
    covered = total = 0
    missing = []
    for path, lines in changed.items():
        if not path.startswith(("smart_ai_router/", "scripts/")) or not path.endswith(
            ".py"
        ):
            continue
        if path not in files:
            missing.append(path)
            continue
        executed = set(files[path]["executed_lines"])
        executable = executed | set(files[path]["missing_lines"])
        covered += len(lines & executed)
        total += len(lines & executable)
    return {
        "covered": covered,
        "executable": total,
        "percent": 100 * covered / total if total else None,
        "unmeasured_files": missing,
    }


def generate(report, output, revision, history, changed=None):
    percent = float(report["totals"]["percent_covered"])
    if not 0 <= percent <= 100:
        raise ValueError("Invalid coverage percentage")
    output.mkdir(parents=True, exist_ok=True)
    snapshot = {"label": "Current run", "revision": revision, "percent": percent}
    (output / "snapshot.json").write_text(json.dumps(snapshot, indent=2) + "\n")
    badge = f'<svg xmlns="http://www.w3.org/2000/svg" width="180" height="24" role="img" aria-label="Python coverage {percent:.2f}%"><rect width="180" height="24" fill="#285e61"/><text x="90" y="16" text-anchor="middle" fill="white" font-family="sans-serif" font-size="12">Python coverage {percent:.2f}%</text></svg>'
    (output / "badge.svg").write_text(badge + "\n")
    rows = history + [snapshot]
    esc = lambda value: html.escape(str(value))
    table = "".join(
        f"<tr><td>{esc(r['label'])}</td><td>{esc(r['revision'])}</td><td>{float(r['percent']):.2f}%</td></tr>"
        for r in rows
    )
    file_rows = "".join(
        f"<tr><td>{esc(path)}</td><td>{float(data['summary']['percent_covered']):.2f}%</td></tr>"
        for path, data in sorted(report["files"].items())
    )
    change = diff_summary(report["files"], changed) if changed is not None else None
    (output / "changed-lines.json").write_text(json.dumps(change, indent=2) + "\n")
    text = "Changed-line coverage unavailable (no comparison ref)."
    if change is not None:
        pct = f"{change['percent']:.2f}%" if change["percent"] is not None else "N/A"
        text = f"Changed executable Python lines: {change['covered']}/{change['executable']} ({pct}); unmeasured files: {len(change['unmeasured_files'])}."
    summary = f"### Coverage dashboard\n\nRevision: `{revision}`\n\nCombined coverage: **{percent:.2f}%**\n\n{text}\n\nDownload coverage-dashboard/index.html and badge.svg from this run artifact. Changed-line coverage is checked against the configured floor when a comparison ref is available.\n"
    (output / "summary.md").write_text(summary)
    (output / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Coverage evidence</title><style>body{font:16px system-ui;max-width:1000px;margin:3rem auto}td,th{padding:.5rem;text-align:left;border-bottom:1px solid #ddd}code{overflow-wrap:anywhere}</style><h1>Python coverage evidence</h1><img src="badge.svg" alt="Current coverage"><p>'
        + esc(text)
        + "</p><p>Compare only matching Python versions, source roots and test selections. Supplied snapshots are observations, not a complete run history. Missing measurements are not zero.</p><h2>Recorded snapshots</h2><table><tr><th>Label</th><th>Revision</th><th>Combined</th></tr>"
        + table
        + "</table><h2>Current files</h2><table><tr><th>File</th><th>Combined</th></tr>"
        + file_rows
        + "</table>"
    )
    return change


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path("coverage.json"))
    parser.add_argument("--output", type=Path, default=Path("coverage-dashboard"))
    parser.add_argument("--revision", required=True)
    parser.add_argument("--history", type=Path, action="append", default=[])
    parser.add_argument("--base-ref")
    parser.add_argument("--min-diff-coverage", type=float, default=0)
    args = parser.parse_args()
    changed = None
    if args.base_ref:
        # Resolve first; never allow a ref value to become a git option.
        base = subprocess.check_output(
            [
                "git",
                "rev-parse",
                "--verify",
                "--end-of-options",
                args.base_ref + "^{commit}",
            ],
            text=True,
        ).strip()
        changed = git_changed_lines(base)
    change = generate(
        json.loads(args.report.read_text()),
        args.output,
        args.revision,
        [json.loads(path.read_text()) for path in args.history],
        changed,
    )

    if args.min_diff_coverage:
        if change is None or change["unmeasured_files"]:
            raise SystemExit("Changed-line gate requires complete diff measurements")
        if change["percent"] is not None and change["percent"] < args.min_diff_coverage:
            raise SystemExit(f"Changed-line coverage below {args.min_diff_coverage:g}%")


if __name__ == "__main__":
    main()
