from pathlib import Path

from src import sv_preprocessor
from src import tb_hierarchy_builder
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


def test_plain_source_skips_directive_comment_masking(tmp_path, monkeypatch):
    source = tmp_path / "plain.sv"
    raw = "module plain; /* ordinary comment */ endmodule\n"
    source.write_text(raw)
    mask_calls = 0
    original = sv_preprocessor._mask_comments

    def counted(line: str, in_block_comment: bool):
        nonlocal mask_calls
        mask_calls += 1
        return original(line, in_block_comment)

    monkeypatch.setattr(sv_preprocessor, "_mask_comments", counted)
    result = SystemVerilogPreprocessor(
        {
            "compile_cwd": str(tmp_path),
            "compile_command": f"vcs -sverilog {source}",
        }
    ).preprocess(str(source))

    assert result.text == raw
    assert result.root_include_directives == ()
    assert mask_calls == 0


def test_root_include_inventory_is_collected_during_expansion(tmp_path):
    source = tmp_path / "top.sv"
    active = tmp_path / "active.svh"
    active.write_text("leaf u_leaf();\n")
    source.write_text(
        "/* `include \"commented.svh\" */\n"
        "`define INCLUDE_TEXT \\\n"
        "`include \"macro_body.svh\"\n"
        "`ifdef DISABLED\n"
        "  `include \"inactive.svh\"\n"
        "`endif\n"
        "module top;\n"
        "  `include \"active.svh\"\n"
        "endmodule\n"
    )

    result = SystemVerilogPreprocessor(
        {
            "compile_cwd": str(tmp_path),
            "compile_command": f"vcs -sverilog +incdir+{tmp_path} {source}",
        }
    ).preprocess(str(source))

    assert result.root_include_directives == (
        "macro_body.svh",
        "inactive.svh",
        "active.svh",
    )
    assert result.active_include_directives == ("active.svh",)
    assert result.complete is True


def test_unchanged_preprocessed_text_reuses_instance_scan(tmp_path, monkeypatch):
    source = tmp_path / "top.sv"
    source.write_text(
        "module leaf; endmodule\n"
        "module top; leaf u_leaf(); endmodule\n"
    )
    preprocessed = SystemVerilogPreprocessor(
        {
            "compile_cwd": str(tmp_path),
            "compile_command": f"vcs -sverilog {source}",
        }
    ).preprocess(str(source))
    extract_calls = 0
    original = tb_hierarchy_builder._extract_module_instances

    def counted(text: str):
        nonlocal extract_calls
        extract_calls += 1
        return original(text)

    monkeypatch.setattr(
        tb_hierarchy_builder,
        "_extract_module_instances",
        counted,
    )

    result = tb_hierarchy_builder.scan_preprocessed_sv(
        str(source),
        preprocessed,
    )

    assert result["module_instance_map"]["top"] == [
        {"module_name": "leaf", "instance_name": "u_leaf"}
    ]
    assert extract_calls == 1


def test_shared_include_mask_cache_is_bounded_and_reused(tmp_path, monkeypatch):
    header = tmp_path / "shared.svh"
    roots = [tmp_path / "first.sv", tmp_path / "second.sv"]
    header_text = (
        "`timescale 1ns/1ps\n"
        "// comment containing `include \"ignored.svh\"\n"
        "\n"
    )
    header.write_text(header_text)
    for index, root in enumerate(roots):
        root.write_text(
            '`include "shared.svh"\n'
            f"module root_{index}; endmodule\n"
        )
    mask_calls = 0
    original = sv_preprocessor._mask_comments

    def counted(line: str, in_block_comment: bool):
        nonlocal mask_calls
        mask_calls += 1
        return original(line, in_block_comment)

    monkeypatch.setattr(sv_preprocessor, "_mask_comments", counted)
    cache_limit = 4096
    preprocessor = SystemVerilogPreprocessor(
        {
            "compile_cwd": str(tmp_path),
            "compile_command": (
                f"vcs -sverilog +incdir+{tmp_path} "
                + " ".join(str(root) for root in roots)
            ),
        },
        source_cache_bytes=cache_limit,
    )

    results = [preprocessor.preprocess(str(root)) for root in roots]

    root_line_count = sum(
        len(root.read_text().splitlines(keepends=True)) for root in roots
    )
    header_line_count = len(header_text.splitlines(keepends=True))
    assert mask_calls == root_line_count + header_line_count
    assert all(result.complete for result in results)
    assert preprocessor._cache_bytes <= cache_limit
    assert preprocessor._cache_bytes == sum(
        entry.size for entry in preprocessor._cache.values()
    )
    cached_header = preprocessor._cache[str(header.resolve())]
    assert cached_header.masked is not None
    assert (
        preprocessor._cached_masked_text(
            str(header.resolve()),
            header_text + "changed",
        )
        is None
    )


def test_shared_include_mask_cache_respects_disabled_and_tiny_limits(
    tmp_path, monkeypatch
):
    header = tmp_path / "shared.svh"
    roots = [tmp_path / "first.sv", tmp_path / "second.sv"]
    header_text = "`timescale 1ns/1ps\n\n"
    header.write_text(header_text)
    for index, root in enumerate(roots):
        root.write_text(
            '`include "shared.svh"\n'
            f"module root_{index}; endmodule\n"
        )
    original = sv_preprocessor._mask_comments

    for cache_limit in (0, len(header_text) * 2 - 1):
        mask_calls = 0

        def counted(line: str, in_block_comment: bool):
            nonlocal mask_calls
            mask_calls += 1
            return original(line, in_block_comment)

        with monkeypatch.context() as patcher:
            patcher.setattr(sv_preprocessor, "_mask_comments", counted)
            preprocessor = SystemVerilogPreprocessor(
                {
                    "compile_cwd": str(tmp_path),
                    "compile_command": (
                        f"vcs -sverilog +incdir+{tmp_path} "
                        + " ".join(str(root) for root in roots)
                    ),
                },
                source_cache_bytes=cache_limit,
            )
            for root in roots:
                preprocessor.preprocess(str(root))

        expected_calls = sum(
            len(root.read_text().splitlines(keepends=True)) for root in roots
        ) + 2 * len(header_text.splitlines(keepends=True))
        assert mask_calls == expected_calls
        assert preprocessor._cache_bytes <= cache_limit


def test_source_cache_lru_accounts_for_raw_and_masked_text(tmp_path):
    preprocessor = SystemVerilogPreprocessor(
        {
            "compile_cwd": str(tmp_path),
            "compile_command": "vcs -sverilog",
        },
        source_cache_bytes=10,
    )

    assert preprocessor._store_cache_entry(
        "first",
        sv_preprocessor._SourceCacheEntry(raw="abc", masked="   "),
    )
    assert preprocessor._store_cache_entry(
        "second",
        sv_preprocessor._SourceCacheEntry(raw="defg"),
    )
    assert preprocessor._cache_bytes == 10

    assert preprocessor._cached_masked_text("first", "abc") == "   "
    assert preprocessor._store_cache_entry(
        "third",
        sv_preprocessor._SourceCacheEntry(raw="hi"),
    )

    assert list(preprocessor._cache) == ["first", "third"]
    assert preprocessor._cache_bytes == 8
