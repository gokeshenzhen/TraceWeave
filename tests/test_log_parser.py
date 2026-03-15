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

CUSTOM_ERROR_LOG_SAMPLE = """\
Booting simulation
timeout ERROR waiting for resp @ 45 ns
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
        assert self.result["total_errors"] == 6
        assert self.result["error_count"] == 5
        assert self.result["fatal_count"] == 1
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


class TestGenericErrorFallback:
    def setup_method(self):
        self.log_path = _write_log(GENERIC_LOG_SAMPLE)
        self.result = SimLogParser(self.log_path, "vcs").parse()

    def teardown_method(self):
        os.unlink(self.log_path)

    def test_generic_error_group(self):
        assert self.result["total_errors"] == 1
        group = self.result["groups"][0]
        assert group["signature"].startswith("ERROR: timeout ERROR waiting for resp")
        assert group["first_time_ps"] == 45000


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

        assert result["total_errors"] == 60
        assert result["unique_types"] == 60
        assert result["total_groups"] == 60
        assert result["truncated"] is True
        assert result["max_groups"] == 5
        assert len(result["groups"]) == 5


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
        finally:
            os.unlink(base_log)
            os.unlink(new_log)


REAL_LOG = "/home/robin/Projects/mcp_demo/tb/work/work_my_case0/run.log"


@pytest.mark.skipif(not os.path.exists(REAL_LOG), reason="真实 log 文件不存在，跳过")
class TestRealLog:
    def setup_method(self):
        self.result = SimLogParser(REAL_LOG, "vcs").parse()

    def test_has_errors(self):
        assert self.result["total_errors"] > 0

    def test_has_groups(self):
        assert len(self.result["groups"]) > 0

    def test_summary_fields_exist(self):
        for field in [
            "log_file",
            "simulator",
            "total_errors",
            "fatal_count",
            "error_count",
            "unique_types",
            "total_groups",
            "truncated",
            "max_groups",
            "first_error_line",
            "groups",
        ]:
            assert field in self.result
