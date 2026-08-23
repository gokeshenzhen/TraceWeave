import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.x_trace import has_x_or_z, trace_x_source
from src.cancellation import OperationCancelled
from src.schemas import TraceXSourceResult


class FakeParser:
    def __init__(self):
        self.values = {
            "top_tb.dut.out_sig": {"value": {"raw": "1x"}},
            "top_tb.dut.mid_sig": {"value": {"raw": "1x"}},
            "top_tb.dut.leaf_sig": {"value": {"raw": "1x"}},
            "top_tb.dut.clean_sig": {"value": {"raw": "10"}},
            "top_tb.source.full_sig": {"value": {"raw": "1x"}},
        }

    def get_value_at_time(self, signal_path, time_ps):
        if signal_path not in self.values:
            raise KeyError(signal_path)
        return self.values[signal_path]

    def search_signals(self, keyword, max_results=10):
        matches = []
        for path in self.values:
            if path.endswith("." + keyword):
                matches.append({"path": path})
        return {"results": matches[:max_results]}


def test_has_x_or_z_accepts_raw_and_bin():
    assert has_x_or_z({"value": {"raw": "1x0"}}) is True
    assert has_x_or_z({"value": {"bin": "10z"}}) is True
    assert has_x_or_z({"value": {"hex": "0xa"}}) is False


@pytest.mark.anyio
async def test_trace_x_source_clean_signal():
    parser = FakeParser()

    def fake_driver_lookup(signal_path):
        raise AssertionError("clean signal should not query driver")

    result = await trace_x_source(
        wave_path="/tmp/a.vcd",
        signal_path="top_tb.dut.clean_sig",
        time_ps=0,
        compile_log="/tmp/compile.log",
        parser=parser,
        top_hint="top_tb",
        driver_lookup=fake_driver_lookup,
    )

    assert result["trace_status"] == "signal_is_clean"
    assert result["propagation_chain"] == []


@pytest.mark.anyio
async def test_trace_x_source_stops_at_instance_ports():
    parser = FakeParser()

    def fake_driver_lookup(signal_path):
        return {
            "driver_status": "resolved",
            "driver_kind": "instance_ports",
            "resolved_module": "dut",
            "source_file": "/tmp/dut.sv",
            "expression_summary": "S driven by 2 instance port(s)",
            "instance_port_connections": [
                {
                    "instance_module": "leaf",
                    "instance_name": "u0",
                    "port_name": "dout",
                    "connected_expression": "out_sig[3:0]",
                    "source_line": 10,
                }
            ],
        }

    result = await trace_x_source(
        wave_path="/tmp/a.vcd",
        signal_path="top_tb.dut.out_sig",
        time_ps=0,
        compile_log="/tmp/compile.log",
        parser=parser,
        top_hint="top_tb",
        driver_lookup=fake_driver_lookup,
    )

    assert result["trace_status"] == "instance_ports_listed"
    assert (
        result["propagation_chain"][0]["trace_stop_reason"] == "instance_ports_listed"
    )
    assert "bit-range continuity" in result["analysis_guide"]["step2"]


@pytest.mark.anyio
async def test_trace_x_source_unresolved_leaf_returns_driver_unresolved():
    parser = FakeParser()
    responses = {
        "top_tb.dut.out_sig": {
            "driver_status": "resolved",
            "driver_kind": "assign",
            "resolved_module": "dut",
            "source_file": "/tmp/dut.sv",
            "expression_summary": "assign out_sig = leaf_sig",
            "upstream_signals": ["leaf_sig"],
        },
        "top_tb.dut.leaf_sig": {
            "driver_status": "partial",
            "driver_kind": "unknown",
            "resolved_module": "dut",
            "source_file": "/tmp/dut.sv",
            "expression_summary": "leaf without simple driver",
            "upstream_signals": [],
        },
    }

    def fake_driver_lookup(signal_path):
        return responses[signal_path]

    result = await trace_x_source(
        wave_path="/tmp/a.vcd",
        signal_path="top_tb.dut.out_sig",
        time_ps=0,
        compile_log="/tmp/compile.log",
        parser=parser,
        top_hint="top_tb",
        driver_lookup=fake_driver_lookup,
    )

    assert result["trace_status"] == "driver_unresolved"
    assert result["propagation_chain"][-1]["signal_path"] == "top_tb.dut.leaf_sig"
    assert result["propagation_chain"][-1]["trace_stop_reason"] == "driver_unresolved"


@pytest.mark.anyio
async def test_trace_x_source_preserves_bounded_partial_driver_fact():
    parser = FakeParser()
    traversal = {
        "returned_fact_count": 32,
        "output_limit": 32,
        "output_truncated": True,
        "visited_state_count": 4096,
        "state_limit": 4096,
        "state_truncated": True,
        "search_exhaustive": False,
        "incomplete_reasons": ["output_limit", "work_limit"],
        "continuation_supported": False,
    }

    result = await trace_x_source(
        wave_path="/tmp/a.vcd",
        signal_path="top_tb.dut.out_sig",
        time_ps=0,
        compile_log="/tmp/compile.log",
        parser=parser,
        driver_lookup=lambda signal_path: {
            "driver_status": "partial",
            "driver_kind": "always_ff",
            "resolved_module": "dut",
            "source_file": "/tmp/dut.sv",
            "source_line": 12,
            "confidence": "partial",
            "upstream_signals": [],
            "traversal": traversal,
        },
    )

    assert result["trace_status"] == "driver_traversal_incomplete"
    node = result["propagation_chain"][0]
    assert node["trace_stop_reason"] == "driver_traversal_incomplete"
    assert node["traversal"] == traversal
    assert result["root_cause"]["stop_reason"] == "driver_traversal_incomplete"
    assert "exclusive root cause" in result["analysis_guide"]["step2"]


@pytest.mark.anyio
async def test_trace_x_source_clean_leaf_returns_traced_to_clean_leaf():
    parser = FakeParser()

    def fake_driver_lookup(signal_path):
        return {
            "driver_status": "resolved",
            "driver_kind": "assign",
            "resolved_module": "dut",
            "source_file": "/tmp/dut.sv",
            "expression_summary": "assign out_sig = clean_sig",
            "upstream_signals": ["clean_sig"],
        }

    result = await trace_x_source(
        wave_path="/tmp/a.vcd",
        signal_path="top_tb.dut.out_sig",
        time_ps=0,
        compile_log="/tmp/compile.log",
        parser=parser,
        top_hint="top_tb",
        driver_lookup=fake_driver_lookup,
    )

    assert result["trace_status"] == "traced_to_clean_leaf"
    assert len(result["propagation_chain"]) == 1


@pytest.mark.anyio
async def test_trace_x_source_missing_signal_returns_explicit_status():
    parser = FakeParser()

    def fake_driver_lookup(signal_path):
        raise AssertionError("missing waveform signal should not query driver")

    result = await trace_x_source(
        wave_path="/tmp/a.vcd",
        signal_path="top_tb.dut.unknown_sig",
        time_ps=0,
        compile_log="/tmp/compile.log",
        parser=parser,
        top_hint="top_tb",
        driver_lookup=fake_driver_lookup,
    )

    assert result["trace_status"] == "signal_not_in_waveform"
    assert (
        result["propagation_chain"][0]["trace_stop_reason"] == "signal_not_in_waveform"
    )


@pytest.mark.anyio
async def test_trace_x_source_accepts_backend_qualified_upstream_path():
    parser = FakeParser()
    responses = {
        "top_tb.dut.out_sig": {
            "driver_status": "resolved",
            "driver_kind": "assign",
            "resolved_module": "dut",
            "source_file": "/tmp/dut.sv",
            "expression_summary": "backend-qualified edge",
            "upstream_signals": ["top_tb.source.full_sig"],
        },
        "top_tb.source.full_sig": {
            "driver_status": "partial",
            "driver_kind": "unknown",
            "resolved_module": "source",
            "source_file": "/tmp/source.sv",
            "expression_summary": "upstream leaf",
            "upstream_signals": [],
        },
    }

    result = await trace_x_source(
        wave_path="/tmp/a.vcd",
        signal_path="top_tb.dut.out_sig",
        time_ps=0,
        compile_log="/tmp/compile.log",
        parser=parser,
        driver_lookup=lambda signal_path: responses[signal_path],
    )

    assert result["propagation_chain"][0]["x_upstream_signals"] == [
        "top_tb.source.full_sig"
    ]
    assert result["propagation_chain"][1]["signal_path"] == "top_tb.source.full_sig"


@pytest.mark.anyio
async def test_trace_x_source_does_not_swallow_cooperative_cancellation():
    async def cancelled_value_lookup(signal_path, time_ps):
        raise OperationCancelled("cancelled")

    with pytest.raises(OperationCancelled):
        await trace_x_source(
            wave_path="/tmp/a.vcd",
            signal_path="top_tb.dut.out_sig",
            time_ps=0,
            compile_log="/tmp/compile.log",
            parser=None,
            driver_lookup=lambda signal_path: {},
            value_lookup=cancelled_value_lookup,
            upstream_lookup=lambda names, current, time_ps: [],
        )


@pytest.mark.anyio
async def test_trace_x_source_preserves_npi_testbench_driver_evidence():
    parser = FakeParser()
    cross_check = {
        "performed": True,
        "conflict": True,
        "matched_scope": "top_tb.dut.consumer",
        "matched_line": 42,
        "note": "reported driver is also a load",
    }

    result = await trace_x_source(
        wave_path="/tmp/a.vcd",
        signal_path="top_tb.dut.out_sig",
        time_ps=0,
        compile_log="/tmp/compile.log",
        parser=parser,
        driver_lookup=lambda signal_path: {
            "driver_status": "testbench_driven",
            "driver_kind": None,
            "resolved_module": "dut",
            "source_file": None,
            "source_line": 42,
            "expression_summary": "real driver is testbench/behavioral",
            "confidence": None,
            "unsupported_reason": "driver_is_load_real_driver_is_testbench",
            "upstream_signals": [],
            "cross_check": cross_check,
        },
    )

    node = result["propagation_chain"][0]
    assert result["trace_status"] == "testbench_driven"
    assert node["driver_status"] == "testbench_driven"
    assert node["source_line"] == 42
    assert node["cross_check"] == cross_check
    assert node["trace_stop_reason"] == "testbench_driven"
    assert result["root_cause"]["stop_reason"] == "testbench_driven"
    assert "UVM driver/BFM" in result["analysis_guide"]["step2"]
    TraceXSourceResult.model_validate(result)


@pytest.mark.anyio
async def test_trace_x_source_marks_resolved_driver_without_upstream_as_partial():
    result = await trace_x_source(
        wave_path="/tmp/a.vcd",
        signal_path="top_tb.dut.out_sig",
        time_ps=0,
        compile_log="/tmp/compile.log",
        parser=FakeParser(),
        driver_lookup=lambda signal_path: {
            "driver_status": "resolved",
            "driver_kind": "always_ff",
            "resolved_module": "dut",
            "source_file": "/tmp/dut.sv",
            "source_line": 18,
            "expression_summary": "NPI register driver",
            "confidence": "exact",
            "claim_semantics": {
                "positive_fact_confidence": "exact",
                "target_bit_coverage": "complete",
                "global_coverage_status": "inconclusive",
                "exhaustive_search": False,
                "exclusive_driver_proved": False,
                "negative_claim_allowed": False,
            },
            "upstream_signals": [],
        },
    )

    node = result["propagation_chain"][0]
    assert result["trace_status"] == "traced_partial_chain"
    assert node["trace_stop_reason"] == "no_upstream_candidates"
    assert node["claim_semantics"]["positive_fact_confidence"] == "exact"
    assert "did not expose" in result["analysis_guide"]["step1"]
