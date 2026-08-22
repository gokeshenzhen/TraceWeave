from pathlib import Path

from src import sv_preprocessor
from src.sv_preprocessor import SystemVerilogPreprocessor


def _conditional_source() -> str:
    return (
        "`ifdef FIRST\n"
        "leaf u_first();\n"
        "`elsif SECOND\n"
        "leaf u_second();\n"
        "`elsif FALLBACK\n"
        "leaf u_fallback();\n"
        "`endif\n"
    )


def test_duplicate_source_record_preserves_first_phase_match(tmp_path):
    source = tmp_path / "top.sv"
    source.write_text(_conditional_source())
    compile_result = {
        "compile_cwd": str(tmp_path),
        "compile_command": "vcs +define+FALLBACK",
        "compile_evidence": {
            "ordered_compilation_units": [
                {"path": str(source), "source_log_index": 2},
                {"path": str(source), "source_log_index": 1},
            ],
            "source_phases": [
                {
                    "source_log_index": 1,
                    "compile_command": "vcs +define+SECOND",
                    "compile_cwd": str(tmp_path),
                },
                {
                    "source_log_index": 2,
                    "compile_command": "vcs +define+FIRST",
                    "compile_cwd": str(tmp_path),
                },
            ],
        },
    }

    result = SystemVerilogPreprocessor(compile_result).preprocess(str(source))

    assert "u_first" in result.text
    assert "u_second" not in result.text
    assert "u_fallback" not in result.text


def test_missing_source_log_index_replays_all_phases_in_order(tmp_path):
    source = tmp_path / "top.sv"
    source.write_text(
        "`ifdef FIRST\n"
        "  `ifdef SECOND\n"
        "    leaf u_both();\n"
        "  `endif\n"
        "`endif\n"
    )
    compile_result = {
        "compile_cwd": str(tmp_path),
        "compile_command": "vcs",
        "compile_evidence": {
            "ordered_compilation_units": [{"path": str(source)}],
            "source_phases": [
                {
                    "source_log_index": 1,
                    "compile_command": "vcs +define+FIRST",
                },
                {
                    "source_log_index": 2,
                    "compile_command": "vcs +define+SECOND",
                },
            ],
        },
    }

    result = SystemVerilogPreprocessor(compile_result).preprocess(str(source))

    assert "u_both" in result.text


def test_missing_matching_phase_uses_compile_result_fallback(tmp_path):
    source = tmp_path / "top.sv"
    source.write_text(_conditional_source())
    compile_result = {
        "compile_cwd": str(tmp_path),
        "compile_command": "vcs +define+FALLBACK",
        "compile_evidence": {
            "ordered_compilation_units": [
                {"path": str(source), "source_log_index": 99}
            ],
            "source_phases": [
                {
                    "source_log_index": 1,
                    "compile_command": "vcs +define+FIRST",
                }
            ],
        },
    }

    result = SystemVerilogPreprocessor(compile_result).preprocess(str(source))

    assert "u_first" not in result.text
    assert "u_fallback" in result.text


def test_context_index_canonicalization_scales_linearly(tmp_path, monkeypatch):
    source_count = 64
    sources = [tmp_path / f"unit_{index:04d}.sv" for index in range(source_count)]
    compile_result = {
        "compile_cwd": str(tmp_path),
        "compile_command": "vcs",
        "compile_evidence": {
            "ordered_compilation_units": [
                {"path": str(path), "source_log_index": 0} for path in sources
            ],
            "source_phases": [
                {
                    "source_log_index": 0,
                    "compile_command": "vcs +define+BENCH",
                    "compile_cwd": str(tmp_path),
                }
            ],
        },
    }
    canonical_calls = 0
    original = sv_preprocessor._canonical

    def counted(path: str | Path) -> str:
        nonlocal canonical_calls
        canonical_calls += 1
        return original(path)

    monkeypatch.setattr(sv_preprocessor, "_canonical", counted)
    preprocessor = SystemVerilogPreprocessor(
        compile_result,
        source_loader=lambda _path: "module unit; endmodule\n",
    )
    for path in sources:
        preprocessor.preprocess(str(path))

    assert canonical_calls <= source_count + 4
