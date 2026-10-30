"""Fail on unreviewed medium/high Bandit findings; keep known risks explicit."""

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys


def fingerprint(finding):
    code = re.sub(r"(?m)^\d+\s", "", finding["code"]).strip()
    return hashlib.sha256(
        json.dumps([finding["filename"], finding["test_id"], code]).encode()
    ).hexdigest()


def check(report, baseline):
    if report.get("errors"):
        raise ValueError("Bandit could not scan all requested files")
    # A reviewed occurrence grants one match, not an unlimited exemption.
    # Fingerprints still omit line numbers so ordinary source shifts are safe.
    reviewed = Counter(
        item["fingerprint"] for item in baseline["findings"] if item.get("reason")
    )
    new = []
    for item in report["results"]:
        if item["issue_severity"] not in {"MEDIUM", "HIGH"}:
            continue
        key = fingerprint(item)
        if reviewed[key]:
            reviewed[key] -= 1
        else:
            new.append(item)
    return new


def main():
    report = json.loads(Path(sys.argv[1]).read_text())
    baseline = json.loads(Path(sys.argv[2]).read_text())
    new = check(report, baseline)
    for item in new:
        print(
            f"{item['filename']}:{item['line_number']} {item['test_id']}: {item['issue_text']}"
        )
    print(
        f"Unreviewed medium/high findings: {len(new)}; known risks remain in baseline"
    )
    return bool(new)


if __name__ == "__main__":
    sys.exit(main())
