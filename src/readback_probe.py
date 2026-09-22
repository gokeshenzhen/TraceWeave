"""License-free MCP readback checks over a neutral, fixed VCD fixture.

This observes an SDK client's catalog and calls, not an AI client's catalog
exposure or a model's natural tool adoption. It is never an MCP tool.
"""

import json
import time
from importlib.metadata import version


PROBE_VCD = """$timescale 1ns $end
$scope module readback_probe $end
$var wire 1 ! clk $end
$var reg 4 " data $end
$var reg 4 # unknown $end
$var wire 1 $ late $end
$upscope $end
$enddefinitions $end
#0
1!
b0001 "
bx01z #
#5
0!
#10
1!
b1010 "
b10xz #
0$
#15
0!
#20
1!
b0110 "
1$
"""

READBACK_TOOLS = (
    "get_waveform_summary", "search_signals", "get_signal_at_time",
    "get_signals_around_time", "get_signals_by_cycle",
)


class ReadbackCheckFailure(Exception):
    """A fixed check label, never a server exception or user input."""


def _require(condition, label):
    if not condition:
        raise ReadbackCheckFailure(label)


def _schema_summary(value):
    if isinstance(value, dict):
        return {k: _schema_summary(v) for k, v in value.items() if k not in {"description", "title"}}
    if isinstance(value, list):
        return [_schema_summary(v) for v in value]
    return value


def client_probe_requests(wave_path):
    """Explicit requests for a separate AI-client smoke session."""
    return [
        {"tool": "get_signal_at_time", "arguments": {
            "wave_path": wave_path, "signal_path": "readback_probe.data", "time_ps": 0,
        }, "expected": {"time_ps": 0, "value.dec": 1}},
        {"tool": "get_signals_around_time", "arguments": {
            "wave_path": wave_path,
            "signal_paths": ["readback_probe.data", "readback_probe.unknown"],
            "center_time_ps": 10000, "window_ps": 0,
            "extra_transitions": 0, "return_mode": "values_only",
        }, "expected": {"center_time_ps": 10000, "data.dec": 10, "unknown.bin": "b10xz"}},
    ]


async def check_readback(session, wave_path, initialized):
    """Use a live MCP session; return observations even if a check fails."""
    report = {
        "status": "failed",
        "server_info": initialized.serverInfo.model_dump(exclude_none=True),
        "protocol_version": initialized.protocolVersion,
        "protocol_client": {"kind": "mcp_python_sdk", "version": version("mcp")},
        "ai_client": {"version": None, "catalog_visibility": "unknown", "readback_calls": "unknown"},
        "model_adoption": "not_measured",
        "calls": [],
    }

    async def call(name, args, *, expect_error=False):
        start = time.perf_counter()
        result = await session.call_tool(name, args)
        raw = "\n".join(c.text for c in result.content if c.type == "text")
        report["calls"].append({
            "tool": name, "is_error": result.isError,
            "response_text_bytes": len(raw.encode("utf-8")),
            "latency_ms": round((time.perf_counter() - start) * 1000, 3),
        })
        _require(result.isError == expect_error, "unexpected_tool_error_status")
        body = json.loads(raw)
        if body.get("format") == "traceweave.compact.v1":
            body = body["result"]
        _require(expect_error or "error" not in body, "tool_error_payload")
        return body

    try:
        tools = (await session.list_tools()).tools
        by_name = {tool.name: tool for tool in tools}
        report["server_catalog"] = {
            "status": "observed", "tool_names": sorted(by_name),
            "readback_schemas": {name: _schema_summary(by_name[name].inputSchema) for name in READBACK_TOOLS if name in by_name},
        }
        missing = sorted(set(READBACK_TOOLS) - by_name.keys())
        report["missing_tools"] = missing
        _require(not missing, "tools_not_registered")
        summary = await call("get_waveform_summary", {"wave_path": wave_path})
        _require(summary["timescale_ps"] == 1000, "timescale_mismatch")
        search = await call("search_signals", {
            "wave_path": wave_path, "keyword": ["data", "unknown", "absent"],
        })
        _require([row["keyword"] for row in search["batch"]] == ["data", "unknown", "absent"], "search_order_mismatch")
        _require(search["batch"][-1]["total_matched"] == 0, "zero_match_mismatch")
        paths = ["readback_probe." + name for name in ("data", "unknown", "late")]
        # The existing VCD reader preserves the binary prefix for X/Z vectors.
        for timestamp, data, unknown in ((0, 1, "bx01z"), (10000, 10, "b10xz")):
            points = {}
            for path in paths:
                point = await call("get_signal_at_time", {
                    "wave_path": wave_path, "signal_path": path, "time_ps": timestamp,
                    "output_format": "compact",
                })
                _require(point["time_ps"] == timestamp, "point_time_mismatch")
                points[path] = point.get("value")
            batch = await call("get_signals_around_time", {
                "wave_path": wave_path, "signal_paths": paths + ["readback_probe.absent"],
                "center_time_ps": timestamp, "window_ps": 0, "extra_transitions": 0,
                "return_mode": "values_only", "output_format": "compact",
            })
            _require(batch["center_time_ps"] == timestamp and not batch["truncated"], "batch_time_or_coverage_mismatch")
            _require(batch["return_mode"] == "values_only", "return_mode_mismatch")
            for path in paths:
                entry = batch["signals"][path]
                _require(entry.get("value_at_center") == points[path], "point_batch_value_mismatch")
                _require("transitions_in_window" not in entry and "pre_window_transitions" not in entry, "unexpected_history")
            _require(points[paths[0]]["dec"] == data, "fixture_value_mismatch")
            _require(points[paths[1]]["bin"] == unknown and points[paths[1]].get("dec") is None, "xz_semantics_mismatch")
            if timestamp == 0:
                _require(points[paths[2]] is None, "uninitialized_value_mismatch")
            _require("error" in batch["signals"]["readback_probe.absent"], "missing_signal_not_reported")
        cycles = await call("get_signals_by_cycle", {
            "wave_path": wave_path, "clock_path": "readback_probe.clk",
            "signal_paths": paths[:1], "start_cycle": 0, "num_cycles": 3,
            "edge": "posedge", "sample_offset_ps": 0,
        })
        _require([c["time_ps"] for c in cycles["cycles"]] == [10000, 20000], "initial_clock_edge_mismatch")
        _require(cycles["num_cycles_returned"] == 2 and cycles["truncated"], "short_cycle_read_not_reported")
        error = await call("search_signals", {
            "wave_path": wave_path, "keyword": ["data"] * 17,
        }, expect_error=True)
        _require(error.get("error_code") == "too_many_keywords" and error.get("search_executed") is False, "keyword_recovery_missing")
        report["status"] = "passed"
    except ReadbackCheckFailure as exc:
        report["failure_reason"] = str(exc)
    except Exception as exc:
        report["failure_reason"] = "protocol_or_response_failure"
        report["exception_type"] = type(exc).__name__
    return report
