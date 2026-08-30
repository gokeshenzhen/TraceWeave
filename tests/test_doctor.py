from __future__ import annotations

import json
from pathlib import Path

from traceweave_mcp import doctor


class _RuntimeConfig:
    def __init__(self, root: Path, *, runtime_enabled: bool = True):
        self.__file__ = str(root / "config.py")
        self._runtime_enabled = runtime_enabled

    def get_fsdb_runtime_info(self):
        return {
            "enabled": self._runtime_enabled,
            "source": "verdi_home" if self._runtime_enabled else None,
            "missing_libs": [] if self._runtime_enabled else ["libnffr.so"],
        }


def _versions(name: str) -> str | None:
    return {
        "mcp": "1.27.0",
        "PyYAML": "6.0.3",
        "pyslang": None,
    }[name]


def test_portable_doctor_marks_missing_wrapper_and_recommends_repo(tmp_path, monkeypatch):
    runtime = _RuntimeConfig(tmp_path)
    monkeypatch.setattr(doctor, "_runtime_config", lambda: (runtime, "portable"))
    monkeypatch.setattr(doctor, "_distribution_version", _versions)

    report = doctor.collect_diagnostics()

    assert report["installation_profile"] == "portable"
    assert report["base_runtime"]["ready"] is True
    assert report["source_graph"]["ready"] is False
    assert report["fsdb"] == {
        "ready": False,
        "status": "wrapper_missing",
        "wrapper_present": False,
        "native_runtime_present": True,
        "native_runtime_source": "verdi_home",
        "missing_runtime_libraries": [],
    }
    assert any("bash scripts/install.sh" in action for action in report["recommended_actions"])


def test_portable_doctor_rejects_a_manually_injected_wrapper(tmp_path, monkeypatch):
    (tmp_path / "libfsdb_wrapper.so").touch()
    runtime = _RuntimeConfig(tmp_path)
    monkeypatch.setattr(doctor, "_runtime_config", lambda: (runtime, "portable"))
    monkeypatch.setattr(doctor, "_distribution_version", _versions)

    report = doctor.collect_diagnostics()

    assert report["fsdb"]["status"] == "unsupported_manual_extension"
    assert report["fsdb"]["ready"] is False
    assert any("mixed layout" in action for action in report["recommended_actions"])


def test_doctor_json_output_is_one_parseable_document(tmp_path, monkeypatch, capsys):
    runtime = _RuntimeConfig(tmp_path, runtime_enabled=False)
    monkeypatch.setattr(doctor, "_runtime_config", lambda: (runtime, "repository"))
    monkeypatch.setattr(doctor, "_distribution_version", _versions)

    status = doctor.run_doctor(json_output=True)
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert status == 0
    assert captured.err == ""
    assert report["fsdb"]["status"] == "wrapper_missing"
