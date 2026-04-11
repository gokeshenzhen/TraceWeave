import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import server
from src import schemas


@pytest.fixture(autouse=True)
def _reset_session_state():
    server.reset_session_state()
    yield
    server.reset_session_state()


def _make_log(lines: list[str]) -> str:
    handle = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False)
    handle.write("\n".join(lines) + "\n")
    handle.close()
    return handle.name


def test_parse_sim_log_default_summary_stays_under_budget():
    log_path = _make_log(
        [
            f"module_{idx} ERROR unique issue {idx} {'x' * 120} @ {idx + 1} ns"
            for idx in range(80)
        ]
    )
    try:
        result = server._handle_parse_sim_log({"log_path": log_path, "simulator": "vcs"})
        assert result.detail_level == "summary"
        assert result.payload_bytes is not None
        assert result.payload_bytes <= schemas.TOKEN_BUDGET_SOFT_LIMIT
        assert result.total_groups > result.max_groups
        assert result.truncated is True
    finally:
        Path(log_path).unlink()


def test_parse_sim_log_auto_downgrades_when_budget_is_tiny(monkeypatch):
    monkeypatch.setattr(schemas, "TOKEN_BUDGET_SOFT_LIMIT", 2500)
    log_path = _make_log(
        [
            f"module_{idx} ERROR unique issue {idx} {'y' * 320} @ {idx + 1} ns"
            for idx in range(40)
        ]
    )
    try:
        result = server._handle_parse_sim_log(
            {
                "log_path": log_path,
                "simulator": "vcs",
                "max_groups": 40,
            }
        )
        assert result.auto_downgraded is True
        assert result.payload_bytes is not None
        assert result.payload_bytes <= schemas.TOKEN_BUDGET_SOFT_LIMIT
    finally:
        Path(log_path).unlink()


@pytest.mark.anyio
async def test_scan_structural_risks_stays_under_budget_when_budget_is_tiny(monkeypatch):
    monkeypatch.setattr(schemas, "TOKEN_BUDGET_SOFT_LIMIT", 2500)

    oversized_risks = [
        {
            "type": "slice_overlap",
            "file": f"/tmp/rtl_{idx}.sv",
            "line": idx + 1,
            "module": f"mod_{idx}",
            "risk_level": "high",
            "detail": f"detail_{idx}_" + ("x" * 400),
            "evidence": [f"evidence_{idx}_{j}_" + ("y" * 160) for j in range(4)],
        }
        for idx in range(40)
    ]

    with patch.object(
        server,
        "scan_structural_risks",
        return_value={
            "scan_scope": "scope1",
            "files_scanned": 12,
            "total_risks": len(oversized_risks),
            "risks": oversized_risks,
            "categories_scanned": [
                "slice_overlap",
                "multi_drive",
                "incomplete_case",
                "magic_condition",
            ],
            "skipped_files": [f"/tmp/skipped_{idx}.sv" for idx in range(20)],
        },
    ):
        result = await server._dispatch(
            "scan_structural_risks",
            {
                "compile_log": "/tmp/elab.log",
                "simulator": "vcs",
            },
        )

    assert result.auto_downgraded is True
    assert result.payload_bytes is not None
    assert result.payload_bytes <= schemas.TOKEN_BUDGET_SOFT_LIMIT


@pytest.mark.anyio
async def test_analyze_failures_stays_under_budget_when_budget_is_tiny(monkeypatch):
    monkeypatch.setattr(schemas, "TOKEN_BUDGET_SOFT_LIMIT", 2500)
    server._session_state["get_sim_paths"] = {
        "verif_root": "/tmp/verif",
        "case_dir": "/tmp/verif/work_case0",
        "simulator": "vcs",
        "compile_log": "/tmp/elab.log",
    }
    server._session_state["build_tb_hierarchy"] = {
        "compile_log": "/tmp/elab.log",
        "simulator": "vcs",
    }

    class _FakeAnalyzer:
        def __init__(self, *args, **kwargs):
            pass

        def analyze(self, **kwargs):
            groups = [
                {
                    "signature": f"group_{idx}",
                    "severity": "error",
                    "count": 1,
                    "first_line": idx + 1,
                    "first_time_ps": idx * 1000 + 1,
                    "sample_event_id": f"evt_{idx}",
                    "sample_message": f"message_{idx}_" + ("m" * 300),
                }
                for idx in range(12)
            ]
            return {
                "summary": {
                    "runtime_total_errors": len(groups),
                    "total_groups": len(groups),
                    "groups": groups,
                },
                "focused_group": groups[0],
                "focused_event": {
                    "event_id": "evt_0",
                    "group_signature": "group_0",
                    "time_ps": 1,
                    "source_file": "/tmp/top_tb.sv",
                    "source_line": 42,
                    "instance_path": "top_tb.u0",
                    "message": "event_" + ("n" * 400),
                },
                "log_context": {
                    "log_file": "/tmp/run.log",
                    "center_line": 42,
                    "start_line": 1,
                    "end_line": 200,
                    "context": "c" * 5000,
                },
                "wave_context": {
                    "center_time_ps": 1000,
                    "signals": {
                        f"top_tb.sig_{idx}": {
                            "value": {"bin": "1"},
                            "transitions": [
                                {"time_ps": step, "value": {"bin": "1" * 64}}
                                for step in range(40)
                            ],
                        }
                        for idx in range(8)
                    },
                },
                "remaining_groups": 11,
                "signals_queried": [f"top_tb.sig_{idx}" for idx in range(8)],
                "extra_transitions": 5,
                "analysis_guide": {
                    "step1": "one " + ("g" * 600),
                    "step2": "two " + ("h" * 600),
                },
                "problem_hints": {
                    "has_x": True,
                    "has_z": False,
                    "first_error_time_ps": 1,
                    "error_pattern": "xprop",
                },
            }

    with patch.object(server, "WaveformAnalyzer", _FakeAnalyzer), patch.object(
        server, "_get_parser", return_value=object()
    ):
        result = await server._dispatch(
            "analyze_failures",
            {
                "log_path": "/tmp/run.log",
                "wave_path": "/tmp/wave.vcd",
                "simulator": "vcs",
                "signal_paths": [f"top_tb.sig_{idx}" for idx in range(8)],
            },
        )

    assert result.auto_downgraded is True
    assert result.payload_bytes is not None
    assert result.payload_bytes <= schemas.TOKEN_BUDGET_SOFT_LIMIT
