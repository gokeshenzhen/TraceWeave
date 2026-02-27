"""
test_log_parser.py
用真实 log 片段测试 log 解析器
覆盖：VCS assertion fail、UVM_ERROR、聚合统计
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import tempfile
from src.log_parser import SimLogParser


# ── 真实 log 片段（直接从 run.log 摘取）─────────────────────────────

VCS_LOG_SAMPLE = """\
Command: /home/robin/Projects/mcp_demo/tb/../tb/work/simv +UVM_TESTNAME=my_case0
Chronologic VCS simulator copyright 1991-2018
Compiler version O-2018.09-SP2-11_Full64; Runtime version O-2018.09-SP2-11_Full64;

"/home/robin/Projects/mcp_demo/tb/../tb/sva_top.sv", 66: top_tb.sva_top_inst.apUNEXPECTED_ASSERTION: started at 270000ps failed at 290000ps
    Offending '(s_bits == STATE1)'
"/home/robin/Projects/mcp_demo/tb/../tb/sva_top.sv", 79: top_tb.sva_top_inst.apEXPECTED_ASSERTION_1: started at 270000ps failed at 290000ps
    Offending '(s_bits == STATE2)'
"/home/robin/Projects/mcp_demo/tb/../tb/sva_top.sv", 66: top_tb.sva_top_inst.apUNEXPECTED_ASSERTION: started at 290000ps failed at 310000ps
    Offending '(s_bits == STATE1)'
UVM_ERROR /home/robin/Projects/mcp_demo/tb/../tb/top_tb.sv(125) @ 1661.000 ns: reporter [TOP] a=1, b=0
UVM_ERROR /home/robin/Projects/mcp_demo/tb/../tb/top_tb.sv(125) @ 2128.000 ns: reporter [TOP] a=1, b=3
UVM_ERROR /home/robin/Projects/mcp_demo/tb/../tb/top_tb.sv(125) @ 2337.000 ns: reporter [TOP] a=1, b=9
$finish called from file "top_tb.sv", line 200.
$finish at simulation time             5800000000ps
UVM_ERROR :   15
UVM_FATAL :    0
"""


def _write_log(content: str) -> str:
    """把 log 内容写到临时文件，返回路径"""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False)
    f.write(content)
    f.close()
    return f.name


# ═══════════════════════════════════════════════════════════════════
# VCS assertion fail 解析
# ═══════════════════════════════════════════════════════════════════

class TestVCSAssertionFail:

    def setup_method(self):
        self.log_path = _write_log(VCS_LOG_SAMPLE)
        self.result   = SimLogParser(self.log_path, "vcs").parse()

    def teardown_method(self):
        os.unlink(self.log_path)

    def test_total_errors(self):
        """应解析到 assertion fail + UVM_ERROR，共 5 条"""
        assert self.result["total_errors"] == 6

    def test_simulator_detected(self):
        assert self.result["simulator"] == "vcs"

    def test_first_assertion_name(self):
        assertions = [e for e in self.result["errors"]
                      if e["error_type"] == "ASSERTION_FAIL"]
        names = [e["assertion_name"] for e in assertions]
        assert "apUNEXPECTED_ASSERTION" in names
        assert "apEXPECTED_ASSERTION_1" in names

    def test_assertion_time_ps(self):
        """第一个 apUNEXPECTED_ASSERTION fail 时刻应为 290000ps"""
        first = next(e for e in self.result["errors"]
                     if e["assertion_name"] == "apUNEXPECTED_ASSERTION")
        assert first["fail_time_ps"] == 290000
        assert first["start_time_ps"] == 270000

    def test_offending_expr(self):
        """应解析出 Offending 表达式"""
        first = next(e for e in self.result["errors"]
                     if e["assertion_name"] == "apUNEXPECTED_ASSERTION")
        assert "STATE1" in first["offending_expr"]

    def test_assertion_file_and_line(self):
        first = next(e for e in self.result["errors"]
                     if e["assertion_name"] == "apUNEXPECTED_ASSERTION")
        assert "sva_top.sv" in first["sva_file"]
        assert first["line_number"] == 66

    def test_sorted_by_time(self):
        """报错应按时间升序排列"""
        times = [e["fail_time_ps"] for e in self.result["errors"]]
        assert times == sorted(times)


# ═══════════════════════════════════════════════════════════════════
# UVM_ERROR 解析
# ═══════════════════════════════════════════════════════════════════

class TestUVMError:

    def setup_method(self):
        self.log_path = _write_log(VCS_LOG_SAMPLE)
        self.result   = SimLogParser(self.log_path, "vcs").parse()
        self.uvm_errors = [e for e in self.result["errors"]
                           if e["error_type"] == "UVM_ERROR"]

    def teardown_method(self):
        os.unlink(self.log_path)

    def test_uvm_error_count(self):
        assert len(self.uvm_errors) == 3

    def test_uvm_tag(self):
        assert all(e["uvm_tag"] == "TOP" for e in self.uvm_errors)

    def test_uvm_time_conversion(self):
        """1661.000 ns → 1661000000 ps"""
        first = next(e for e in self.uvm_errors
                     if "a=1, b=0" in e["message"])
        assert first["fail_time_ps"] == 1661000

    def test_uvm_file_and_line(self):
        first = self.uvm_errors[0]
        assert "top_tb.sv" in first["sva_file"]
        assert first["line_number"] == 125

    def test_uvm_severity(self):
        assert all(e["severity"] == "ERROR" for e in self.uvm_errors)


# ═══════════════════════════════════════════════════════════════════
# Xcelium assertion fail 解析（用构造的 log 测试格式兼容性）
# ═══════════════════════════════════════════════════════════════════

XCE_LOG_SAMPLE = """\
xmsim: *E,ASRTST (/path/sva_top.sv,66): (time 270 NS) Assertion top_tb.sva_top_inst.apUNEXPECTED_ASSERTION has failed (2 cycles, starting 250 NS)
    $rose(start) |=> s_bits == STATE1 ##1 s_bits == STATE2;
xmsim: *E,ASRTST (/path/sva_top.sv,79): (time 290 NS) Assertion top_tb.sva_top_inst.apEXPECTED_ASSERTION_1 has failed (2 cycles, starting 270 NS)
UVM_ERROR /path/top_tb.sv(129) @ 1429.000 ns: reporter [TOP] a=0, b=5
"""

class TestXceliumAssertionFail:

    def setup_method(self):
        self.log_path = _write_log(XCE_LOG_SAMPLE)
        self.result   = SimLogParser(self.log_path, "xcelium").parse()

    def teardown_method(self):
        os.unlink(self.log_path)

    def test_assertion_count(self):
        assertions = [e for e in self.result["errors"]
                      if e["error_type"] == "ASSERTION_FAIL"]
        assert len(assertions) == 2

    def test_xce_fail_time(self):
        """270 NS → 270000 ps"""
        first = next(e for e in self.result["errors"]
                     if e["assertion_name"] == "apUNEXPECTED_ASSERTION")
        assert first["fail_time_ps"] == 270000

    def test_xce_start_time(self):
        """starting 250 NS → start_time_ps = 250000"""
        first = next(e for e in self.result["errors"]
                     if e["assertion_name"] == "apUNEXPECTED_ASSERTION")
        assert first["start_time_ps"] == 250000

    def test_xce_sva_code(self):
        first = next(e for e in self.result["errors"]
                     if e["assertion_name"] == "apUNEXPECTED_ASSERTION")
        assert "rose" in first["sva_code"] or "STATE1" in first["sva_code"]

    def test_xce_uvm_error(self):
        uvm = [e for e in self.result["errors"] if e["error_type"] == "UVM_ERROR"]
        assert len(uvm) == 1
        assert uvm[0]["fail_time_ps"] == 1429000


# ═══════════════════════════════════════════════════════════════════
# 真实 log 文件测试（使用 mcp_demo 项目的实际文件）
# ═══════════════════════════════════════════════════════════════════

REAL_LOG = "/home/robin/Projects/mcp_demo/tb/work/work_my_case0/run.log"

@pytest.mark.skipif(not os.path.exists(REAL_LOG),
                    reason="真实 log 文件不存在，跳过")
class TestRealLog:

    def setup_method(self):
        self.result = SimLogParser(REAL_LOG, "auto").parse()

    def test_simulator_is_vcs(self):
        assert self.result["simulator"] == "vcs"

    def test_has_errors(self):
        assert self.result["total_errors"] > 0

    def test_has_assertion_fails(self):
        assertions = [e for e in self.result["errors"]
                      if e["error_type"] == "ASSERTION_FAIL"]
        assert len(assertions) > 0

    def test_has_uvm_errors(self):
        uvm = [e for e in self.result["errors"]
               if e["error_type"] == "UVM_ERROR"]
        assert len(uvm) > 0

    def test_uvm_count_matches_log_summary(self):
        """log 末尾显示 UVM_ERROR: 15，解析结果应一致"""
        uvm_count = sum(1 for e in self.result["errors"]
                        if e["error_type"] == "UVM_ERROR")
        assert uvm_count == 15

    def test_errors_sorted_by_time(self):
        times = [e["fail_time_ps"] for e in self.result["errors"]]
        assert times == sorted(times)

    def test_first_error_has_required_fields(self):
        e = self.result["errors"][0]
        for field in ["error_type", "severity", "fail_time_ps",
                      "fail_time_ns", "sva_file", "line_number", "message"]:
            assert field in e, f"缺少字段: {field}"
