from __future__ import annotations

from pathlib import Path

from smart_ai_router import review


def test_run_review_dry_run_outputs_markdown(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "smart_ai_router").mkdir()
    (repo / "smart_ai_router" / "__init__.py").write_text("# package\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (repo / "logs").mkdir()

    out = review.run_review(repo, output_path=repo / "logs" / "review.md", dry_run=True)

    assert out["dry_run"] is True
    assert out["output"].endswith("review.md")
    assert "# Daily Code Review" in out["report"]
    assert "Code quality" in out["report"]
    assert "Actionable follow-up" in out["report"]


def test_install_review_launchd_uses_expected_plist(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "logs").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(review.Path, "home", lambda: home)

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        class Proc:
            returncode = 0
            stdout = ""
            stderr = ""
        return Proc()

    monkeypatch.setattr(review.subprocess, "run", fake_run)

    plist_path = review.install_review_launchd(repo, output_path=repo / "logs" / "review.md", hour=5, minute=45)

    assert plist_path.name == "com.smart-ai-router-review.plist"
    assert plist_path.parent.name == "LaunchAgents"
    text = plist_path.read_bytes()
    assert b"com.smart-ai-router-review" in text
    assert b"<integer>5</integer>" in text
    assert b"<integer>45</integer>" in text
    assert calls[0][:3] == ["launchctl", "unload", str(plist_path)]
    assert calls[1][:3] == ["launchctl", "load", str(plist_path)]


def test_cli_schedule_invokes_install(monkeypatch, capsys):
    calls = []

    def fake_install(repo_root, *, output_path=None, hour=3, minute=0):
        calls.append((str(repo_root), str(output_path), hour, minute))
        return Path("/tmp/review.plist")

    monkeypatch.setattr(review, "install_review_launchd", fake_install)

    rc = review.run_review_cli(["--repo", "/tmp/repo", "--output", "/tmp/review.md", "--schedule", "--hour", "7", "--minute", "15"])

    assert rc == 0
    assert calls == [("/tmp/repo", "/tmp/review.md", 7, 15)]
    assert "Installed daily review LaunchAgent" in capsys.readouterr().out
