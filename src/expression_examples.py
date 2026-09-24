"""Small, shared client examples; expectations are hand-calculated from demo.vcd."""


def expression_examples(wave_path="/absolute/path/to/TraceWeave/examples/expressions/demo.vcd"):
    bindings = {"a": "tb.data", "i": "tb.index", "e": "tb.expected"}
    return [
        {
            "name": "dynamic_bit",
            "tool": "get_signal_at_time",
            "arguments": {"wave_path": wave_path, "time_ps": 5, "signal_path": {
                "expr": "a[i] + e", "typing": "wave_bits", "bindings": bindings}},
            "expected": {"value": 6},
        },
        {
            "name": "dynamic_array",
            "tool": "get_signal_transitions",
            "arguments": {"wave_path": wave_path, "start_time_ps": 0, "end_time_ps": 25,
                "signal_path": {"expr": "mem[i]", "bindings": {
                    "i": "tb.index", "mem": {"path_template": "tb.mem[{index}][7:0]"}},
                    "types": {"i": {"width": 3}, "mem": {
                        "width": 8, "unpacked": [[0, 7]]}}}},
            "expected": {"times": [0, 10, 20], "values": [17, 34, 51]},
        },
        {
            "name": "typed_slice",
            "tool": "get_signals_by_cycle",
            "arguments": {"wave_path": wave_path, "clock_path": "tb.clk", "num_cycles": 4,
                "sample_offset_ps": 1, "signal_paths": [{
                    "expr": "a[i -: W]", "bindings": {"a": "tb.data", "i": "tb.index"},
                    "types": {"a": {"width": 8}, "i": {"width": 3}},
                    "constants": {"W": "2"}}]},
            "expected": {"values": [2, 1, 2, None]},
        },
        {
            "name": "window_counterexample",
            "tool": "verify_window",
            "arguments": {"wave_path": wave_path, "clock": "tb.clk", "mode": "always",
                "start_time_ps": 0, "end_time_ps": 25, "predicate": [{
                    "expr": "a[i] == 1'b1", "typing": "wave_bits",
                    "bindings": {"a": "tb.data", "i": "tb.index"}}]},
            "expected": {"holds": False, "unknown_cycles": 0},
        },
    ]


def check_example(example, payload):
    """Check values and coverage, including the deliberately unknown last slice."""
    if payload.get("error"):
        raise AssertionError(payload)
    receipts = payload.get("expressions", [])
    assert receipts and all(r["coverage_status"] == "complete" for r in receipts), receipts
    if example["tool"] == "get_signal_at_time":
        actual = {"value": payload["value"]["dec"]}
    elif example["tool"] == "get_signal_transitions":
        actual = {"times": [row["time_ps"] for row in payload["transitions"]],
                  "values": [row["value"]["dec"] for row in payload["transitions"]]}
        assert not payload["truncated"]
    elif example["tool"] == "get_signals_by_cycle":
        key = receipts[0]["key"]
        actual = {"values": [row["signals"][key].get("dec") for row in payload["cycles"]]}
        assert payload["cycles"][-1]["signals"][key]["bin"].lower().endswith("xx")
    else:
        actual = {k: payload[k] for k in ("holds", "unknown_cycles")}
        assert payload.get("counterexample"), payload
    assert actual == example["expected"], (actual, example["expected"])
    return actual
