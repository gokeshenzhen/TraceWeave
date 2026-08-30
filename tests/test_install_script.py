from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = ROOT / "scripts" / "install.sh"
VERIFY_SCRIPT = ROOT / "scripts" / "verify_fsdb.sh"


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _fake_install_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "Trace Weave"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    copied = scripts / INSTALL_SCRIPT.name
    shutil.copy2(INSTALL_SCRIPT, copied)

    record = repo / "calls.log"
    _write_executable(
        scripts / "setup_source_graph.sh",
        """#!/usr/bin/env bash
printf 'source_graph:%s\n' "${1:-install}" >> "$TRACEWEAVE_TEST_RECORD"
printf 'source graph child output\n'
""",
    )
    _write_executable(
        scripts / "setup_fsdb.sh",
        """#!/usr/bin/env bash
printf 'fsdb_setup:%s\n' "${VERDI_HOME:-unset}" >> "$TRACEWEAVE_TEST_RECORD"
printf 'fsdb setup child output\n'
""",
    )
    _write_executable(
        scripts / "verify_fsdb.sh",
        """#!/usr/bin/env bash
printf 'fsdb_verify\n' >> "$TRACEWEAVE_TEST_RECORD"
printf 'fsdb verify child output\n'
""",
    )

    (repo / "config.py").write_text(
        "from pathlib import Path\nREPO_ROOT = Path(__file__).resolve().parent\n",
        encoding="utf-8",
    )
    (repo / "server.py").write_text(
        """class _Options:
    server_name = "traceweave"
    server_version = "test"

class _App:
    @staticmethod
    def create_initialization_options():
        return _Options()

app = _App()
""",
        encoding="utf-8",
    )
    python = repo / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(Path(sys.executable).resolve())
    return repo, copied, record


def _install_env(record: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["TRACEWEAVE_TEST_RECORD"] = os.fspath(record)
    return env


def test_install_script_has_valid_bash_syntax():
    completed = subprocess.run(
        ["bash", "-n", str(INSTALL_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_check_mode_is_read_only_and_json_keeps_logs_off_stdout(tmp_path):
    repo, script, record = _fake_install_repo(tmp_path)

    completed = subprocess.run(
        ["bash", str(script), "--check", "--json"],
        cwd=repo,
        env=_install_env(record),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(completed.stdout)
    assert receipt["profile"] == "repository_full_eda"
    assert receipt["mode"] == "check"
    assert receipt["ready"] is True
    assert receipt["runtime"] == {
        "command": str(repo / ".venv" / "bin" / "python"),
        "args": [str(repo / "server.py")],
        "cwd": str(repo),
    }
    assert record.read_text(encoding="utf-8").splitlines() == [
        "source_graph:--check",
        "fsdb_verify",
    ]
    assert "child output" not in completed.stdout
    assert "child output" in completed.stderr


def test_default_mode_runs_existing_setup_scripts_in_order(tmp_path):
    repo, script, record = _fake_install_repo(tmp_path)
    verdi_home = tmp_path / "site verdi"

    completed = subprocess.run(
        ["bash", str(script), "--verdi-home", str(verdi_home)],
        cwd=repo,
        env=_install_env(record),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert record.read_text(encoding="utf-8").splitlines() == [
        "source_graph:install",
        f"fsdb_setup:{verdi_home}",
        "fsdb_verify",
    ]
    assert "repository-local full EDA profile is ready" in completed.stdout
    assert f"{repo / '.venv/bin/python'} {repo / 'server.py'}" in completed.stdout


def test_print_config_implies_check_and_never_writes_client_files(tmp_path):
    repo, script, record = _fake_install_repo(tmp_path)

    completed = subprocess.run(
        ["bash", str(script), "--print-config", "all"],
        cwd=repo,
        env=_install_env(record),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert record.read_text(encoding="utf-8").splitlines() == [
        "source_graph:--check",
        "fsdb_verify",
    ]
    assert "[mcp_servers.TraceWeave]" in completed.stdout
    assert '"mcpServers"' in completed.stdout
    assert '"servers"' in completed.stdout
    assert not (repo / ".mcp.json").exists()
    assert not (repo / ".vscode").exists()


def test_json_print_config_contains_machine_readable_templates(tmp_path):
    repo, script, record = _fake_install_repo(tmp_path)

    completed = subprocess.run(
        ["bash", str(script), "--print-config", "all", "--json"],
        cwd=repo,
        env=_install_env(record),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(completed.stdout)
    assert set(receipt["client_configs"]) == {
        "codex",
        "claude",
        "copilot_cli",
        "vscode_copilot",
    }
    assert receipt["client_configs"]["codex"]["command"] == str(
        repo / ".venv/bin/python"
    )


def test_json_failure_receipt_names_the_failed_step(tmp_path):
    repo, script, record = _fake_install_repo(tmp_path)
    _write_executable(
        repo / "scripts" / "setup_source_graph.sh",
        """#!/usr/bin/env bash
printf 'source_graph_failed\n' >&2
exit 2
""",
    )

    completed = subprocess.run(
        ["bash", str(script), "--json"],
        cwd=repo,
        env=_install_env(record),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    receipt = json.loads(completed.stdout)
    assert receipt["ready"] is False
    assert receipt["failed_step"] == "source_graph_setup"
    assert receipt["exit_code"] == 2
    assert "source_graph_failed" in completed.stderr


def test_verify_fsdb_parser_failure_is_fatal(tmp_path):
    repo = tmp_path / "TraceWeave"
    scripts = repo / "scripts"
    runtime = repo / "third_party" / "verdi_runtime" / "linux64"
    scripts.mkdir(parents=True)
    runtime.mkdir(parents=True)
    copied = scripts / VERIFY_SCRIPT.name
    shutil.copy2(VERIFY_SCRIPT, copied)
    for name in ("libnsys.so", "libnffr.so"):
        (runtime / name).touch()
    (repo / "libfsdb_wrapper.so").touch()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    _write_executable(
        fake_python,
        """#!/usr/bin/env bash
if [ "${1:-}" = "--version" ]; then
    printf 'Python 3.11.0\n'
    exit 0
fi
if [ "$#" -eq 3 ]; then
    printf 'simulated parser import failure\n' >&2
    exit 9
fi
case " $* " in
  *" libfsdb_wrapper.so "*) exit 0 ;;
  *" verdi_runtime/linux64 "*) exit 0 ;;
esac
printf 'simulated parser import failure\n' >&2
exit 9
""",
    )
    _write_executable(fake_bin / "ldd", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        fake_bin / "nm",
        "#!/usr/bin/env bash\nprintf '00000000 T fsdb_open\\n'\n",
    )
    env = dict(os.environ)
    env["PYTHON"] = os.fspath(fake_python)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    completed = subprocess.run(
        ["bash", str(copied)],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "simulated parser import failure" in completed.stderr
    assert "All checks passed" not in completed.stdout
