import hashlib
import json
from copy import deepcopy
from pathlib import Path
import shlex
import subprocess
import sys

from scripts import spike_source_frontend as spike


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "spike_source_frontend.py"
HAND_SOURCE = (
    ROOT / "tests" / "fixtures" / "source_graph_frontend" / "hand_connectivity.sv"
)
HAND_ORACLE = ROOT / "tests" / "fixtures" / "source_graph_frontend" / "hand_oracle.json"
REQUIREMENTS = ROOT / "scripts" / "frontend_spike_requirements_cp311_linux_x86_64.txt"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_dependency_receipt_pins_the_isolated_cp311_wheel_by_hash():
    requirements = REQUIREMENTS.read_text(encoding="utf-8")

    assert "pyslang @ https://files.pythonhosted.org/" in requirements
    assert spike.FRONTEND_WHEEL in requirements
    assert f"sha256:{spike.FRONTEND_WHEEL_SHA256}" in requirements
    assert "not a TraceWeave runtime dependency" in requirements

    receipt = spike._dependency_receipt(Path("/tmp/probe-venv/bin/python"))
    assert receipt["version"] == "11.0.0"
    assert receipt["wheel"]["bytes"] == 6_530_751
    assert receipt["wheel"]["url"].endswith(spike.FRONTEND_WHEEL)
    assert receipt["system_python_modified"] is False
    assert "--only-binary=:all:" in receipt["reproduction_commands"][1]
    assert "--require-hashes" in receipt["reproduction_commands"][1]
    assert receipt["platform_policy"]["validated_target"].startswith("CPython 3.11")


def test_frontend_platform_policy_never_silently_builds_an_unvalidated_wheel():
    supported = spike._frontend_platform_policy(
        system="Linux",
        machine="x86_64",
        implementation="CPython",
        python_major=3,
        python_minor=11,
    )
    unsupported = spike._frontend_platform_policy(
        system="Darwin",
        machine="arm64",
        implementation="CPython",
        python_major=3,
        python_minor=12,
    )

    assert supported["status"] == "validated_binary_wheel_platform"
    assert unsupported["status"] == "optional_extra_dependency_blocker"
    assert unsupported["source_build_evaluated"] is False
    assert "do not infer frontend incompatibility" in unsupported[
        "unsupported_platform_policy"
    ]


def test_hand_fixture_and_oracle_cover_required_frontend_features():
    oracle = json.loads(HAND_ORACLE.read_text(encoding="utf-8"))
    source = HAND_SOURCE.read_text(encoding="utf-8")

    assert set(oracle["required_features"]) == {
        "named_ports",
        "positional_ports",
        "slice",
        "concat",
        "generate",
        "always_comb",
        "always_ff",
        "interface",
        "modport",
    }
    assert "interface sg_bus_if" in source
    assert "modport producer" in source
    assert "always_ff" in source
    assert "always_comb" in source
    assert "begin : gen_lanes" in source
    assert "sg_bridge u_bridge (clk, rst_n, bus, lane_data);" in source
    assert ".data_i(lane_i)" in source
    assert "{comb_y[3:0], seq_q[7:4]}" in source

    workload = spike.build_hand_workload()
    assert workload["top"] == "sg_top"
    assert workload["source_facts"]["existing_source_count"] == 1
    assert workload["manual_oracle"] == oracle


def test_vcs_translation_is_explicit_about_supported_and_excluded_options():
    source = HAND_SOURCE.resolve()
    translation = spike.translate_vcs_invocation(
        " ".join(
            [
                "vcs",
                "+define+VCS+define+UVM_OBJECT_MUST_HAVE_CONSTRUCTOR",
                "+incdir+/vendor/include",
                "+libext+.v+.sv",
                "-sverilog",
                "-timescale=1ns/1ps",
                "-kdb",
                "-debug_access+all",
                "-CFLAGS",
                "-DVCS",
                "/vendor/uvm_dpi.cc",
                shlex.quote(str(source)),
                "-top",
                "sg_top",
            ]
        ),
        top="sg_top",
        fallback_sources=[source],
    )

    args = translation["frontend_args"]
    assert args[:5] == [
        "--compat",
        "vcs",
        "--enable-legacy-protect",
        "--single-unit",
        "-Wno-unknown-sys-name",
    ]
    assert "+define+VCS+UVM_OBJECT_MUST_HAVE_CONSTRUCTOR" in args
    assert "+define+VCS+define+UVM_OBJECT_MUST_HAVE_CONSTRUCTOR" not in args
    assert ["--top", "sg_top"] == args[-2:]

    unsupported = {
        item["option"]: item["impact"] for item in translation["unsupported_options"]
    }
    assert unsupported["-kdb"] == "frontend_irrelevant"
    assert unsupported["-debug_access+all"] == "frontend_irrelevant"
    assert unsupported["-CFLAGS -DVCS"] == "frontend_irrelevant"
    assert unsupported["/vendor/uvm_dpi.cc"] == "external_runtime_not_modeled"


def test_xcelium_translation_maps_compile_inputs_and_receipts_vendor_options():
    source = HAND_SOURCE.resolve()
    translation = spike.translate_xcelium_invocation(
        " ".join(
            [
                "xrun",
                "-elaborate",
                "-sv",
                "-64bit",
                "+define+XCELIUM+DUMP_FSDB",
                "-incdir",
                "/vendor/uvm/src",
                "-timescale",
                "1ns/10ps",
                "-access",
                "+rc",
                "-disable_sem2009",
                shlex.quote(str(source)),
                "-top",
                "sg_top",
                "-log",
                "/tmp/elab.log",
            ]
        ),
        top="sg_top",
        fallback_sources=[source],
    )

    args = translation["frontend_args"]
    assert args[:5] == [
        "--compat",
        "all",
        "--enable-legacy-protect",
        "--single-unit",
        "-Wno-unknown-sys-name",
    ]
    assert ["-I", "/vendor/uvm/src"] == args[args.index("-I") : args.index("-I") + 2]
    assert ["--timescale", "1ns/10ps"] == args[
        args.index("--timescale") : args.index("--timescale") + 2
    ]
    assert ["--top", "sg_top"] == args[-2:]

    unsupported = {
        item["option"]: item["impact"] for item in translation["unsupported_options"]
    }
    assert unsupported["-elaborate"] == "frontend_irrelevant"
    assert unsupported["-64bit"] == "frontend_irrelevant"
    assert unsupported["-access +rc"] == "frontend_irrelevant"
    assert unsupported["-disable_sem2009"] == ("semantic_compatibility_oracle_required")
    assert unsupported["-log /tmp/elab.log"] == "frontend_irrelevant"


def test_xcelium_translation_expands_uvmhome_and_classifies_opentitan_options(
    tmp_path,
):
    uvm_src = tmp_path / "CDNS-1.2" / "sv" / "src"
    additions = tmp_path / "CDNS-1.2" / "additions" / "sv"
    uvm_pkg = uvm_src / "uvm_pkg.sv"
    cdns_pkg = additions / "cdns_uvm_pkg.sv"
    _write(uvm_pkg, "package uvm_pkg; endpackage\n")
    _write(cdns_pkg, "package cdns_uvm_pkg; endpackage\n")
    library = {
        "include_dirs": [str(uvm_src), str(additions)],
        "sources": [str(uvm_pkg), str(cdns_pkg)],
    }
    translation = spike.translate_xcelium_invocation(
        (
            "xrun -L/tmp/dpi -licqueue -ALLOWREDEFINITION -errormax 50 "
            "-f /tmp/design.scr "
            "-uvmhome CDNS-1.2 -enable_strict_timescale -nocopyright "
            "-nowarn TSNSPK -xmerror ENUMERR -enable_abv_asrtctrl_enh "
            "-xprop F -xverbose -lelf -lcrypto -lutil -top tb"
        ),
        top="tb",
        fallback_sources=[],
        xcelium_uvm_library=library,
    )

    args = translation["frontend_args"]
    assert args[args.index("-I") : args.index("-I") + 6] == [
        "-I",
        str(uvm_src),
        "-I",
        str(additions),
        str(uvm_pkg),
        str(cdns_pkg),
    ]
    assert args.index(str(uvm_pkg)) < args.index("-f")
    unsupported = {
        item["option"]: item["impact"] for item in translation["unsupported_options"]
    }
    assert unsupported["-ALLOWREDEFINITION"] == (
        "semantic_compatibility_oracle_required"
    )
    assert unsupported["-errormax 50"] == "frontend_irrelevant"
    assert unsupported["-nowarn TSNSPK"] == "frontend_irrelevant"
    assert unsupported["-xmerror ENUMERR"] == "frontend_irrelevant"
    assert unsupported["-xprop F"] == "frontend_irrelevant"
    assert unsupported["-L/tmp/dpi"] == "external_runtime_not_modeled"
    assert unsupported["-lcrypto"] == "external_runtime_not_modeled"
    assert "requires_workload_review" not in unsupported.values()


def test_xcelium_real_workload_records_simulator_provided_uvm_sources(tmp_path):
    work = tmp_path / "fusesoc-work"
    compile_log = work / "xrun.log"
    top = work / "src" / "tb.sv"
    uvmhome = tmp_path / "tools" / "methodology" / "UVM" / "CDNS-1.2"
    uvm_pkg = uvmhome / "sv" / "src" / "uvm_pkg.sv"
    cdns_pkg = uvmhome / "additions" / "sv" / "cdns_uvm_pkg.sv"
    _write(top, "module tb; endmodule\n")
    _write(uvm_pkg, "package uvm_pkg; endpackage\n")
    _write(cdns_pkg, "package cdns_uvm_pkg; endpackage\n")
    _write(
        compile_log,
        "xrun(64): 26.03-s001-20260323: started\n"
        "xrun\n"
        "\t-elaborate\n"
        "\t-uvmhome CDNS-1.2\n"
        f"\t{top}\n"
        "\t-top tb\n"
        f"file: {top}\n"
        "\tmodule worklib.tb:sv\n"
        "Compiling UVM packages (uvm_pkg.sv cdns_uvm_pkg.sv) using uvmhome "
        f"location {uvmhome}\n",
    )

    workload = spike.build_real_workload(compile_log, {}, simulator="xcelium")

    library = workload["simulator_source_libraries"][0]
    assert library["source"] == "xrun_log_uvmhome_record"
    assert library["sources"] == [str(uvm_pkg.resolve()), str(cdns_pkg.resolve())]
    assert workload["source_facts"]["existing_source_count"] == 3
    assert str(uvm_pkg.resolve()) in workload["translation"]["frontend_args"]


def test_xcelium_filelist_expansion_excludes_native_dpi_inputs(tmp_path):
    work = tmp_path / "fusesoc-work"
    source = work / "src" / "top.sv"
    native = work / "src" / "model.cc"
    include_dir = work / "src" / "include"
    filelist = work / "design.scr"
    _write(source, "module tb; endmodule\n")
    _write(native, "int dpi_model(void) { return 0; }\n")
    include_dir.mkdir(parents=True)
    _write(
        filelist,
        "+incdir+src/include\n"
        "src/model.cc\n"
        "src/top.sv\n",
    )

    translation = spike.translate_xcelium_invocation(
        "xrun -f design.scr -top tb",
        top="tb",
        fallback_sources=[],
        compile_cwd=work,
        environment={},
    )

    args = translation["frontend_args"]
    assert str(source.resolve()) in args
    assert str(native.resolve()) not in args
    assert f"+incdir+{include_dir.resolve()}" in args
    assert "-f" not in args
    assert translation["filelist_expansions"] == [
        {
            "input": "-f design.scr",
            "status": "expanded_for_frontend",
            "visited_filelists": [str(filelist.resolve())],
            "frontend_argument_count": 2,
            "hdl_input_count": 1,
            "excluded_input_count": 1,
        }
    ]
    excluded = translation["unsupported_options"]
    assert any(
        item["option"] == str(native.resolve())
        and item["impact"] == "external_runtime_not_modeled"
        for item in excluded
    )


def test_real_workload_planning_uses_explicit_environment_and_nested_filelists(
    tmp_path,
):
    project = tmp_path / "uvm_demo"
    compile_log = project / "tb" / "comp.log"
    rtl = project / "des" / "rtl" / "verilog" / "dut.sv"
    top = project / "tb" / "top_tb.sv"
    uvm_source = project / "vendor" / "uvm" / "src" / "uvm.sv"
    dpi_source = project / "vendor" / "uvm" / "src" / "dpi" / "uvm_dpi.cc"
    filelist = project / "dut" / "filelist.f"
    nested = project / "dut" / "nested.f"
    for path, text in (
        (rtl, "module dut; endmodule\n"),
        (top, "module top_tb; dut u_dut(); endmodule\n"),
        (uvm_source, "package uvm_pkg; endpackage\n"),
        (dpi_source, "/* development-only DPI placeholder */\n"),
    ):
        _write(path, text)
    _write(
        filelist,
        "+incdir+$UVM_HOME/src\n$DUT_SRC_DIR/dut.sv\n-f $TB_DIR/dut/nested.f\n",
    )
    _write(nested, "$TB_DIR/tb/top_tb.sv\n")
    _write(
        compile_log,
        "Chronologic VCS simulator\n"
        "Command: vcs +define+VCS+define+UVM_OBJECT_MUST_HAVE_CONSTRUCTOR "
        "-sverilog -kdb -f $TB_DIR/dut/filelist.f "
        f"{uvm_source} {dpi_source} -CFLAGS -DVCS\n"
        f"Parsing design file '{rtl}'\n"
        f"Parsing design file '{top}'\n"
        f"Parsing design file '{uvm_source}'\n"
        "Top Level Modules:\n"
        "       top_tb\n",
    )

    environment = {
        "TB_DIR": str(project),
        "DUT_SRC_DIR": str(rtl.parent),
        "UVM_HOME": str(uvm_source.parent.parent),
    }
    workload = spike.build_real_workload(compile_log, environment)

    assert workload["top"] == "top_tb"
    assert workload["environment"] == environment
    assert set(workload["filelists"]) == {
        str(filelist.resolve()),
        str(nested.resolve()),
    }
    assert workload["source_facts"]["existing_source_count"] == 3
    assert (
        "+define+VCS+UVM_OBJECT_MUST_HAVE_CONSTRUCTOR"
        in workload["translation"]["frontend_args"]
    )


def test_xcelium_real_workload_recovers_bounded_command_and_nested_sources(tmp_path):
    project = tmp_path / "uvm_demo"
    compile_log = project / "tb" / "work" / "elab.log"
    rtl = project / "des" / "rtl" / "verilog" / "dut.sv"
    top = project / "tb" / "top_tb.sv"
    uvm_source = project / "vendor" / "uvm" / "src" / "uvm_pkg.sv"
    filelist = project / "dut" / "filelist.f"
    nested = project / "dut" / "nested.f"
    for path, text in (
        (rtl, "module dut; endmodule\n"),
        (top, "module top_tb; dut u_dut(); endmodule\n"),
        (uvm_source, "package uvm_pkg; endpackage\n"),
    ):
        _write(path, text)
    _write(
        filelist,
        "$UVM_HOME/src/uvm_pkg.sv\n$DUT_SRC_DIR/dut.sv\n"
        "-f $TB_DIR/dut/nested.f\n"
        "// $TB_DIR/tb/not_compiled.sv\n",
    )
    _write(nested, "$TB_DIR/tb/top_tb.sv\n")
    _write(
        compile_log,
        "xrun(64): 26.03-s001-20260323: started\n"
        "xrun\n"
        "\t-elaborate\n"
        "\t-sv\n"
        "\t-disable_sem2009\n"
        f"\t{uvm_source}\n"
        "\t-incdir $UVM_HOME/src\n"
        "\t-f $TB_DIR/dut/filelist.f\n"
        f"\t\t{rtl}\n"
        f"\t\t{top}\n"
        "\t-timescale 1ns/10ps\n"
        "\t-top top_tb\n"
        "\t-log /tmp/elab.log\n"
        f"file: {rtl}\n"
        "\tmodule worklib.dut:sv\n"
        f"file: {top}\n"
        "\tmodule worklib.top_tb:sv\n"
        "DEFINE std ./STD\n"
        "xrun: *W,AFTER: this diagnostic is not part of the invocation\n",
    )

    workload = spike.build_real_workload(compile_log, {}, simulator="auto")

    assert workload["simulator"] == "xcelium"
    assert workload["simulator_version"] == "26.03-s001-20260323"
    assert workload["top"] == "top_tb"
    assert workload["compile_cwd"] == str((project / "tb").resolve())
    assert workload["environment"] == {
        "TB_DIR": str(project.resolve()),
        "UVM_HOME": str(uvm_source.parent.parent.resolve()),
        "DUT_SRC_DIR": str(rtl.parent.resolve()),
    }
    assert set(workload["filelists"]) == {
        str(filelist.resolve()),
        str(nested.resolve()),
    }
    assert workload["source_facts"]["existing_source_count"] == 3
    invocation = workload["translation"]["original_invocation"]
    assert invocation.startswith("xrun -elaborate -sv")
    assert "this diagnostic is not part of the invocation" not in invocation
    assert workload["diagnostic_policy"]["excluded_inputs"][
        "protected_source"
    ].endswith("never a fallback")


def test_manual_oracle_comparison_checks_hierarchy_and_file_line_fidelity():
    oracle = json.loads(HAND_ORACLE.read_text(encoding="utf-8"))
    source = str(HAND_SOURCE.resolve())
    recovered = {
        "tops": ["sg_top"],
        "instances": [{"path": path} for path in oracle["expected_instance_paths"]],
        "definitions": [
            {"name": "sg_bus_if", "file": source, "line": 3},
            {"name": "sg_leaf", "file": source, "line": 12},
        ],
        "procedural_blocks": [
            {"procedure_kind": "AlwaysFF", "file": source, "line": 19},
            {"procedure_kind": "AlwaysComb", "file": source, "line": 26},
        ],
    }

    matched = spike.compare_oracles(recovered, oracle, None)
    assert matched["all_available_oracles_match"] is True

    recovered["procedural_blocks"][1]["line"] = 27
    mismatched = spike.compare_oracles(recovered, oracle, None)
    assert mismatched["all_available_oracles_match"] is False
    assert mismatched["comparisons"][0]["missing_source_locations"] == [
        oracle["expected_source_locations"][3]
    ]


def test_oracle_comparison_uses_exact_membership_beyond_retained_instance_cap():
    oracle = {
        "top": "tb",
        "expected_instance_paths": ["tb", "tb.dut.deep.u_leaf"],
    }
    recovered = {
        "tops": ["tb"],
        "instances": [{"path": "tb"}],
        "instances_truncated": True,
        "oracle_instance_path_membership": {"tb.dut.deep.u_leaf": True},
    }

    comparison = spike.compare_oracles(recovered, oracle, None)

    assert comparison["all_available_oracles_match"] is True
    assert comparison["comparisons"][0]["missing_instance_paths"] == []


def test_oracle_probe_paths_include_supplemental_signal_parent_scopes():
    paths = spike._oracle_probe_instance_paths(
        {"expected_instance_paths": ["tb"]},
        {
            "fsdb": {
                "available": True,
                "signal_paths": ["tb.dut.bus.valid"],
            },
            "npi": {"available": False, "expected_instance_paths": ["ignored"]},
        },
    )

    assert paths == ["tb", "tb.dut.bus"]


def test_plan_only_preserves_venv_launcher_and_never_imports_frontend(tmp_path):
    launcher = tmp_path / "venv" / "bin" / "python"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(sys.executable)
    args = spike.parse_args(
        [
            "--frontend-python",
            str(launcher),
            "--workload",
            "hand_fixture",
            "--plan-only",
        ]
    )

    result = spike.run_spike(args)

    assert result["status"] == "planned"
    assert result["dependency"]["selected_interpreter"] == str(launcher.absolute())
    assert result["workloads"][0]["name"] == "hand_fixture"


def test_missing_frontend_is_a_structured_blocker_without_fallback(tmp_path):
    launcher = tmp_path / "python-without-site-packages"
    launcher.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -S "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--frontend-python",
            str(launcher),
            "--workload",
            "hand_fixture",
            "--cold-repeats",
            "1",
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 3
    assert completed.stderr == ""
    result = json.loads(completed.stdout)
    workload = result["workloads"][0]
    assert result["status"] == "blocked"
    assert workload["status"] == "blocked"
    assert workload["blockers"][0]["code"] == "frontend_unavailable"
    assert "fallback" not in workload["representative_frontend_result"]


def test_plan_output_file_has_machine_readable_receipt(tmp_path):
    output = tmp_path / "nested" / "plan.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--workload",
            "hand_fixture",
            "--plan-only",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(completed.stdout)
    payload = output.read_bytes()
    assert receipt["output"] == str(output.resolve())
    assert receipt["sha256"] == hashlib.sha256(payload).hexdigest()
    assert json.loads(payload)["status"] == "planned"


def test_worker_runs_from_captured_compile_working_directory(
    tmp_path, monkeypatch
):
    compile_cwd = tmp_path / "fusesoc-work"
    compile_cwd.mkdir()
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["cwd"] = kwargs["cwd"]
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout=json.dumps({"status": "supported"}),
            stderr="",
        )

    monkeypatch.setattr(spike.subprocess, "run", fake_run)

    payload, receipt = spike._invoke_worker(
        Path("/tmp/frontend-venv/bin/python"),
        {"compile_cwd": str(compile_cwd)},
        timeout=10,
    )

    assert captured["cwd"] == compile_cwd.resolve()
    assert payload == {"status": "supported"}
    assert receipt["returncode"] == 0


def test_assessment_does_not_request_second_frontend_without_a_key_gap():
    results = [
        {
            "name": name,
            "status": "supported",
            "oracle_comparison": {"all_available_oracles_match": True},
            "cold_process": {"stable_semantics": True},
        }
        for name in spike.WORKLOAD_NAMES
    ]

    assessment = spike._assessment(results)

    assert assessment["slang_primary_frontend"]["decision"] == (
        "recommended_for_phase_1_prototype"
    )
    assert assessment["surelog_uhdm_comparison"] == {
        "performed": False,
        "fallback_needed": False,
        "reason": (
            "Slang recovered all required workload tops, annotated hierarchy paths, "
            "and source locations without a key frontend blocker"
        ),
    }
    assert assessment["dependency_model"]["recommendation"] == "optional_extra"
    assert assessment["cold_frontend_worker_model"]["recommendation"] == (
        "isolated_worker_process"
    )
    assert assessment["cache_fingerprint_contract"]["recommendation"] == (
        "semantic_identity_not_full_diagnostic_payload"
    )
    assert (
        "formatted diagnostic message text"
        in assessment["cache_fingerprint_contract"]["excludes"]
    )


def test_assessment_context_combines_explicit_repository_evidence_without_mutation():
    base = spike._assessment([])
    context = {
        "assessment_overrides": {
            "slang_primary_frontend": {"decision": "conditional_primary"},
            "surelog_uhdm_comparison": {
                "performed": True,
                "fallback_selected": False,
            },
            "open_questions": [],
            "known_limitations": ["bind gap"],
            "phase_1_acceptance_items": ["IR edge fidelity"],
        },
        "evidence_receipt": {"results": ["tracked.json"]},
    }

    combined = spike._apply_assessment_context(base, context)

    assert combined["slang_primary_frontend"]["decision"] == "conditional_primary"
    assert combined["surelog_uhdm_comparison"]["performed"] is True
    assert combined["surelog_uhdm_comparison"]["fallback_selected"] is False
    assert combined["open_questions"] == []
    assert combined["known_limitations"] == ["bind gap"]
    assert combined["phase_1_acceptance_items"] == ["IR edge fidelity"]
    assert combined["repository_evidence_context"] == {
        "results": ["tracked.json"]
    }
    assert base["slang_primary_frontend"]["decision"] != "conditional_primary"


def test_semantic_stability_separates_advisory_diagnostic_count_jitter():
    worker = {
        "status": "blocked",
        "frontend": {"name": "Slang/pyslang", "version": "11.0.0"},
        "diagnostics": {
            "total": 11,
            "blocking_error_count": 1,
            "by_code": {"AdvisoryLint": 10, "BadBinaryExpression": 1},
            "by_effective_severity": {"Error": 1, "Ignored": 10},
            "items": [
                {
                    "code": "BadBinaryExpression",
                    "option": None,
                    "severity": "Error",
                    "message": "specialization T=first",
                    "file": "/vendor/uvm.svh",
                    "line": 714,
                    "column": 15,
                }
            ],
            "items_truncated": False,
            "explicitly_suppressed_unknown_system_count": 0,
        },
        "recovered": {
            "tops": ["top"],
            "instances": [{"path": "top"}],
            "definitions": [],
            "procedural_blocks": [],
            "object_counts": {},
        },
        "blockers": [
            {
                "code": "blocking_frontend_diagnostics",
                "phase": "elaboration",
                "message": "1 effective error remains",
            }
        ],
        "phase_measurements": {},
        "worker_wall_time_ms": 1.0,
        "worker_cpu_time_ms": 1.0,
        "process_rss_kib": {"start": 10, "peak": 20, "end": 20},
    }
    jittered = deepcopy(worker)
    jittered["diagnostics"]["total"] = 13
    jittered["diagnostics"]["by_code"]["AdvisoryLint"] = 12
    jittered["diagnostics"]["items"][0]["message"] = "specialization T=second"
    receipt = {"process_wall_time_ms": 2.0, "process_cpu_time_ms": 2.0}

    result = spike._summarize_workload(
        {
            "name": "fixture",
            "manual_oracle": {"top": "top", "expected_instance_paths": ["top"]},
        },
        [worker, jittered],
        [receipt, receipt],
        None,
    )

    assert result["cold_process"]["stable_semantics"] is True
    stability = result["cold_process"]["diagnostic_stability"]
    assert stability["stable_full_payload"] is False
    assert stability["varying_by_code"]["AdvisoryLint"] == {
        "count": 2,
        "min": 10.0,
        "p50": 11.0,
        "p95": 11.9,
        "max": 12.0,
    }
    assert spike._diagnostic_semantic_identities(worker["diagnostics"]) == [
        {
            "code": "BadBinaryExpression",
            "option": None,
            "severity": "Error",
            "file": "/vendor/uvm.svh",
            "line": 714,
            "column": 15,
        }
    ]


def test_frontend_ipc_serialization_scope_contains_only_frontend_facts():
    diagnostics = {"blocking_error_count": 0, "items": []}
    recovered = {"tops": ["tb"], "instances": [{"path": "tb"}]}
    blockers = [{"code": "example", "phase": "elaboration"}]

    payload = spike._frontend_ipc_payload(diagnostics, recovered, blockers)
    serialized = spike._canonical_json(payload)

    assert json.loads(serialized) == {
        "diagnostics": diagnostics,
        "recovered": recovered,
        "blockers": blockers,
    }
    assert "phase_measurements" not in payload
    assert "invocation" not in payload


def test_semantic_projection_excludes_only_truncated_presentation_samples():
    base = {
        "tops": ["tb"],
        "instances": [{"path": "tb.first"}],
        "instances_truncated": True,
        "procedural_blocks": [{"path": "tb.first.proc"}],
        "procedural_blocks_truncated": True,
        "oracle_instance_path_membership": {"tb.dut": True},
        "definitions": [{"name": "tb"}],
        "object_counts": {"symbols_by_kind": {"Instance": 32000}},
    }
    jittered = deepcopy(base)
    jittered["instances"] = [{"path": "tb.second"}]
    jittered["procedural_blocks"] = [{"path": "tb.second.proc"}]

    assert spike._recovered_semantic_projection(base) == (
        spike._recovered_semantic_projection(jittered)
    )

    base["instances_truncated"] = False
    jittered["instances_truncated"] = False
    assert spike._recovered_semantic_projection(base) != (
        spike._recovered_semantic_projection(jittered)
    )


def test_semantic_projection_reports_but_does_not_key_on_aggregate_counts():
    first = {
        "tops": ["tb"],
        "definitions": [{"name": "tb"}],
        "object_counts": {"symbols_by_kind": {"Variable": 319721}},
        "count_scope": "frontend visitor facts",
    }
    second = deepcopy(first)
    second["object_counts"]["symbols_by_kind"]["Variable"] = 319762

    assert first != second
    assert spike._recovered_semantic_projection(first) == (
        spike._recovered_semantic_projection(second)
    )


def test_diagnostic_detail_cap_prioritizes_every_blocker_before_warnings():
    blocking = [{"severity": "Error", "id": index} for index in range(65)]
    warnings = [{"severity": "Warning", "id": index} for index in range(80)]
    suppressed = [{"severity": "Ignored", "id": index} for index in range(20)]

    items = spike._prioritize_diagnostic_items(
        blocking, warnings, suppressed, limit=100
    )

    assert items[:65] == blocking
    assert items[65:] == warnings[:35]
    assert not any(item["severity"] == "Ignored" for item in items)
