from pathlib import Path
import signal
import sys

from scripts import spike_surelog_comparison as comparison


def test_comparison_dependency_is_pinned_but_rejected_as_runtime_distribution():
    requirements = comparison.REQUIREMENTS.read_text(encoding="utf-8")

    assert comparison.WHEEL in requirements
    assert comparison.WHEEL_SHA256 in requirements
    assert "not a TraceWeave runtime" in requirements

    receipt = comparison._dependency_receipt(Path("/tmp/surelog"))
    assert receipt["package"] == "sc-surelog"
    assert receipt["package_version"] == "1.84.1"
    assert receipt["binary_reported_version"] == "1.84"
    assert receipt["runtime_dependency"] is False
    assert receipt["production_distribution_accepted"] is False
    assert receipt["wheel"]["sha256"] == comparison.WHEEL_SHA256
    assert "--require-hashes" in receipt["reproduction_commands"][1]


def test_translation_flattens_deduplicated_sources_and_preserves_compile_inputs(
    tmp_path,
):
    uvm = tmp_path / "uvm_pkg.sv"
    top = tmp_path / "top.sv"
    uvm.write_text("package uvm_pkg; endpackage\n", encoding="utf-8")
    top.write_text("module top; endmodule\n", encoding="utf-8")
    workload = {
        "top": "top",
        "source_facts": {"source_paths": [str(uvm), str(top)]},
        "translation": {
            "frontend_args": [
                "--compat",
                "all",
                "--single-unit",
                "+define+XCELIUM+DUMP_FSDB",
                "-I",
                str(tmp_path),
                "-f",
                str(tmp_path / "original.f"),
                "--timescale",
                "1ns/10ps",
                "--top",
                "top",
            ],
            "unsupported_options": [
                {
                    "option": "-disable_sem2009",
                    "reason": "no exact mapping",
                    "impact": "semantic_compatibility_oracle_required",
                }
            ],
        },
    }

    translated = comparison.translate_workload(workload)

    assert translated["source_paths"] == [str(uvm), str(top)]
    assert translated["source_count"] == 2
    assert translated["defines"] == ["+define+XCELIUM+DUMP_FSDB"]
    assert translated["include_dirs"] == [str(tmp_path)]
    assert "-timescale=1ns/10ps" in translated["base_args_template"]
    top_index = translated["base_args_template"].index("-top")
    assert translated["base_args_template"][top_index : top_index + 2] == [
        "-top",
        "top",
    ]
    assert translated["unsupported_options"][0]["option"] == "-disable_sem2009"
    assert "<run_dir>/inputs.f" in translated["phase_invocations"]["elaboration"]


def test_evidence_parser_records_profiles_diagnostics_top_and_frontend_counts():
    log = """[WRN:PP0113] /src/uvm.sv:10:2: Unused macro argument.
[NTE:EL0503] /src/top.sv:26:1: Top level module "work@top".
[NTE:EL0508] Nb Top level modules: 1.
[NTE:EL0509] Max instance depth: 5.
[NTE:EL0510] Nb instances: 72.
[NTE:EL0511] Nb leaf instances: 14.
[  FATAL] : 0
[ SYNTAX] : 0
[  ERROR] : 0
[WARNING] : 1
[   NOTE] : 4
"""
    stdout = "Preprocessing took 1.170s\nParsing took 3.824s\n"

    evidence = comparison.parse_surelog_evidence(log, stdout)

    assert evidence["diagnostics"]["summary"]["warning"] == 1
    assert evidence["diagnostics"]["items"][0]["code"] == "PP0113"
    assert evidence["frontend_profile"] == {
        "preprocessing_wall_time_ms": 1170.0,
        "parsing_wall_time_ms": 3824.0,
    }
    assert evidence["recovered"]["tops"] == [
        {"name": "work@top", "file": "/src/top.sv", "line": 26, "column": 1}
    ]
    assert evidence["frontend_object_counts"]["counts"]["instances"] == 72


def test_child_measurement_reports_signal_without_shell_or_global_artifacts(tmp_path):
    command = [
        sys.executable,
        "-c",
        "import os, signal; os.kill(os.getpid(), signal.SIGSEGV)",
    ]

    result = comparison._run_child(command, tmp_path, {}, timeout=5.0)

    assert result["return_code"] == -signal.SIGSEGV
    assert result["shell_exit_code"] == 139
    assert result["signal"] == "SIGSEGV"
    assert result["timed_out"] is False
    assert result["wall_time_ms"] > 0
    assert result["cpu_time_ms"] >= 0
    assert result["stdout_bytes"] == 0
    assert (tmp_path / "frontend.stderr").is_file()


def test_oracle_comparison_never_turns_missing_recovery_into_a_match():
    oracle_doc = {
        "workloads": {
            "real_uvm": {
                "annotated_source": {"available": True, "top": "top_tb"},
                "kdb_npi": {"available": False, "blocker": "no KDB"},
            }
        }
    }

    result = comparison._compare_oracles({"tops": []}, oracle_doc)

    assert result["comparisons"] == [
        {
            "oracle": "annotated_source",
            "available": True,
            "expected_top": "top_tb",
            "top_match": None,
            "status": "not_compared_frontend_failure",
        },
        {
            "oracle": "kdb_npi",
            "available": False,
            "expected_top": None,
            "top_match": None,
            "status": "unavailable",
        },
    ]
