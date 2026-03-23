import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from src.structural_scanner import scan_structural_risks


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "structural"


def _compile_result_for(*names: str) -> dict:
    return {
        "files": {
            "user": [{"path": str(FIXTURE_DIR / name), "type": "module", "category": "rtl"} for name in names]
        }
    }


class TestStructuralScanner:
    def test_detects_slice_overlap_and_gap(self, monkeypatch):
        monkeypatch.setattr(
            "src.structural_scanner.parse_compile_log",
            lambda compile_log, simulator: _compile_result_for("crp_buggy.v"),
        )

        result = scan_structural_risks("/tmp/compile.log", "vcs", categories=["slice_overlap"])

        assert result["files_scanned"] == 1
        assert result["total_risks"] == 1
        risk = result["risks"][0]
        assert risk["type"] == "slice_overlap"
        assert risk["risk_level"] == "high"
        assert "overlap at bit 9" in risk["detail"]
        assert "gap at bit 5" in risk["detail"]

    def test_fixed_slice_layout_does_not_report(self, monkeypatch):
        monkeypatch.setattr(
            "src.structural_scanner.parse_compile_log",
            lambda compile_log, simulator: _compile_result_for("crp_fixed.v"),
        )

        result = scan_structural_risks("/tmp/compile.log", "vcs", categories=["slice_overlap"])

        assert result["total_risks"] == 0

    def test_detects_narrow_condition_injection(self, monkeypatch):
        monkeypatch.setattr(
            "src.structural_scanner.parse_compile_log",
            lambda compile_log, simulator: _compile_result_for("des_backdoor.v"),
        )

        result = scan_structural_risks("/tmp/compile.log", "vcs", categories=["narrow_condition_injection"])

        assert result["total_risks"] == 1
        risk = result["risks"][0]
        assert risk["type"] == "narrow_condition_injection"
        assert "{31'b0, (roundSel == 4'hd) & decrypt & (L[1:4] == 4'hA)}" in risk["evidence"][0]
        assert "total_width=unknown" in risk["evidence"][1]

    def test_reports_when_total_width_unknown(self, monkeypatch):
        monkeypatch.setattr(
            "src.structural_scanner.parse_compile_log",
            lambda compile_log, simulator: _compile_result_for("narrow_no_width.v"),
        )

        result = scan_structural_risks("/tmp/compile.log", "vcs", categories=["narrow_condition_injection"])

        assert result["total_risks"] == 1
        assert result["risks"][0]["line"] == 6

    def test_detects_multiline_assign_narrow_injection(self, monkeypatch):
        monkeypatch.setattr(
            "src.structural_scanner.parse_compile_log",
            lambda compile_log, simulator: _compile_result_for("des_backdoor_multiline.v"),
        )

        result = scan_structural_risks("/tmp/compile.log", "vcs", categories=["narrow_condition_injection"])

        assert result["total_risks"] == 1
        assert result["risks"][0]["type"] == "narrow_condition_injection"
        assert result["risks"][0]["line"] == 8

    def test_detects_always_comb_expr_prefix_narrow_injection(self, monkeypatch):
        monkeypatch.setattr(
            "src.structural_scanner.parse_compile_log",
            lambda compile_log, simulator: _compile_result_for("always_comb_expr.v"),
        )

        result = scan_structural_risks("/tmp/compile.log", "vcs", categories=["narrow_condition_injection"])

        assert result["total_risks"] == 1
        assert result["risks"][0]["type"] == "narrow_condition_injection"
        assert result["risks"][0]["line"] == 7

    def test_detects_always_ff_nonblocking_narrow_injection(self, monkeypatch):
        monkeypatch.setattr(
            "src.structural_scanner.parse_compile_log",
            lambda compile_log, simulator: _compile_result_for("always_ff_expr.v"),
        )

        result = scan_structural_risks("/tmp/compile.log", "vcs", categories=["narrow_condition_injection"])

        assert result["total_risks"] == 1
        assert result["risks"][0]["type"] == "narrow_condition_injection"
        assert result["risks"][0]["line"] == 8

    def test_detects_narrow_injection_in_long_block(self, monkeypatch):
        monkeypatch.setattr(
            "src.structural_scanner.parse_compile_log",
            lambda compile_log, simulator: _compile_result_for("long_block_narrow.v"),
        )

        result = scan_structural_risks("/tmp/compile.log", "vcs", categories=["narrow_condition_injection"])

        assert result["total_risks"] == 1
        assert result["risks"][0]["type"] == "narrow_condition_injection"
        assert result["risks"][0]["line"] == 47

    def test_detects_multi_drive_and_incomplete_case(self, monkeypatch):
        monkeypatch.setattr(
            "src.structural_scanner.parse_compile_log",
            lambda compile_log, simulator: _compile_result_for("multi_drive_and_case.v"),
        )

        result = scan_structural_risks("/tmp/compile.log", "vcs")
        risk_types = {risk["type"] for risk in result["risks"]}

        assert "multi_drive" in risk_types
        assert "incomplete_case" in risk_types

    def test_detects_magic_condition_but_skips_case_item(self, monkeypatch):
        monkeypatch.setattr(
            "src.structural_scanner.parse_compile_log",
            lambda compile_log, simulator: _compile_result_for("magic_case_skip.v"),
        )

        result = scan_structural_risks("/tmp/compile.log", "vcs", categories=["magic_condition"])

        assert result["total_risks"] == 1
        risk = result["risks"][0]
        assert risk["type"] == "magic_condition"
        assert risk["line"] == 12

    def test_skips_missing_files(self, monkeypatch):
        compile_result = _compile_result_for("des_clean.v")
        compile_result["files"]["user"].append(
            {"path": str(FIXTURE_DIR / "missing_file.v"), "type": "module", "category": "rtl"}
        )
        monkeypatch.setattr(
            "src.structural_scanner.parse_compile_log",
            lambda compile_log, simulator: compile_result,
        )

        result = scan_structural_risks("/tmp/compile.log", "vcs", categories=["magic_condition"])

        assert result["files_scanned"] == 1
        assert len(result["skipped_files"]) == 1

    def test_rejects_unknown_category(self):
        with pytest.raises(ValueError, match="未知 categories"):
            scan_structural_risks("/tmp/compile.log", "vcs", categories=["not_real"])
