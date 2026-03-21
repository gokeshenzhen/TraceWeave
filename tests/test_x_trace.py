import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.x_trace import has_x_or_z, trace_x_source


class FakeParser:
    def __init__(self):
        self.values = {
            "top_tb.dut.out_sig": {"value": {"raw": "1x"}},
            "top_tb.dut.mid_sig": {"value": {"raw": "1x"}},
            "top_tb.dut.leaf_sig": {"value": {"raw": "1x"}},
            "top_tb.dut.clean_sig": {"value": {"raw": "10"}},
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


def test_trace_x_source_clean_signal(monkeypatch):
    parser = FakeParser()

    def fake_explain_signal_driver(**kwargs):
        raise AssertionError("clean signal should not query driver")

    monkeypatch.setattr("src.x_trace.explain_signal_driver", fake_explain_signal_driver)
    result = trace_x_source(
        wave_path="/tmp/a.vcd",
        signal_path="top_tb.dut.clean_sig",
        time_ps=0,
        compile_log="/tmp/compile.log",
        parser=parser,
        top_hint="top_tb",
    )

    assert result["trace_status"] == "signal_is_clean"
    assert result["propagation_chain"] == []


def test_trace_x_source_stops_at_instance_ports(monkeypatch):
    parser = FakeParser()

    def fake_explain_signal_driver(**kwargs):
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

    monkeypatch.setattr("src.x_trace.explain_signal_driver", fake_explain_signal_driver)
    result = trace_x_source(
        wave_path="/tmp/a.vcd",
        signal_path="top_tb.dut.out_sig",
        time_ps=0,
        compile_log="/tmp/compile.log",
        parser=parser,
        top_hint="top_tb",
    )

    assert result["trace_status"] == "instance_ports_listed"
    assert result["propagation_chain"][0]["trace_stop_reason"] == "instance_ports_listed"
    assert "bit-range continuity" in result["analysis_guide"]["step2"]


def test_trace_x_source_unresolved_leaf_returns_driver_unresolved(monkeypatch):
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

    def fake_explain_signal_driver(**kwargs):
        return responses[kwargs["signal_path"]]

    monkeypatch.setattr("src.x_trace.explain_signal_driver", fake_explain_signal_driver)
    result = trace_x_source(
        wave_path="/tmp/a.vcd",
        signal_path="top_tb.dut.out_sig",
        time_ps=0,
        compile_log="/tmp/compile.log",
        parser=parser,
        top_hint="top_tb",
    )

    assert result["trace_status"] == "driver_unresolved"
    assert result["propagation_chain"][-1]["signal_path"] == "top_tb.dut.leaf_sig"
    assert result["propagation_chain"][-1]["trace_stop_reason"] == "driver_unresolved"


def test_trace_x_source_clean_leaf_returns_traced_to_clean_leaf(monkeypatch):
    parser = FakeParser()

    def fake_explain_signal_driver(**kwargs):
        return {
            "driver_status": "resolved",
            "driver_kind": "assign",
            "resolved_module": "dut",
            "source_file": "/tmp/dut.sv",
            "expression_summary": "assign out_sig = clean_sig",
            "upstream_signals": ["clean_sig"],
        }

    monkeypatch.setattr("src.x_trace.explain_signal_driver", fake_explain_signal_driver)
    result = trace_x_source(
        wave_path="/tmp/a.vcd",
        signal_path="top_tb.dut.out_sig",
        time_ps=0,
        compile_log="/tmp/compile.log",
        parser=parser,
        top_hint="top_tb",
    )

    assert result["trace_status"] == "traced_to_clean_leaf"
    assert len(result["propagation_chain"]) == 1


def test_trace_x_source_missing_signal_returns_explicit_status(monkeypatch):
    parser = FakeParser()

    def fake_explain_signal_driver(**kwargs):
        raise AssertionError("missing waveform signal should not query driver")

    monkeypatch.setattr("src.x_trace.explain_signal_driver", fake_explain_signal_driver)
    result = trace_x_source(
        wave_path="/tmp/a.vcd",
        signal_path="top_tb.dut.unknown_sig",
        time_ps=0,
        compile_log="/tmp/compile.log",
        parser=parser,
        top_hint="top_tb",
    )

    assert result["trace_status"] == "signal_not_in_waveform"
    assert result["propagation_chain"][0]["trace_stop_reason"] == "signal_not_in_waveform"
