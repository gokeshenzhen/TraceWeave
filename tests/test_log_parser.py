"""
test_log_parser.py
覆盖：两阶段 log 解析、分组摘要、通用 error 捕获、上下文提取
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import src.log_parser as log_parser_module
from src.log_parser import SimLogParser, get_error_context
from src.problem_hints import compute_problem_hints_from_events, event_has_x_or_z


VCS_LOG_SAMPLE = """\
Command: /home/robin/Projects/mcp_demo/tb/../tb/work/simv +UVM_TESTNAME=my_case0
Chronologic VCS simulator copyright 1991-2018

"/home/robin/Projects/mcp_demo/tb/../tb/sva_top.sv", 66: top_tb.sva_top_inst.apUNEXPECTED_ASSERTION: started at 270000ps failed at 290000ps
    Offending '(s_bits == STATE1)'
"/home/robin/Projects/mcp_demo/tb/../tb/sva_top.sv", 79: top_tb.sva_top_inst.apEXPECTED_ASSERTION_1: started at 270000ps failed at 290000ps
    Offending '(s_bits == STATE2)'
"/home/robin/Projects/mcp_demo/tb/../tb/sva_top.sv", 66: top_tb.sva_top_inst.apUNEXPECTED_ASSERTION: started at 290000ps failed at 310000ps
    Offending '(s_bits == STATE1)'
UVM_ERROR /home/robin/Projects/mcp_demo/tb/../tb/top_tb.sv(125) @ 1661.000 ns: reporter [TOP] a=1, b=0
UVM_ERROR /home/robin/Projects/mcp_demo/tb/../tb/top_tb.sv(125) @ 2128.000 ns: reporter [TOP] a=1, b=3
UVM_FATAL /home/robin/Projects/mcp_demo/tb/../tb/top_tb.sv(130) @ 2500.000 ns: reporter [TOP] stop simulation
"""

XCE_LOG_SAMPLE = """\
xmsim: *E,ASRTST (/path/sva_top.sv,66): (time 270 NS) Assertion top_tb.sva_top_inst.apUNEXPECTED_ASSERTION has failed (2 cycles, starting 250 NS)
    $rose(start) |=> s_bits == STATE1 ##1 s_bits == STATE2;
xmsim: *E,ASRTST (/path/sva_top.sv,79): (time 290 NS) Assertion top_tb.sva_top_inst.apEXPECTED_ASSERTION_1 has failed (2 cycles, starting 270 NS)
UVM_ERROR /path/top_tb.sv(129) @ 1429.000 ns: reporter [TOP] a=0, b=5
"""

GENERIC_LOG_SAMPLE = """\
Booting simulation
INFO test has started
timeout ERROR waiting for resp @ 45 ns
still running
"""

MISSING_TIME_LOG_SAMPLE = """\
Booting simulation
plain ERROR missing time token
"""

CUSTOM_ERROR_LOG_SAMPLE = """\
Booting simulation
timeout ERROR waiting for resp @ 45 ns
"""

MIXED_LOG_SAMPLE = """\
xrun(64)
xmvlog: *E,SYNTAX (/tmp/dut.sv,27): syntax error near 'endmodule'
xmelab: elaborating design
UVM_ERROR /tmp/top_tb.sv(129) @ 1429.000 ns: scoreboard [SCOREBOARD] expected=0x5a, actual=0x58 txn_id=84
"""

UVM_DOTTED_REPORTER_LOG_SAMPLE = """\
UVM_ERROR /tmp/top_tb.sv(129) @ 1429.000 ns: uvm_test_top.env.scb [SCOREBOARD] compare failed
"""

UVM_MULTILINE_TABLE_LOG_SAMPLE = """\
UVM_ERROR /tmp/top_tb.sv(129) @ 1429.000 ns: uvm_test_top.env.scb [SCOREBOARD] packet compare failed
  the expect pkt is
  -------------------------------------------------------
  Name               Type            Size  Value
  -------------------------------------------------------
  uvm_sequence_item  my_transaction  -     @1422
    dmac             integral        48    'h55183781a6be
    smac             integral        48    'hf334b7d71d03
    ether_type       integral        16    'hc8b5
    pload            da(integral)    1385  -
    crc              integral        32    'hffffffff
  -------------------------------------------------------
  the actual pkt is
  -------------------------------------------------------
  Name               Type            Size  Value
  -------------------------------------------------------
  uvm_sequence_item  my_transaction  -     @1386
    dmac             integral        48    'h55183781a6bf
    smac             integral        48    'hf334b7d71d03
    ether_type       integral        16    'hc8b5
    pload            da(integral)    1385  -
    crc              integral        32    'hffffffff

UVM_FATAL /tmp/top_tb.sv(200) @ 1500.000 ns: reporter [TOP] stop
"""

UVM_CONTINUATION_BOUNDARY_LOG_SAMPLE = """\
UVM_ERROR /tmp/top_tb.sv(129) @ 100.000 ns: uvm_test_top.env.scb [SCOREBOARD] packet compare failed
  the expect pkt is
  -------------------------------------------------------
  Name               Type            Size  Value
  -------------------------------------------------------
    dmac             integral        48    'h55183781a6be
ERROR: unrelated runtime error @ 300 ns
"""

COMPILE_ONLY_MIXED_LOG_SAMPLE = """\
xrun(64)
xmvlog: *E,SYNTAX (/tmp/dut.sv,27): syntax error near 'endmodule'
xmelab: *E,CUVMUR: design unit not found
"""


def _write_log(content: str) -> str:
    handle = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False)
    handle.write(content)
    handle.close()
    return handle.name


class TestGroupedSummary:
    def setup_method(self):
        self.log_path = _write_log(VCS_LOG_SAMPLE)
        self.result = SimLogParser(self.log_path, "vcs").parse()

    def teardown_method(self):
        os.unlink(self.log_path)

    def test_total_counts(self):
        assert self.result["schema_version"] == "2.0"
        assert self.result["contract_version"] == "1.2"
        assert self.result["failure_events_schema_version"] == "1.0"
        assert "mixed_log_detection" in self.result["parser_capabilities"]
        assert self.result["runtime_total_errors"] == 6
        assert self.result["runtime_error_count"] == 5
        assert self.result["runtime_fatal_count"] == 1
        assert self.result["unique_types"] == 4

    def test_grouped_assertions(self):
        groups = {group["signature"]: group for group in self.result["groups"]}
        assert groups["ASSERTION_FAIL: apUNEXPECTED_ASSERTION"]["count"] == 2
        assert groups["ASSERTION_FAIL: apUNEXPECTED_ASSERTION"]["first_time_ps"] == 290000
        assert groups["ASSERTION_FAIL: apUNEXPECTED_ASSERTION"]["last_time_ps"] == 310000
        assert groups["ASSERTION_FAIL: apEXPECTED_ASSERTION_1"]["count"] == 1

    def test_grouped_uvm(self):
        groups = {group["signature"]: group for group in self.result["groups"]}
        assert groups["UVM_ERROR [TOP]"]["count"] == 2
        assert groups["UVM_ERROR [TOP]"]["first_time_ps"] == 1661000
        assert groups["UVM_FATAL [TOP]"]["severity"] == "FATAL"

    def test_first_error_line(self):
        assert self.result["first_error_line"] == 4

    def test_failure_events_are_normalized(self):
        events = SimLogParser(self.log_path, "vcs").parse_failure_events()
        assert len(events) == 6
        first = events[0]
        assert first["event_id"].startswith("failure-")
        assert first["group_signature"] == "ASSERTION_FAIL: apUNEXPECTED_ASSERTION"
        assert first["time_ps"] == 290000
        assert first["source_file"].endswith("sva_top.sv")
        assert first["source_line"] == 66
        assert first["instance_path"] == "top_tb.sva_top_inst.apUNEXPECTED_ASSERTION"
        assert first["structured_fields"]["assertion_name"] == "apUNEXPECTED_ASSERTION"
        assert first["raw_time"] == "290000"
        assert first["raw_time_unit"] == "ps"
        assert first["time_parse_status"] == "exact"
        assert first["log_phase"] == "runtime"
        assert first["failure_source"] == "assertion"
        assert first["failure_mechanism"] == "protocol"
        assert first["transaction_hint"] is None
        assert first["expected"] is None
        assert first["actual"] is None
        assert first["missing_fields"] == []
        assert first["field_provenance"]["log_phase"] == "derived"
        assert first["field_provenance"]["time_ps"] == "observed"
        assert first["field_provenance"]["source_file"] == "observed"
        assert first["field_provenance"]["source_line"] == "observed"
        assert first["field_provenance"]["instance_path"] == "observed"
        assert first["field_provenance"]["failure_source"] == "derived"
        assert first["field_provenance"]["failure_mechanism"] == "heuristic"


class TestXceliumSummary:
    def setup_method(self):
        self.log_path = _write_log(XCE_LOG_SAMPLE)
        self.result = SimLogParser(self.log_path, "xcelium").parse()

    def teardown_method(self):
        os.unlink(self.log_path)

    def test_assertion_times(self):
        groups = {group["signature"]: group for group in self.result["groups"]}
        assert groups["ASSERTION_FAIL: apUNEXPECTED_ASSERTION"]["first_time_ps"] == 270000
        assert groups["ASSERTION_FAIL: apEXPECTED_ASSERTION_1"]["first_time_ps"] == 290000

    def test_uvm_time(self):
        groups = {group["signature"]: group for group in self.result["groups"]}
        assert groups["UVM_ERROR [TOP]"]["first_time_ps"] == 1429000


class TestUvmParsing:
    def test_uvm_reporter_allows_dotted_paths(self):
        log_path = _write_log(UVM_DOTTED_REPORTER_LOG_SAMPLE)
        try:
            event = SimLogParser(log_path, "vcs").parse_failure_events()[0]
            assert event["group_signature"] == "UVM_ERROR [SCOREBOARD]"
            assert event["instance_path"] == "uvm_test_top.env.scb"
            assert event["failure_source"] == "scoreboard"
        finally:
            os.unlink(log_path)

    def test_uvm_multiline_table_is_extracted(self):
        log_path = _write_log(UVM_MULTILINE_TABLE_LOG_SAMPLE)
        try:
            events = SimLogParser(log_path, "vcs").parse_failure_events()
            assert len(events) == 2
            first = events[0]
            assert first["group_signature"] == "UVM_ERROR [SCOREBOARD]"
            assert first["expected"] == "dmac='h55183781a6be"
            assert first["actual"] == "dmac='h55183781a6bf"
            assert first["failure_mechanism"] == "mismatch"
        finally:
            os.unlink(log_path)

    def test_uvm_continuation_stops_before_non_indented_runtime_error(self):
        log_path = _write_log(UVM_CONTINUATION_BOUNDARY_LOG_SAMPLE)
        try:
            events = SimLogParser(log_path, "vcs").parse_failure_events()
            assert len(events) == 2
            assert events[0]["group_signature"] == "UVM_ERROR [SCOREBOARD]"
            assert events[1]["group_signature"].startswith("ERROR: ERROR: unrelated runtime error")
            assert events[1]["time_ps"] == 300000
        finally:
            os.unlink(log_path)


class TestGenericErrorFallback:
    def setup_method(self):
        self.log_path = _write_log(GENERIC_LOG_SAMPLE)
        self.result = SimLogParser(self.log_path, "vcs").parse()

    def teardown_method(self):
        os.unlink(self.log_path)

    def test_generic_error_group(self):
        assert self.result["runtime_total_errors"] == 1
        group = self.result["groups"][0]
        assert group["signature"].startswith("ERROR: timeout ERROR waiting for resp")
        assert group["first_time_ps"] == 45000

    def test_supported_time_patterns(self, monkeypatch):
        monkeypatch.setattr(log_parser_module, "CUSTOM_PATTERNS_FILE", "/tmp/does_not_exist.yaml")
        log_path = _write_log(
            "\n".join(
                [
                    "ERROR: @23100000 bare_at_form",
                    "checker ERROR [23100ns] bracket_form",
                    "module ERROR time=23100000 inferred_ticks",
                ]
            )
            + "\n"
        )
        try:
            events = SimLogParser(log_path, "vcs").parse_failure_events()
            assert events[0]["time_ps"] == 23100000
            assert events[0]["raw_time_unit"] == "ps"
            assert events[0]["time_parse_status"] == "exact"
            assert events[1]["time_ps"] == 23100000
            assert events[1]["raw_time_unit"] == "ns"
            assert events[2]["time_ps"] == 23100000
            assert events[2]["raw_time_unit"] == "ticks"
            assert events[2]["time_parse_status"] == "inferred"
        finally:
            os.unlink(log_path)

    def test_missing_time_stays_null(self):
        log_path = _write_log(MISSING_TIME_LOG_SAMPLE)
        try:
            event = SimLogParser(log_path, "vcs").parse_failure_events()[0]
            assert event["time_ps"] is None
            assert event["raw_time"] is None
            assert event["raw_time_unit"] is None
            assert event["time_parse_status"] == "missing"
            assert "time_ps" in event["missing_fields"]
            assert "time_ps" not in event["field_provenance"]
            assert SimLogParser(log_path, "vcs").parse()["groups"][0]["first_time_ps"] is None
        finally:
            os.unlink(log_path)


class TestCustomPatterns:
    def test_custom_pattern_overrides_generic_error(self, monkeypatch):
        custom_patterns = Path(tempfile.mkdtemp()) / "custom_patterns.yaml"
        custom_patterns.write_text(
            "\n".join(
                [
                    "patterns:",
                    "  - name: timeout_wait",
                    "    severity: ERROR",
                    "    regex: 'timeout ERROR waiting for resp @ (?P<time>[\\d.]+) (?P<time_unit>ns|ps)'",
                    "    description: custom timeout matcher",
                ]
            )
            + "\n"
        )

        log_path = _write_log(CUSTOM_ERROR_LOG_SAMPLE)
        monkeypatch.setattr(log_parser_module, "CUSTOM_PATTERNS_FILE", str(custom_patterns))

        try:
            result = SimLogParser(log_path, "vcs").parse()
            group = result["groups"][0]
            assert group["signature"] == "CUSTOM: timeout_wait"
            assert group["first_time_ps"] == 45000
        finally:
            os.unlink(log_path)
            custom_patterns.unlink()
            custom_patterns.parent.rmdir()

    def test_custom_pattern_builds_failure_event(self, monkeypatch):
        custom_patterns = Path(tempfile.mkdtemp()) / "custom_patterns.yaml"
        custom_patterns.write_text(
            "\n".join(
                [
                    "patterns:",
                    "  - name: sb_compare",
                    "    severity: ERROR",
                    "    regex: 'SB_FAIL src=(?P<source_file>[^ ]+) line=(?P<source_line>\\d+) inst=(?P<instance_path>[^ ]+) sig=(?P<signal>\\w+) @ (?P<time>[\\d.]+) (?P<time_unit>ns)'",
                ]
            )
            + "\n"
        )
        log_path = _write_log("SB_FAIL src=/tmp/tb.sv line=42 inst=top_tb.dut sig=data @ 15 ns\n")
        monkeypatch.setattr(log_parser_module, "CUSTOM_PATTERNS_FILE", str(custom_patterns))
        try:
            event = SimLogParser(log_path, "vcs").parse_failure_events()[0]
            assert event["source_file"] == "/tmp/tb.sv"
            assert event["source_line"] == 42
            assert event["instance_path"] == "top_tb.dut"
            assert event["structured_fields"]["signal"] == "data"
        finally:
            os.unlink(log_path)
            custom_patterns.unlink()
            custom_patterns.parent.rmdir()

    def test_partial_custom_pattern_reports_missing_fields_and_provenance(self, monkeypatch):
        custom_patterns = Path(tempfile.mkdtemp()) / "custom_patterns.yaml"
        custom_patterns.write_text(
            "\n".join(
                [
                    "patterns:",
                    "  - name: partial_checker",
                    "    severity: ERROR",
                    "    regex: 'CHK_FAIL src=(?P<source_file>[^ ]+) inst=(?P<instance_path>[^ ]+) message=(?P<message>.+)'",
                ]
            )
            + "\n"
        )
        log_path = _write_log("CHK_FAIL src=/tmp/tb.sv inst=top_tb.env.chk message=checker fired without timestamp\n")
        monkeypatch.setattr(log_parser_module, "CUSTOM_PATTERNS_FILE", str(custom_patterns))
        try:
            event = SimLogParser(log_path, "vcs").parse_failure_events()[0]
            assert "missing_fields" in event
            assert "field_provenance" in event
            assert set(event["missing_fields"]) >= {"time_ps", "source_line"}
            assert event["field_provenance"]["source_file"] == "observed"
            assert event["field_provenance"]["instance_path"] == "observed"
            assert event["field_provenance"]["failure_source"] == "derived"
            assert "source_line" not in event["field_provenance"]
        finally:
            os.unlink(log_path)
            custom_patterns.unlink()
            custom_patterns.parent.rmdir()

    def test_custom_pattern_falls_back_to_generic_time_extraction(self, monkeypatch):
        custom_patterns = Path(tempfile.mkdtemp()) / "custom_patterns.yaml"
        custom_patterns.write_text(
            "\n".join(
                [
                    "patterns:",
                    "  - name: phase_checker",
                    "    severity: ERROR",
                    "    regex: 'PHASE_FAIL src=(?P<source_file>[^ ]+) message=(?P<message>.+)'",
                ]
            )
            + "\n"
        )
        log_path = _write_log("PHASE_FAIL src=/tmp/tb.sv message=phase mismatch @7300000\n")
        monkeypatch.setattr(log_parser_module, "CUSTOM_PATTERNS_FILE", str(custom_patterns))
        try:
            event = SimLogParser(log_path, "vcs").parse_failure_events()[0]
            assert event["time_ps"] == 7300000
            assert event["raw_time"] == "7300000"
            assert event["raw_time_unit"] == "ps"
            assert event["time_parse_status"] == "exact"
        finally:
            os.unlink(log_path)
            custom_patterns.unlink()
            custom_patterns.parent.rmdir()

    def test_custom_pattern_compile_diagnostic_is_filtered_in_mixed_log(self, monkeypatch):
        custom_patterns = Path(tempfile.mkdtemp()) / "custom_patterns.yaml"
        custom_patterns.write_text(
            "\n".join(
                [
                    "patterns:",
                    "  - name: compile_syntax",
                    "    severity: ERROR",
                    "    regex: 'xmvlog:\\s+\\*E,SYNTAX\\s+\\((?P<source_file>[^,]+),(?P<source_line>\\d+)\\):\\s+(?P<message>.+)'",
                ]
            )
            + "\n"
        )
        log_path = _write_log(MIXED_LOG_SAMPLE)
        monkeypatch.setattr(log_parser_module, "CUSTOM_PATTERNS_FILE", str(custom_patterns))
        try:
            result = SimLogParser(log_path, "xcelium").parse()
            events = SimLogParser(log_path, "xcelium").parse_failure_events()
            assert result["runtime_total_errors"] == 1
            assert len(events) == 1
            assert events[0]["group_signature"] == "UVM_ERROR [SCOREBOARD]"
        finally:
            os.unlink(log_path)
            custom_patterns.unlink()
            custom_patterns.parent.rmdir()


class TestGroupTruncation:
    def setup_method(self):
        lines = ["Booting simulation"]
        for i in range(60):
            lines.append(f"module_{i} ERROR unique issue {i} @ {i + 1} ns")
        self.log_path = _write_log("\n".join(lines) + "\n")

    def teardown_method(self):
        os.unlink(self.log_path)

    def test_parse_truncates_groups(self):
        result = SimLogParser(self.log_path, "vcs").parse(max_groups=5)

        assert result["runtime_total_errors"] == 60
        assert result["unique_types"] == 60
        assert result["total_groups"] == 60
        assert result["truncated"] is True
        assert result["max_groups"] == 5
        assert len(result["groups"]) == 5

    def test_extracts_phase2_runtime_fields(self):
        log_path = _write_log(
            "UVM_ERROR /tmp/top_tb.sv(99) @ 12 ns: scoreboard [SB] expected=0x3, actual=0x7 txn_id=wr42\n"
        )
        try:
            event = SimLogParser(log_path, "vcs").parse_failure_events()[0]
            assert event["log_phase"] == "runtime"
            assert event["failure_source"] == "scoreboard"
            assert event["failure_mechanism"] == "mismatch"
            assert event["expected"] == "0x3"
            assert event["actual"] == "0x7"
            assert event["transaction_hint"] == "wr42"
        finally:
            os.unlink(log_path)

    def test_expected_actual_falls_back_per_field(self):
        log_path = _write_log(
            "module ERROR compare failed expected=0x3 got=0x7 @ 12 ns\n"
        )
        try:
            original = log_parser_module._extract_structured_fields

            def partial_structured_fields(_line):
                return {"expected": "0x3"}

            log_parser_module._extract_structured_fields = partial_structured_fields
            event = SimLogParser(log_path, "vcs").parse_failure_events()[0]
            assert event["expected"] == "0x3"
            assert event["actual"] == "0x7"
            assert event["failure_mechanism"] == "mismatch"
        finally:
            log_parser_module._extract_structured_fields = original
            os.unlink(log_path)


class TestRerunHints:
    def test_parse_summary_reports_previous_logs(self, tmp_path):
        older = tmp_path / "run_prev.log"
        current = tmp_path / "run.log"
        older.write_text("module_a ERROR previous @ 1 ns\n")
        current.write_text("module_b ERROR current @ 2 ns\n")
        os.utime(older, (older.stat().st_atime, older.stat().st_mtime - 10))

        result = SimLogParser(str(current), "vcs").parse()

        assert result["previous_log_detected"] is True
        assert str(older.resolve()) in result["candidate_previous_logs"]
        assert result["suggested_followup_tool"] == "diff_sim_failure_results"


class TestMixedLogRuntimeSafety:
    def test_mixed_log_ignores_compile_errors_and_keeps_runtime_counts(self):
        log_path = _write_log(MIXED_LOG_SAMPLE)
        try:
            result = SimLogParser(log_path, "xcelium").parse()
            events = SimLogParser(log_path, "xcelium").parse_failure_events()
            assert result["runtime_total_errors"] == 1
            assert result["runtime_error_count"] == 1
            assert result["runtime_fatal_count"] == 0
            assert result["total_groups"] == 1
            assert len(events) == 1
            assert events[0]["group_signature"] == "UVM_ERROR [SCOREBOARD]"
            assert events[0]["expected"] == "0x5a"
            assert events[0]["actual"] == "0x58"
            assert events[0]["transaction_hint"] == "84"
        finally:
            os.unlink(log_path)

    def test_compile_elab_only_mixed_log_returns_zero_runtime_events(self):
        log_path = _write_log(COMPILE_ONLY_MIXED_LOG_SAMPLE)
        try:
            result = SimLogParser(log_path, "xcelium").parse()
            events = SimLogParser(log_path, "xcelium").parse_failure_events()
            assert result["runtime_total_errors"] == 0
            assert result["runtime_error_count"] == 0
            assert result["runtime_fatal_count"] == 0
            assert result["total_groups"] == 0
            assert events == []
        finally:
            os.unlink(log_path)


class TestGetErrorContext:
    def setup_method(self):
        self.log_path = _write_log(VCS_LOG_SAMPLE)

    def teardown_method(self):
        os.unlink(self.log_path)

    def test_context_window(self):
        context = get_error_context(self.log_path, line=4, before=1, after=2)
        assert context["center_line"] == 4
        assert context["start_line"] == 3
        assert context["end_line"] == 6
        assert "apUNEXPECTED_ASSERTION" in context["context"]
        assert "Offending '(s_bits == STATE1)'" in context["context"]
        assert "apEXPECTED_ASSERTION_1" in context["context"]

    def test_context_out_of_range(self):
        with pytest.raises(ValueError):
            get_error_context(self.log_path, line=999, before=1, after=1)


class TestFailureEventDiff:
    def test_diff_detects_resolved_persistent_and_new(self):
        base_log = _write_log(
            "\n".join(
                [
                    '"/path/a.sv", 10: top_tb.dut.apA: started at 10ns failed at 12ns',
                    "UVM_ERROR /path/top_tb.sv(125) @ 20 ns: reporter [TOP] mismatch a=1, b=0",
                ]
            )
            + "\n"
        )
        new_log = _write_log(
            "\n".join(
                [
                    '"/path/a.sv", 10: top_tb.dut.apA: started at 10ns failed at 14ns',
                    "module_c ERROR unique issue c @ 3 ns",
                ]
            )
            + "\n"
        )
        try:
            diff = SimLogParser(base_log, "vcs").diff_against(new_log)
            assert diff["base_summary"]["total_events"] == 2
            assert diff["new_summary"]["total_events"] == 2
            assert len(diff["persistent_events"]) == 1
            assert len(diff["resolved_events"]) == 1
            assert len(diff["new_events"]) == 1
            assert diff["persistent_events"][0]["time_shift_ps"] == 2000
            assert diff["persistent_events"][0]["time_direction"] == "later"
            assert diff["persistent_events"][0]["mechanism_changed"] is False
            assert diff["persistent_events"][0]["x_to_deterministic"] is False
            assert diff["persistent_events"][0]["value_changed"] is False
            assert diff["problem_hints_comparison"]["first_error_time_shift_ps"] == -9000
            assert diff["convergence_summary"] is not None
        finally:
            os.unlink(base_log)
            os.unlink(new_log)

    def test_diff_x_resolved(self):
        base_log = _write_log(
            "UVM_ERROR /path/top_tb.sv(125) @ 20 ns: reporter [TOP] xprop detected on bus\n"
        )
        new_log = _write_log(
            "UVM_ERROR /path/top_tb.sv(125) @ 20 ns: reporter [TOP] expected=0x1234, actual=0x5678\n"
        )
        try:
            diff = SimLogParser(base_log, "vcs").diff_against(new_log)
            persistent = diff["persistent_events"][0]
            assert diff["problem_hints_comparison"]["x_resolved"] is True
            assert persistent["x_to_deterministic"] is True
            assert "X propagation resolved" in diff["convergence_summary"]
        finally:
            os.unlink(base_log)
            os.unlink(new_log)

    def test_diff_x_introduced(self):
        base_log = _write_log(
            "UVM_ERROR /path/top_tb.sv(125) @ 20 ns: reporter [TOP] expected=0x1234, actual=0x5678\n"
        )
        new_log = _write_log(
            "UVM_ERROR /path/top_tb.sv(125) @ 20 ns: reporter [TOP] xprop detected on bus\n"
        )
        try:
            diff = SimLogParser(base_log, "vcs").diff_against(new_log)
            assert diff["problem_hints_comparison"]["x_introduced"] is True
            assert "X propagation introduced" in diff["convergence_summary"]
        finally:
            os.unlink(base_log)
            os.unlink(new_log)

    def test_diff_first_error_time_shift(self):
        base_log = _write_log(
            "UVM_ERROR /path/top_tb.sv(125) @ 1000 ns: reporter [TOP] expected=0x1, actual=0x0\n"
        )
        new_log = _write_log(
            "UVM_ERROR /path/top_tb.sv(125) @ 2000 ns: reporter [TOP] expected=0x1, actual=0x0\n"
        )
        try:
            diff = SimLogParser(base_log, "vcs").diff_against(new_log)
            assert diff["problem_hints_comparison"]["first_error_time_shift_ps"] == 1_000_000
            assert diff["problem_hints_comparison"]["first_error_time_direction"] == "later"
        finally:
            os.unlink(base_log)
            os.unlink(new_log)

    def test_diff_mechanism_transition(self):
        base_log = _write_log(
            "UVM_ERROR /path/top_tb.sv(125) @ 20 ns: reporter [TOP] xprop detected on bus\n"
        )
        new_log = _write_log(
            "UVM_ERROR /path/top_tb.sv(125) @ 20 ns: reporter [TOP] expected=0x1234, actual=0x5678\n"
        )
        try:
            diff = SimLogParser(base_log, "vcs").diff_against(new_log)
            persistent = diff["persistent_events"][0]
            assert persistent["mechanism_changed"] is True
            assert persistent["mechanism_transition"] == "xprop → mismatch"
        finally:
            os.unlink(base_log)
            os.unlink(new_log)

    def test_diff_convergence_summary_all_resolved(self):
        base_log = _write_log(
            "\n".join(
                [
                    "module_a ERROR unique issue a @ 1 ns",
                    "module_b ERROR unique issue b @ 2 ns",
                    "module_c ERROR unique issue c @ 3 ns",
                ]
            )
            + "\n"
        )
        new_log = _write_log("Booting simulation\n")
        try:
            diff = SimLogParser(base_log, "vcs").diff_against(new_log)
            assert len(diff["resolved_events"]) == 3
            assert "resolved" in diff["convergence_summary"]
        finally:
            os.unlink(base_log)
            os.unlink(new_log)

    def test_diff_time_shift_is_signed(self):
        base_log = _write_log(
            "UVM_ERROR /path/top_tb.sv(125) @ 2000 ns: reporter [TOP] expected=0x1, actual=0x0\n"
        )
        new_log = _write_log(
            "UVM_ERROR /path/top_tb.sv(125) @ 1000 ns: reporter [TOP] expected=0x1, actual=0x0\n"
        )
        try:
            diff = SimLogParser(base_log, "vcs").diff_against(new_log)
            persistent = diff["persistent_events"][0]
            assert persistent["time_shift_ps"] < 0
            assert persistent["time_direction"] == "earlier"
        finally:
            os.unlink(base_log)
            os.unlink(new_log)

    def test_diff_keeps_zero_ps_time_shift(self):
        base_log = _write_log(
            "UVM_ERROR /path/top_tb.sv(125) @ 0 ps: reporter [TOP] expected=0x1, actual=0x0\n"
        )
        new_log = _write_log(
            "UVM_ERROR /path/top_tb.sv(125) @ 1000 ps: reporter [TOP] expected=0x1, actual=0x0\n"
        )
        try:
            diff = SimLogParser(base_log, "vcs").diff_against(new_log)
            persistent = diff["persistent_events"][0]
            assert persistent["time_shift_ps"] == 1000
            assert persistent["time_direction"] == "later"
            assert diff["problem_hints_comparison"]["first_error_time_shift_ps"] == 1000
            assert diff["problem_hints_comparison"]["first_error_time_direction"] == "later"
        finally:
            os.unlink(base_log)
            os.unlink(new_log)


class TestProblemHintsUnknownValueDetection:
    def test_identifier_values_do_not_trigger_x_or_z(self):
        event = {
            "message_text": "compare failed",
            "group_signature": "UVM_ERROR [TOP]",
            "instance_path": "uvm_test_top.env.scb",
            "failure_mechanism": "mismatch",
            "expected": "TX_IDLE",
            "actual": "TX_BUSY",
            "structured_fields": {},
        }
        assert event_has_x_or_z(event) == (False, False)

    def test_literal_unknown_values_still_trigger_x_detection(self):
        event = {
            "message_text": "compare failed",
            "group_signature": "UVM_ERROR [TOP]",
            "instance_path": "uvm_test_top.env.scb",
            "failure_mechanism": "mismatch",
            "expected": "xx",
            "actual": "12",
            "structured_fields": {},
        }
        assert event_has_x_or_z(event) == (True, False)

    def test_compute_problem_hints_keeps_zero_ps_first_error(self):
        hints = compute_problem_hints_from_events(
            [
                {
                    "message_text": "error at reset",
                    "group_signature": "UVM_ERROR [TOP]",
                    "instance_path": "reporter",
                    "failure_mechanism": "timeout",
                    "expected": None,
                    "actual": None,
                    "structured_fields": {},
                    "time_ps": 0,
                },
                {
                    "message_text": "later error",
                    "group_signature": "UVM_ERROR [TOP]",
                    "instance_path": "reporter",
                    "failure_mechanism": "timeout",
                    "expected": None,
                    "actual": None,
                    "structured_fields": {},
                    "time_ps": 1000,
                },
            ]
        )
        assert hints["first_error_time_ps"] == 0


REAL_LOG = "/home/robin/Projects/mcp_demo/tb/work/work_my_case0/run.log"


@pytest.mark.skipif(not os.path.exists(REAL_LOG), reason="真实 log 文件不存在，跳过")
class TestRealLog:
    def setup_method(self):
        self.result = SimLogParser(REAL_LOG, "vcs").parse()

    def test_has_errors(self):
        assert self.result["runtime_total_errors"] > 0

    def test_has_groups(self):
        assert len(self.result["groups"]) > 0

    def test_summary_fields_exist(self):
        for field in [
            "log_file",
            "simulator",
            "schema_version",
            "contract_version",
            "failure_events_schema_version",
            "parser_capabilities",
            "runtime_total_errors",
            "runtime_fatal_count",
            "runtime_error_count",
            "unique_types",
            "total_groups",
            "truncated",
            "max_groups",
            "first_error_line",
            "groups",
        ]:
            assert field in self.result
