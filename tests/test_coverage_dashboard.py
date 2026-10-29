import json
import pytest
from scripts.coverage_dashboard import changed_lines, diff_summary, generate


def test_diff_hunks_include_added_lines_not_deleted_or_context():
    diff = "+++ b/scripts/new.py\n@@ -0,0 +1,2 @@\n+x\n+y\n@@ -8 +10 @@\n-z\n+a\n@@ -12,2 +14,0 @@\n-z\n-z\n"
    assert changed_lines(diff, "scripts/new.py") == {"scripts/new.py": {1, 2, 10}}


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
        {},
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
        if "rev-parse" in args:
            return "abc123\n"
        if "--name-status" in args:
            return b"A\0scripts/a.py\0"
        return "+++ b/scripts/a.py\n@@ -0,0 +1 @@\n+x\n"

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


@pytest.mark.parametrize(
    "name",
    [
        "space name.py",
        "tab\tname.py",
        "line\nname.py",
        'quote"name.py',
        "carriage\rname.py",
        "[glob]*?.py",
        "unicode-é.py",
    ],
)
def test_real_git_special_path_cannot_bypass_gate(monkeypatch, tmp_path, name):
    import subprocess
    import sys
    from scripts import coverage_dashboard as dashboard

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True)

    git("init", "-q")
    (tmp_path / "README").write_text("base\n")
    git("add", ".")
    config = (
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
    )
    git(*config, "commit", "-qm", "base")
    base = git("rev-parse", "HEAD").strip()
    (tmp_path / "scripts").mkdir()
    relative = "scripts/" + name
    (tmp_path / relative).write_text("uncovered = 1\n")
    git("add", ".")
    git(*config, "commit", "-qm", "add uncovered script")
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "totals": {"percent_covered": 80},
                "files": {
                    relative: {
                        "executed_lines": [],
                        "missing_lines": [1],
                        "summary": {"percent_covered": 0},
                    }
                },
            }
        )
    )
    output = tmp_path / "out"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "coverage_dashboard",
            "--report",
            str(report),
            "--output",
            str(output),
            "--revision",
            "test",
            "--base-ref",
            base,
            "--min-diff-coverage",
            "80",
        ],
    )
    with pytest.raises(SystemExit, match="below"):
        dashboard.main()
    assert json.loads((output / "changed-lines.json").read_text())["percent"] == 0
    assert "0/1 (0.00%)" in (output / "summary.md").read_text()


@pytest.mark.parametrize(
    "edited,covered", [(False, False), (True, True), (True, False)]
)
@pytest.mark.parametrize(
    "old,new",
    [
        ("scripts/old.py", "scripts/new.py"),
        ('scripts/old \t\n\r"[x]é.py', 'scripts/new \t\n\r"[x]é.py'),
        ("outside/old.py", "scripts/moved.py"),
    ],
)
def test_real_git_rename_cli_counts_only_edits(tmp_path, old, new, edited, covered):
    import subprocess
    import sys
    from pathlib import Path
    from scripts import coverage_dashboard as dashboard

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True)

    git("init", "-q")
    source = tmp_path / old
    source.parent.mkdir(parents=True)
    lines = [f"value_{i} = {i}\n" for i in range(20)]
    source.write_text("".join(lines))
    git("add", ".")
    config = (
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
    )
    git(*config, "commit", "-qm", "base")
    base = git("rev-parse", "HEAD").strip()
    destination = tmp_path / new
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)
    if edited:
        lines[9] = "value_9 = 999\n"
        destination.write_text("".join(lines))
    git("add", "-A")
    git(*config, "commit", "-qm", "rename")
    status = git("diff", "--name-status", "--find-renames", base, "HEAD")
    assert status.startswith("R")
    report = {
        "totals": {"percent_covered": 80},
        "files": {
            new: {
                "executed_lines": [10] if covered else [],
                "missing_lines": [n for n in range(1, 21) if not covered or n != 10],
                "summary": {"percent_covered": 5 if covered else 0},
            }
        },
    }
    (tmp_path / "coverage.json").write_text(json.dumps(report))
    result = subprocess.run(
        [
            sys.executable,
            str(Path(dashboard.__file__).resolve()),
            "--report",
            "coverage.json",
            "--output",
            "out",
            "--revision",
            "test",
            "--base-ref",
            base,
            "--min-diff-coverage",
            "80",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == (1 if edited and not covered else 0), result.stderr
    summary = json.loads((tmp_path / "out/changed-lines.json").read_text())
    assert summary["executable"] == (1 if edited else 0)
    assert summary["covered"] == (1 if edited and covered else 0)
    assert summary["percent"] == ((100 if covered else 0) if edited else None)
