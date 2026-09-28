"""Dependency detection must work even in a portable environment without FST."""
import importlib.metadata

from src import fst_runtime
from src.path_discovery import discover_sim_paths
from traceweave_mcp import doctor


def test_missing_extra_reports_unavailable_without_loading_native(tmp_path, monkeypatch):
    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(fst_runtime.importlib.util, "find_spec", lambda _: None)
    monkeypatch.setattr(fst_runtime.importlib.metadata, "version", missing)
    (tmp_path / "a.fst").write_bytes(b"not opened by discovery")
    result = discover_sim_paths(str(tmp_path))
    assert result["fst_runtime"]["status"] == "dependency_missing"
    assert not result["fst_runtime"]["enabled"]
    assert result["wave_files"][0]["format"] == "fst"
    assert doctor.collect_diagnostics()["fst"]["status"] == "dependency_missing"


def test_wrong_dependency_version_is_not_advertised_ready(monkeypatch):
    monkeypatch.setattr(fst_runtime.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(fst_runtime.importlib.metadata, "version", lambda _: "9.0.0")
    result = fst_runtime.fst_runtime_info()
    assert result["status"] == "version_unsupported" and not result["enabled"]
