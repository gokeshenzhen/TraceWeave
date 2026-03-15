"""
test_path_discovery.py
覆盖：root/case 语义识别、case 隔离、compile log 回退、.mcp.yaml 覆盖和提示信息。
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import config
from src.path_discovery import discover_sim_paths


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _touch_with_age(path: Path, seconds_ago: int):
    ts = time.time() - seconds_ago
    os.utime(path, (ts, ts))


class TestRootAndCaseDiscovery:
    def test_root_dir_without_case_name_lists_available_cases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            work_root = Path(tmpdir) / "work"
            _write(work_root / "compile.log", "vlogan\n")
            _write(work_root / "case0" / "irun.log", "Chronologic VCS\n")
            _write(work_root / "case0" / "top_tb.fsdb", "1" * 2048)
            _write(work_root / "my_test" / "sim.log", "run ok\n")

            result = discover_sim_paths(str(work_root))

            assert result["discovery_mode"] == "root_dir"
            assert result["case_dir"] is None
            assert result["sim_logs"] == []
            assert result["wave_files"] == []
            assert result["compile_logs"][0]["path"] == str((work_root / "compile.log").resolve())
            assert result["available_cases"] == [
                {
                    "name": "case0",
                    "dir": str((work_root / "case0").resolve()),
                    "has_sim_log": True,
                    "has_wave": True,
                },
                {
                    "name": "my_test",
                    "dir": str((work_root / "my_test").resolve()),
                    "has_sim_log": True,
                    "has_wave": False,
                },
            ]

    def test_root_dir_case_selection_isolates_sim_and_wave(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            work_root = Path(tmpdir) / "sim"
            root_compile = work_root / "elab.log"
            case0_log = work_root / "case0" / "irun.log"
            case0_wave = work_root / "case0" / "top_tb.fsdb"
            case1_log = work_root / "case1" / "irun.log"
            case1_wave = work_root / "case1" / "top_tb.fsdb"

            _write(root_compile, "xrun\nxmelab\n")
            _write(case0_log, "run ok\n")
            _write(case0_wave, "0" * 4096)
            _write(case1_log, "run ok\n")
            _write(case1_wave, "1" * 4096)
            _touch_with_age(case0_log, 120)
            _touch_with_age(case0_wave, 120)
            _touch_with_age(case1_log, 10)
            _touch_with_age(case1_wave, 10)

            result = discover_sim_paths(str(work_root), "case0")

            assert result["discovery_mode"] == "root_dir"
            assert result["case_dir"] == str((work_root / "case0").resolve())
            assert result["compile_logs"][0]["path"] == str(root_compile.resolve())
            assert [entry["path"] for entry in result["sim_logs"]] == [str(case0_log.resolve())]
            assert [entry["path"] for entry in result["wave_files"]] == [str(case0_wave.resolve())]

    def test_root_dir_falls_back_to_case_local_compile_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            work_root = Path(tmpdir) / "run_outputs"
            case_dir = work_root / "my_test"
            compile_log = case_dir / "compile.log"
            sim_log = case_dir / "sim_run.log"
            wave = case_dir / "dump.fsdb"

            _write(compile_log, "vlogan\n")
            _write(sim_log, "Chronologic VCS\n")
            _write(wave, "2" * 2048)

            result = discover_sim_paths(str(work_root), "my_test")

            assert result["discovery_mode"] == "root_dir"
            assert result["compile_logs"][0]["path"] == str(compile_log.resolve())
            assert result["sim_logs"][0]["path"] == str(sim_log.resolve())
            assert result["wave_files"][0]["path"] == str(wave.resolve())
            assert result["simulator"] == "vcs"

    def test_case_dir_prefers_local_compile_then_parent_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            work_root = Path(tmpdir) / "work"
            case_dir = work_root / "case0"
            parent_compile = work_root / "elab.log"
            sim_log = case_dir / "irun.log"
            wave = case_dir / "top_tb.fsdb"

            _write(parent_compile, "xrun\nxmelab\n")
            _write(sim_log, "run ok\n")
            _write(wave, "3" * 4096)

            result = discover_sim_paths(str(case_dir))

            assert result["discovery_mode"] == "case_dir"
            assert result["case_dir"] == str(case_dir.resolve())
            assert result["compile_logs"][0]["path"] == str(parent_compile.resolve())
            assert result["sim_logs"][0]["path"] == str(sim_log.resolve())
            assert result["wave_files"][0]["path"] == str(wave.resolve())

    def test_case_dir_validates_conflicting_case_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            case_dir = Path(tmpdir) / "case0"
            _write(case_dir / "sim.log", "run ok\n")
            _write(case_dir / "wave.vcd", "$date\n$end\n")

            result = discover_sim_paths(str(case_dir), "case1")

            assert result["discovery_mode"] == "case_dir"
            assert any("does not match current case directory" in hint for hint in result["hints"])


class TestCaseMatching:
    def test_common_prefixes_are_normalized(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            work_root = Path(tmpdir) / "work"
            _write(work_root / "work_case0" / "irun.log", "run ok\n")
            _write(work_root / "work_case0" / "top_tb.fsdb", "4" * 2048)

            result = discover_sim_paths(str(work_root), "case0")

            assert result["case_dir"] == str((work_root / "work_case0").resolve())
            assert result["sim_logs"][0]["path"] == str((work_root / "work_case0" / "irun.log").resolve())

    def test_ambiguous_normalized_case_name_returns_hint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            work_root = Path(tmpdir) / "sim"
            _write(work_root / "case_foo" / "irun.log", "run ok\n")
            _write(work_root / "work_foo" / "irun.log", "run ok\n")

            result = discover_sim_paths(str(work_root), "foo")

            assert result["sim_logs"] == []
            assert result["wave_files"] == []
            assert any("Ambiguous case_name 'foo'" in hint for hint in result["hints"])


class TestConfigOverride:
    def test_mcp_yaml_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            verif_root = Path(tmpdir)
            _write(
                verif_root / ".mcp.yaml",
                "\n".join(
                    [
                        "compile_log: work/elab.log",
                        "case_dir: work/work_{case}",
                        "sim_log: irun.log",
                        "wave_file: top_tb.fsdb",
                    ]
                )
                + "\n",
            )
            elab_log = verif_root / "work" / "elab.log"
            sim_log = verif_root / "work" / "work_case0" / "irun.log"
            wave = verif_root / "work" / "work_case0" / "top_tb.fsdb"

            _write(elab_log, "xrun\nxmelab\n")
            _write(sim_log, "Chronologic VCS\n")
            _write(wave, "2" * 4096)

            result = discover_sim_paths(str(verif_root), "case0")

            assert result["config_source"] == ".mcp.yaml"
            assert result["config_root"] == str(verif_root.resolve())
            assert result["discovery_mode"] == "unknown"
            assert result["case_dir"] == str((verif_root / "work" / "work_case0").resolve())
            assert result["compile_logs"][0]["path"] == str(elab_log.resolve())
            assert result["sim_logs"][0]["path"] == str(sim_log.resolve())
            assert result["wave_files"][0]["path"] == str(wave.resolve())

    def test_case_dir_inherits_parent_mcp_yaml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            verif_root = Path(tmpdir)
            _write(
                verif_root / ".mcp.yaml",
                "\n".join(
                    [
                        "compile_log: work/elab.log",
                        "case_dir: work/{case}",
                        "sim_log: irun.log",
                        "wave_file: top_tb.fsdb",
                    ]
                )
                + "\n",
            )
            case_dir = verif_root / "work" / "case0"
            _write(verif_root / "work" / "elab.log", "xrun\nxmelab\n")
            _write(case_dir / "irun.log", "Chronologic VCS\n")
            _write(case_dir / "top_tb.fsdb", "8" * 4096)

            result = discover_sim_paths(str(case_dir))

            assert result["config_source"] == ".mcp.yaml"
            assert result["config_root"] == str(verif_root.resolve())
            assert result["discovery_mode"] == "case_dir"
            assert result["case_dir"] == str(case_dir.resolve())
            assert result["compile_logs"][0]["path"] == str((verif_root / "work" / "elab.log").resolve())
            assert result["sim_logs"][0]["path"] == str((case_dir / "irun.log").resolve())
            assert result["wave_files"][0]["path"] == str((case_dir / "top_tb.fsdb").resolve())


class TestHints:
    def test_missing_fsdb_runtime_adds_vcd_fallback_hint(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmpdir:
            work_root = Path(tmpdir) / "work"
            case_dir = work_root / "case0"
            compile_log = work_root / "compile.log"
            fsdb_wave = case_dir / "wave.fsdb"
            vcd_wave = case_dir / "wave.vcd"

            _write(compile_log, "vlogan\n")
            _write(fsdb_wave, "1" * 4096)
            _write(vcd_wave, "$date\n$end\n")

            monkeypatch.setattr(config, "LOCAL_FSDB_RUNTIME_DIR", Path(tmpdir) / "missing_runtime")
            monkeypatch.delenv("VERDI_HOME", raising=False)

            result = discover_sim_paths(str(work_root), "case0")

            assert result["fsdb_runtime"]["enabled"] is False
            assert any("prefer VCD waveforms" in hint for hint in result["hints"])

    def test_unknown_directory_returns_actionable_hints(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = discover_sim_paths(tmpdir, "case0")

            assert result["discovery_mode"] == "unknown"
            assert result["compile_logs"] == []
            assert any("does not look like a case directory or a shared simulation root" in hint for hint in result["hints"])

    def test_zero_byte_log_and_small_wave_generate_hints(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            work_root = Path(tmpdir) / "work"
            compile_log = work_root / "compile.log"
            sim_log = work_root / "case0" / "sim.log"
            wave = work_root / "case0" / "wave.fsdb"

            _write(compile_log, "vlogan\n")
            _write(sim_log, "")
            _write(wave, "tiny")
            _touch_with_age(compile_log, 25 * 3600)

            result = discover_sim_paths(str(work_root), "case0")

            assert "Simulation log is empty (0 bytes), simulation may not have completed" in result["hints"]
            assert "Waveform file is very small, simulation may have aborted early" in result["hints"]
            assert "No elaborate-phase log found. build_tb_hierarchy may return partial results" in result["hints"]
            assert any(hint.startswith("File is ") for hint in result["hints"])


class TestLogPhaseDetection:
    def test_elaborate_phase_detection_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            work_root = Path(tmpdir) / "work"
            compile_log = work_root / "elab.log"
            _write(compile_log, "Parsing Design File 'a.sv'\nTop Level Modules:\n")

            result = discover_sim_paths(str(work_root))

            assert result["compile_logs"][0]["phase"] == "elaborate"

    def test_compile_phase_detection_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            work_root = Path(tmpdir) / "work"
            compile_log = work_root / "compile.log"
            _write(compile_log, "XMVLOG\n")

            result = discover_sim_paths(str(work_root))

            assert result["compile_logs"][0]["phase"] == "compile"


class TestMixedLogDetection:
    def test_case_dir_reuses_vcs_mixed_log_as_compile_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            case_dir = Path(tmpdir) / "case0"
            sim_log = case_dir / "vcs.log"
            wave = case_dir / "top_tb.fsdb"

            _write(
                sim_log,
                "\n".join(
                    [
                        "Chronologic VCS",
                        "Parsing design file 'tb/top_tb.sv'",
                        "Top Level Modules:",
                        "  top_tb",
                    ]
                )
                + "\n",
            )
            _write(wave, "5" * 4096)

            result = discover_sim_paths(str(case_dir))

            assert result["compile_logs"] == [
                {
                    "path": str(sim_log.resolve()),
                    "size": sim_log.stat().st_size,
                    "mtime": result["compile_logs"][0]["mtime"],
                    "age_hours": result["compile_logs"][0]["age_hours"],
                    "phase": "elaborate",
                    "is_mixed": True,
                }
            ]
            assert result["sim_logs"][0]["path"] == str(sim_log.resolve())
            assert any("reused from sim_logs" in hint for hint in result["hints"])

    def test_case_dir_reuses_xrun_mixed_log_after_extended_scan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            case_dir = Path(tmpdir) / "case0"
            sim_log = case_dir / "xrun.log"
            wave = case_dir / "top_tb.fsdb"

            long_preamble = [f"+incdir+/tmp/include_{idx}" for idx in range(80)]
            _write(
                sim_log,
                "\n".join(["xrun(64)"] + long_preamble + ["xmvlog worklib.tb:sv", "simulation starts"])
                + "\n",
            )
            _write(wave, "6" * 4096)

            result = discover_sim_paths(str(case_dir))

            assert result["compile_logs"][0]["path"] == str(sim_log.resolve())
            assert result["compile_logs"][0]["phase"] == "compile"
            assert result["compile_logs"][0]["is_mixed"] is True

    def test_case_dir_does_not_reuse_banner_only_sim_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            case_dir = Path(tmpdir) / "case0"
            sim_log = case_dir / "sim.log"
            wave = case_dir / "top_tb.fsdb"

            _write(
                sim_log,
                "\n".join(
                    [
                        "Chronologic VCS",
                        "Simulation begins",
                        "UVM_INFO test.sv(1) @ 0ns: reporter [TAG] hello",
                    ]
                )
                + "\n",
            )
            _write(wave, "7" * 4096)

            result = discover_sim_paths(str(case_dir))

            assert result["compile_logs"] == []
            assert any("No compile/elab log found" in hint for hint in result["hints"])

    def test_root_dir_without_case_name_does_not_scan_case_logs_for_mixed_compile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            work_root = Path(tmpdir) / "work"
            sim_log = work_root / "case0" / "vcs.log"
            wave = work_root / "case0" / "top_tb.fsdb"

            _write(sim_log, "Chronologic VCS\nParsing design file 'tb/top_tb.sv'\n")
            _write(wave, "8" * 4096)

            result = discover_sim_paths(str(work_root))

            assert result["compile_logs"] == []
            assert result["sim_logs"] == []
            assert result["available_cases"][0]["name"] == "case0"

    def test_config_case_dir_reuses_mixed_sim_log_when_compile_log_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            verif_root = Path(tmpdir)
            _write(
                verif_root / ".mcp.yaml",
                "\n".join(
                    [
                        "case_dir: work/{case}",
                        "sim_log: xrun.log",
                        "wave_file: top_tb.fsdb",
                    ]
                )
                + "\n",
            )
            case_dir = verif_root / "work" / "case0"
            sim_log = case_dir / "xrun.log"
            wave = case_dir / "top_tb.fsdb"

            _write(sim_log, "xrun(64)\nxmelab\n")
            _write(wave, "9" * 4096)

            result = discover_sim_paths(str(verif_root), "case0")

            assert result["compile_logs"][0]["path"] == str(sim_log.resolve())
            assert result["compile_logs"][0]["phase"] == "elaborate"
            assert result["compile_logs"][0]["is_mixed"] is True

    def test_existing_compile_log_beats_mixed_sim_log_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            case_dir = Path(tmpdir) / "case0"
            compile_log = case_dir / "compile.log"
            sim_log = case_dir / "xrun.log"
            wave = case_dir / "top_tb.fsdb"

            _write(compile_log, "vlogan\n")
            _write(sim_log, "xrun(64)\nxmelab\n")
            _write(wave, "a" * 4096)

            result = discover_sim_paths(str(case_dir))

            assert result["compile_logs"][0]["path"] == str(compile_log.resolve())
            assert "is_mixed" not in result["compile_logs"][0]
