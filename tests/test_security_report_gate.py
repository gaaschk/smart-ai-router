from copy import deepcopy

import pytest

from scripts.check_security_report import check, fingerprint


def test_new_code_under_same_rule_cannot_hide_in_baseline():
    known = {
        "filename": "app.py",
        "test_id": "B324",
        "code": "1 sha1(old)",
        "issue_severity": "HIGH",
    }
    baseline = {
        "findings": [
            {"fingerprint": fingerprint(known), "reason": "tracked migration risk"}
        ]
    }
    assert check({"results": [known]}, baseline) == []
    changed = {**known, "code": "1 sha1(new_untrusted_input)"}
    assert check({"results": [changed]}, baseline) == [changed]
    assert fingerprint({**known, "code": "99 sha1(old)"}) == fingerprint(known)


def test_missing_review_reason_is_not_an_exemption():
    finding = {
        "filename": "app.py",
        "test_id": "B1",
        "code": "1 bad()",
        "issue_severity": "MEDIUM",
    }
    assert check(
        {"results": [finding]}, {"findings": [{"fingerprint": fingerprint(finding)}]}
    ) == [finding]


def test_scan_errors_fail_closed():
    with pytest.raises(ValueError):
        check({"errors": ["could not parse"], "results": []}, {"findings": []})
