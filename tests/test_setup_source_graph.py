from __future__ import annotations

from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "setup_source_graph.sh"
REQUIREMENTS = ROOT / "requirements-source-graph.txt"


def _isolated_script(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "TraceWeave"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    copied = scripts / SCRIPT.name
    shutil.copy2(SCRIPT, copied)
    return repo, copied


def test_source_graph_requirements_pin_the_frontend_and_server_runtime():
    entries = {
        line.strip()
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert entries == {"mcp==1.27.0", "PyYAML", "pyslang==11.0.0"}


def test_setup_source_graph_script_has_valid_bash_syntax():
    completed = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_check_mode_is_read_only_when_environment_is_missing(tmp_path):
    repo, script = _isolated_script(tmp_path)

    completed = subprocess.run(
        ["bash", str(script), "--check"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "environment not found" in completed.stderr
    assert not (repo / ".venv").exists()


def test_check_mode_accepts_a_ready_environment_without_installing(tmp_path):
    repo, script = _isolated_script(tmp_path)
    fake_python = repo / ".venv" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text(
        """#!/usr/bin/env bash
if [ "${1:-}" != "-c" ]; then
    exit 9
fi
printf '%s\\n' \\
    'Python 3.11.9' \\
    'mcp 1.27.0' \\
    'PyYAML 6.0.2' \\
    'pyslang 11.0.0' \\
    'pyslang driver Driver'
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    completed = subprocess.run(
        ["bash", str(script), "--check"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "pyslang 11.0.0" in completed.stdout
    assert "environment is ready" in completed.stdout
    assert "installing pinned" not in completed.stdout
