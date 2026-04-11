import os
from pathlib import Path

import pytest

import server


@pytest.fixture(autouse=True)
def _reset_session_state():
    server.reset_session_state()
    yield
    server.reset_session_state()


def _fake_sim_paths(simulator: str) -> server.schemas.SimPathsResult:
    return server.schemas.SimPathsResult.model_validate(
        {
            "verif_root": "/tmp/verif",
            "case_name": "case0",
            "config_source": "auto",
            "discovery_mode": "case_dir",
            "case_dir": "/tmp/verif/work_case0",
            "simulator": simulator,
            "compile_logs": [],
            "sim_logs": [],
            "wave_files": [],
            "available_cases": [],
            "hints": [],
        }
    )


@pytest.mark.anyio
async def test_dispatch_build_only_session_uses_build_hierarchy_provenance_for_explain_signal_driver(
    monkeypatch, tmp_path
):
    compile_log = tmp_path / "compile.log"
    compile_log.write_text("xrun\nxmelab\n")
    server._session_state["build_tb_hierarchy"] = {"compile_log": str(compile_log), "simulator": "xcelium"}
    server._result_cache["build_tb_hierarchy"] = server.schemas.BuildTbHierarchyResult.model_validate(
        {
            "project": {"top_module": "top_tb", "simulator": "vcs"},
            "files": {},
            "component_tree": {},
            "class_hierarchy": [],
            "interfaces": [],
            "compile_result": {},
        }
    )
    server._result_provenance["build_tb_hierarchy"] = {
        "compile_log": str(compile_log),
        "simulator": "xcelium",
    }

    rtl = tmp_path / "dut.sv"
    rtl.write_text(
        """\
module top_tb;
  dut u0();
endmodule

module dut;
  logic a, b;
  assign K_sub = a ^ b;
endmodule
"""
    )
    wave_path = tmp_path / "wave.vcd"
    wave_path.write_text("$timescale 1ps $end\n$enddefinitions $end\n#0\n")

    captured: dict[str, str] = {}

    def fake_parse_compile_log(log_path, simulator="auto"):
        captured["simulator"] = simulator
        return {
            "top_modules": ["top_tb"],
            "files": {"user": [{"path": str(rtl), "type": "module", "category": "rtl"}]},
        }

    monkeypatch.setattr("src.signal_driver.parse_compile_log", fake_parse_compile_log)

    result = await server._dispatch(
        "explain_signal_driver",
        {
            "signal_path": "top_tb.u0.K_sub",
            "wave_path": str(wave_path),
            "compile_log": str(compile_log),
            "top_hint": "top_tb",
        },
    )

    assert captured["simulator"] == "xcelium"
    assert result["driver_status"] == "resolved"


@pytest.mark.anyio
async def test_build_only_session_stale_compile_log_falls_back_to_auto_for_explain_signal_driver(
    monkeypatch, tmp_path
):
    old_compile_log = tmp_path / "old_compile.log"
    new_compile_log = tmp_path / "new_compile.log"
    old_compile_log.write_text("xrun\nxmelab\n")
    new_compile_log.write_text("xrun\nxmelab\n")
    server._session_state["build_tb_hierarchy"] = {"compile_log": str(old_compile_log), "simulator": "xcelium"}
    server._result_cache["build_tb_hierarchy"] = server.schemas.BuildTbHierarchyResult.model_validate(
        {
            "project": {"top_module": "top_tb", "simulator": "vcs"},
            "files": {},
            "component_tree": {},
            "class_hierarchy": [],
            "interfaces": [],
            "compile_result": {},
        }
    )
    server._result_provenance["build_tb_hierarchy"] = {
        "compile_log": str(old_compile_log),
        "simulator": "xcelium",
    }

    rtl = tmp_path / "dut.sv"
    rtl.write_text(
        """\
module top_tb;
  dut u0();
endmodule

module dut;
  logic a, b;
  assign K_sub = a ^ b;
endmodule
"""
    )
    wave_path = tmp_path / "wave.vcd"
    wave_path.write_text("$timescale 1ps $end\n$enddefinitions $end\n#0\n")

    captured: dict[str, str] = {}

    def fake_parse_compile_log(log_path, simulator="auto"):
        captured["simulator"] = simulator
        return {
            "top_modules": ["top_tb"],
            "files": {"user": [{"path": str(rtl), "type": "module", "category": "rtl"}]},
        }

    monkeypatch.setattr("src.signal_driver.parse_compile_log", fake_parse_compile_log)

    result = await server._dispatch(
        "explain_signal_driver",
        {
            "signal_path": "top_tb.u0.K_sub",
            "wave_path": str(wave_path),
            "compile_log": str(new_compile_log),
            "top_hint": "top_tb",
        },
    )

    assert captured["simulator"] == "auto"
    assert result["driver_status"] == "resolved"


@pytest.mark.anyio
async def test_dispatch_auto_injects_cached_simulator_for_explain_signal_driver(monkeypatch, tmp_path):
    server._session_state["build_tb_hierarchy"] = {"compile_log": "/tmp/elab.log", "simulator": "vcs"}
    server._result_cache["get_sim_paths"] = _fake_sim_paths("vcs")

    rtl = tmp_path / "dut.sv"
    rtl.write_text(
        """\
module top_tb;
  dut u0();
endmodule

module dut;
  logic a, b;
  assign K_sub = a ^ b;
endmodule
"""
    )
    wave_path = tmp_path / "wave.vcd"
    wave_path.write_text("$timescale 1ps $end\n$enddefinitions $end\n#0\n")

    captured: dict[str, str] = {}

    def fake_parse_compile_log(log_path, simulator="auto"):
        captured["simulator"] = simulator
        return {
            "top_modules": ["top_tb"],
            "files": {"user": [{"path": str(rtl), "type": "module", "category": "rtl"}]},
        }

    monkeypatch.setattr("src.signal_driver.parse_compile_log", fake_parse_compile_log)

    result = await server._dispatch(
        "explain_signal_driver",
        {
            "signal_path": "top_tb.u0.K_sub",
            "wave_path": str(wave_path),
            "compile_log": str(tmp_path / "compile.log"),
            "top_hint": "top_tb",
        },
    )

    assert captured["simulator"] == "vcs"
    assert result["driver_status"] == "resolved"


@pytest.mark.anyio
async def test_explicit_simulator_override_beats_cached_value(monkeypatch, tmp_path):
    server._session_state["build_tb_hierarchy"] = {"compile_log": "/tmp/elab.log", "simulator": "vcs"}
    server._result_cache["get_sim_paths"] = _fake_sim_paths("vcs")

    rtl = tmp_path / "dut.sv"
    rtl.write_text(
        """\
module top_tb;
  dut u0();
endmodule

module dut;
  logic a, b;
  assign K_sub = a ^ b;
endmodule
"""
    )
    wave_path = tmp_path / "wave.vcd"
    wave_path.write_text("$timescale 1ps $end\n$enddefinitions $end\n#0\n")

    captured: dict[str, str] = {}

    def fake_parse_compile_log(log_path, simulator="auto"):
        captured["simulator"] = simulator
        return {
            "top_modules": ["top_tb"],
            "files": {"user": [{"path": str(rtl), "type": "module", "category": "rtl"}]},
        }

    monkeypatch.setattr("src.signal_driver.parse_compile_log", fake_parse_compile_log)

    await server._dispatch(
        "explain_signal_driver",
        {
            "signal_path": "top_tb.u0.K_sub",
            "wave_path": str(wave_path),
            "compile_log": str(tmp_path / "compile.log"),
            "top_hint": "top_tb",
            "simulator": "xcelium",
        },
    )

    assert captured["simulator"] == "xcelium"


@pytest.mark.anyio
async def test_trace_x_source_build_only_session_uses_build_hierarchy_provenance(monkeypatch, tmp_path):
    compile_log = tmp_path / "compile.log"
    compile_log.write_text("xrun\nxmelab\n")
    server._session_state["build_tb_hierarchy"] = {"compile_log": str(compile_log), "simulator": "xcelium"}
    server._result_cache["build_tb_hierarchy"] = server.schemas.BuildTbHierarchyResult.model_validate(
        {
            "project": {"top_module": "top_tb", "simulator": "vcs"},
            "files": {},
            "component_tree": {},
            "class_hierarchy": [],
            "interfaces": [],
            "compile_result": {},
        }
    )
    server._result_provenance["build_tb_hierarchy"] = {
        "compile_log": str(compile_log),
        "simulator": "xcelium",
    }

    wave_path = tmp_path / "wave.vcd"
    wave_path.write_text("$timescale 1ps $end\n$enddefinitions $end\n#0\n")
    captured: dict[str, str] = {}

    class _FakeParser:
        pass

    def fake_trace_x_source(**kwargs):
        captured["simulator"] = kwargs["simulator"]
        return {
            "start_signal": kwargs["signal_path"],
            "start_time_ps": kwargs["time_ps"],
            "trace_status": "signal_is_clean",
            "trace_depth": 0,
            "max_depth": kwargs["max_depth"],
            "propagation_chain": [],
            "root_cause": None,
            "analysis_guide": {"step1": "clean"},
        }

    monkeypatch.setattr(server, "_get_parser", lambda wave_path: _FakeParser())
    monkeypatch.setattr(server, "trace_x_source", fake_trace_x_source)

    await server._dispatch(
        "trace_x_source",
        {
            "signal_path": "top_tb.u0.sig",
            "wave_path": str(wave_path),
            "compile_log": str(compile_log),
            "time_ps": 0,
        },
    )

    assert captured["simulator"] == "xcelium"


@pytest.mark.anyio
async def test_trace_x_source_build_only_stale_compile_log_falls_back_to_auto(monkeypatch, tmp_path):
    old_compile_log = tmp_path / "old_compile.log"
    new_compile_log = tmp_path / "new_compile.log"
    old_compile_log.write_text("xrun\nxmelab\n")
    new_compile_log.write_text("xrun\nxmelab\n")
    server._session_state["build_tb_hierarchy"] = {"compile_log": str(old_compile_log), "simulator": "xcelium"}
    server._result_cache["build_tb_hierarchy"] = server.schemas.BuildTbHierarchyResult.model_validate(
        {
            "project": {"top_module": "top_tb", "simulator": "vcs"},
            "files": {},
            "component_tree": {},
            "class_hierarchy": [],
            "interfaces": [],
            "compile_result": {},
        }
    )
    server._result_provenance["build_tb_hierarchy"] = {
        "compile_log": str(old_compile_log),
        "simulator": "xcelium",
    }

    wave_path = tmp_path / "wave.vcd"
    wave_path.write_text("$timescale 1ps $end\n$enddefinitions $end\n#0\n")
    captured: dict[str, str] = {}

    class _FakeParser:
        pass

    def fake_trace_x_source(**kwargs):
        captured["simulator"] = kwargs["simulator"]
        return {
            "start_signal": kwargs["signal_path"],
            "start_time_ps": kwargs["time_ps"],
            "trace_status": "signal_is_clean",
            "trace_depth": 0,
            "max_depth": kwargs["max_depth"],
            "propagation_chain": [],
            "root_cause": None,
            "analysis_guide": {"step1": "clean"},
        }

    monkeypatch.setattr(server, "_get_parser", lambda wave_path: _FakeParser())
    monkeypatch.setattr(server, "trace_x_source", fake_trace_x_source)

    await server._dispatch(
        "trace_x_source",
        {
            "signal_path": "top_tb.u0.sig",
            "wave_path": str(wave_path),
            "compile_log": str(new_compile_log),
            "time_ps": 0,
        },
    )

    assert captured["simulator"] == "auto"


@pytest.mark.anyio
async def test_trace_x_source_dispatch_receives_auto_injected_simulator(monkeypatch, tmp_path):
    server._session_state["build_tb_hierarchy"] = {"compile_log": "/tmp/elab.log", "simulator": "vcs"}
    server._result_cache["get_sim_paths"] = _fake_sim_paths("vcs")

    wave_path = tmp_path / "wave.vcd"
    wave_path.write_text("$timescale 1ps $end\n$enddefinitions $end\n#0\n")
    captured: dict[str, str] = {}

    class _FakeParser:
        pass

    def fake_trace_x_source(**kwargs):
        captured["simulator"] = kwargs["simulator"]
        return {
            "start_signal": kwargs["signal_path"],
            "start_time_ps": kwargs["time_ps"],
            "trace_status": "signal_is_clean",
            "trace_depth": 0,
            "max_depth": kwargs["max_depth"],
            "propagation_chain": [],
            "root_cause": None,
            "analysis_guide": {"step1": "clean"},
        }

    monkeypatch.setattr(server, "_get_parser", lambda wave_path: _FakeParser())
    monkeypatch.setattr(server, "trace_x_source", fake_trace_x_source)

    await server._dispatch(
        "trace_x_source",
        {
            "signal_path": "top_tb.u0.sig",
            "wave_path": str(wave_path),
            "compile_log": str(tmp_path / "compile.log"),
            "time_ps": 0,
        },
    )

    assert captured["simulator"] == "vcs"


@pytest.mark.anyio
async def test_get_sim_paths_resets_downstream_cache_when_session_identity_changes(tmp_path):
    first_root = tmp_path / "first"
    first_case = first_root / "work_case0"
    first_case.mkdir(parents=True)
    (first_root / "elab.log").write_text("xrun\nxmelab\n")
    (first_case / "irun.log").write_text("module_a ERROR issue @ 1 ns\n")

    await server._dispatch("get_sim_paths", {"verif_root": str(first_root), "case_name": "case0"})

    server._result_cache["build_tb_hierarchy"] = server.schemas.BuildTbHierarchyResult.model_validate(
        {
            "project": {"top_module": "top_tb", "simulator": "xcelium"},
            "files": {},
            "component_tree": {},
            "class_hierarchy": [],
            "interfaces": [],
            "compile_result": {},
        }
    )

    second_root = tmp_path / "second"
    second_case = second_root / "work_case1"
    second_case.mkdir(parents=True)
    (second_root / "elab.log").write_text("Chronologic VCS\n")
    (second_case / "irun.log").write_text("module_b ERROR issue @ 2 ns\n")

    await server._dispatch("get_sim_paths", {"verif_root": str(second_root), "case_name": "case1"})

    assert server._result_cache["build_tb_hierarchy"] is None
