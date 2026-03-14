"""
test_log_parser.py
覆盖：两阶段 log 解析、分组摘要、通用 error 捕获、上下文提取
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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
            "first_error_line",
            "groups",
        ]:
            assert field in self.result
