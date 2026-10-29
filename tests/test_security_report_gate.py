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


def test_reviewed_occurrence_cannot_exempt_duplicate_at_another_line():
    known = {
        "filename": "app.py",
        "test_id": "B324",
        "code": "1 sha1(old)",
        "issue_severity": "HIGH",
        "line_number": 1,
    }
    shifted = {**known, "code": "99 sha1(old)", "line_number": 99}
    duplicate = {**known, "code": "150 sha1(old)", "line_number": 150}
    baseline = {
        "findings": [{"fingerprint": fingerprint(known), "reason": "reviewed once"}]
    }
    assert check({"results": [shifted]}, baseline) == []
    assert check({"results": [shifted, duplicate]}, baseline) == [duplicate]
    # Each separately reviewed occurrence permits exactly one match.
    baseline["findings"].append(dict(baseline["findings"][0]))
    assert check({"results": [shifted, duplicate]}, baseline) == []
    assert check({"results": [shifted, duplicate, known]}, baseline) == [known]
