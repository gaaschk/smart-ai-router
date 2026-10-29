"""Exercise only argument parsing; no installation or service commands run."""
from pathlib import Path
import subprocess

import pytest

INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"


@pytest.mark.parametrize("args", [
    ["--install-dir"], ["--install-dir", ""],
    ["--install-dir", "--no-setup"], ["--install-dir", "--help"],
])
def test_invalid_install_dir_stops_before_preflight(args, tmp_path):
    result = subprocess.run(["/bin/bash", str(INSTALLER), *args], cwd=tmp_path,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert "requires a non-empty path" in result.stderr
    assert "unbound variable" not in result.stderr
    assert "Checking dependencies" not in result.stdout
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("path", ["/tmp/example with spaces", "./-example"])
def test_valid_path_can_be_parsed_without_running_installation(path, tmp_path):
    result = subprocess.run(["/bin/bash", str(INSTALLER), "--install-dir", path, "--help"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert "Checking dependencies" not in result.stdout
    assert not list(tmp_path.iterdir())
