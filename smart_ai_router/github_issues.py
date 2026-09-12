"""Mirroring a user's report into the repo's issue tracker.

A report is only useful if it reaches whoever can fix it, and for this project
that is the GitHub issue list — not a table on an admin page nobody opens. So a
filed report becomes an issue, and the operator has to say so first: the feature
is off until a repo and a token are set on the Settings page.

Two things stay deliberately conservative, because an issue is public and a
report is somebody's chat:

**The transcript is not published unless the operator opts in.** By default the
issue carries what the user *wrote* plus the routing decision, and cites the
local report id for the conversation. The chat stays on the machine that has it.

**The reporter is never named.** Reports come from anonymous visitors and from
users whose handle is their API key's owner; neither belongs in a public issue.
The local report has the attribution.

Failure here is not failure of the report: `create_issue` returns
`(url, error)` and the caller records whichever it got. A revoked token costs
the operator the issue, never the user's words.
"""
from __future__ import annotations

import json

import httpx

from smart_ai_router import settings as _settings

_TITLE_CHARS = 72
_API = "https://api.github.com/repos/{repo}/issues"
_TIMEOUT = 10.0  # a user is waiting on the POST that triggered this


def enabled() -> bool:
    """True when the operator has turned mirroring on *and* given it what it needs."""
    return bool(
        _settings.get_bool("github_issues_enabled")
        and _settings.get_str("github_repo").strip()
        and _settings.get_str("github_token").strip()
    )


def _title(description: str) -> str:
    first = description.strip().splitlines()[0] if description.strip() else "feedback"
    if len(first) > _TITLE_CHARS:
        first = first[: _TITLE_CHARS - 1].rstrip() + "…"
    return f"[report] {first}"


def _body(report_id: int, description: str, meta: dict, transcript: list) -> str:
    lines = [description.strip(), ""]

    facts = [(k, meta.get(k)) for k in ("routed", "profile", "classifier", "why")]
    facts = [(k, v) for k, v in facts if v]
    if facts:
        lines += ["| | |", "|---|---|"]
        # A pipe in a value would end its table cell early.
        lines += [f"| {k} | {str(v).replace('|', ' ')} |" for k, v in facts]
        lines.append("")

    reported = str(meta.get("reported_text") or "").strip()
    if reported:
        lines += ["**The reply being reported**", ""]
        lines += [f"> {ln}" for ln in reported.splitlines()] + [""]

    if transcript:
        lines += [
            "<details><summary>Conversation "
            f"({len(transcript)} messages)</summary>",
            "",
            "```json",
            json.dumps(transcript, indent=1)[:60_000],
            "```",
            "</details>",
            "",
        ]
    else:
        lines.append(
            f"_Conversation withheld; see local report #{report_id} on the router's "
            "Reports page._"
        )

    lines.append("")
    lines.append(f"<sub>Filed from the router UI · local report #{report_id}</sub>")
    return "\n".join(lines)


def create_issue(
    report_id: int, description: str, meta: dict, transcript: list
) -> tuple[str, str]:
    """Open an issue for a report. Returns `(issue_url, error)` — one of them empty.

    Raises nothing: every outcome the caller can't fix (no token, 404 repo, GitHub
    down) comes back as a message to store next to the report.
    """
    if not enabled():
        return "", ""

    repo = _settings.get_str("github_repo").strip().strip("/")
    with_transcript = _settings.get_bool("github_include_transcript")
    payload = {
        "title": _title(description),
        "body": _body(
            report_id, description, meta, transcript if with_transcript else []
        ),
    }
    # ponytail: no labels. An unknown label 422s the whole create, and the
    # operator's repo is not ours to assume labels in — add one if theirs exists.
    try:
        resp = httpx.post(
            _API.format(repo=repo),
            json=payload,
            timeout=_TIMEOUT,
            headers={
                "Authorization": f"Bearer {_settings.get_str('github_token').strip()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    except httpx.HTTPError as exc:
        return "", f"GitHub unreachable: {exc}"

    if resp.status_code >= 300:
        detail = resp.text[:200].replace("\n", " ")
        return "", f"GitHub said {resp.status_code}: {detail}"
    return str(resp.json().get("html_url") or ""), ""
