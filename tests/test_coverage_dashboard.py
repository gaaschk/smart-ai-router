import json
import pytest
from scripts.coverage_dashboard import changed_lines, diff_summary, generate


def test_diff_hunks_include_added_lines_not_deleted_or_context():
    diff = "+++ b/scripts/new.py\n@@ -0,0 +1,2 @@\n+x\n+y\n@@ -8 +10 @@\n-z\n+a\n@@ -12,2 +14,0 @@\n-z\n-z\n"
    assert changed_lines(diff) == {"scripts/new.py": {1, 2, 10}}


def test_missing_measurement_is_explicit_and_nonexecutable_lines_excluded():
    files = {"scripts/a.py": {"executed_lines": [1], "missing_lines": [3]}}
    result = diff_summary(
        files,
        {"scripts/a.py": {1, 2, 3}, "scripts/unknown.py": {1}, "tests/test.py": {1}},
    )
    assert result == {
        "covered": 1,
        "executable": 2,
        "percent": 50,
        "unmeasured_files": ["scripts/unknown.py"],
    }
    assert diff_summary(files, {})["percent"] is None


def test_dashboard_escapes_labels_and_writes_machine_readable_evidence(tmp_path):
    report = {
        "totals": {"percent_covered": 81.25},
        "files": {
            "scripts/<script>.py": {
                "executed_lines": [1],
                "missing_lines": [],
                "summary": {"percent_covered": 100},
            }
        },
    }
    generate(
        report,
        tmp_path,
        "abc",
        [{"label": "<script>bad</script>", "revision": "old", "percent": 80}],
        "",
    )
    page = (tmp_path / "index.html").read_text()
    assert "<script>" not in page
    assert "&lt;script&gt;" in page
    assert "81.25%" in (tmp_path / "badge.svg").read_text()
    assert json.loads((tmp_path / "snapshot.json").read_text())["revision"] == "abc"
    assert json.loads((tmp_path / "changed-lines.json").read_text())["percent"] is None


def test_invalid_total_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        generate({"totals": {"percent_covered": 101}}, tmp_path, "abc", [])


@pytest.mark.parametrize(
    "covered,minimum,passes", [(True, 80, True), (False, 80, False)]
)
def test_cli_changed_line_gate_uses_resolved_ref_and_actual_report(
    monkeypatch, tmp_path, covered, minimum, passes
):
    import sys
    from scripts import coverage_dashboard as dashboard

    path = tmp_path / "coverage.json"
    path.write_text(
        json.dumps(
            {
                "totals": {"percent_covered": 80},
                "files": {
                    "scripts/a.py": {
                        "executed_lines": [1] if covered else [],
                        "missing_lines": [] if covered else [1],
                        "summary": {"percent_covered": 100 if covered else 0},
                    }
                },
            }
        )
    )
    calls = []

    def git(args, **kw):
        calls.append(args)
        return (
            "abc123\n"
            if "rev-parse" in args
            else "+++ b/scripts/a.py\n@@ -0,0 +1 @@\n+x\n"
        )

    monkeypatch.setattr(dashboard.subprocess, "check_output", git)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "coverage_dashboard",
            "--report",
            str(path),
            "--output",
            str(tmp_path / "out"),
            "--revision",
            "test",
            "--base-ref",
            "base",
            "--min-diff-coverage",
            str(minimum),
        ],
    )
    if passes:
        dashboard.main()
    else:
        with pytest.raises(SystemExit, match="below"):
            dashboard.main()
    assert "--end-of-options" in calls[0]
    assert "abc123" in calls[1]
