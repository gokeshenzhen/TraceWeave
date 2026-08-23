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


def test_include_evidence_mismatch_keeps_positive_hierarchy_evidence(tmp_path):
    source = tmp_path / "top.sv"
    active = tmp_path / "active.svh"
    extra = tmp_path / "simulator_only.svh"
    source.write_text(
        "module top;\n"
        '  `include "active.svh"\n'
        "endmodule\n"
    )
    active.write_text("leaf u_leaf();\n")
    extra.write_text("// recorded by the simulator but not locally replayed\n")
    compile_result = {
        "compile_cwd": str(tmp_path),
        "compile_command": f"vcs -sverilog +incdir+{tmp_path} {source}",
        "compile_evidence": {
            "ordered_includes": [
                {"parent": str(source), "path": str(active)},
                {"parent": str(source), "path": str(extra)},
            ]
        },
    }

    preprocessed = SystemVerilogPreprocessor(compile_result).preprocess(
        str(source)
    )
    scan = tb_hierarchy_builder.scan_preprocessed_sv(
        str(source),
        preprocessed,
    )

    assert preprocessed.complete is False
    assert preprocessed.issues == ("include_evidence_mismatch",)
    assert preprocessed.hierarchy_evidence_status == "positive_local"
    assert "u_leaf" in preprocessed.trusted_hierarchy_text
    assert scan["module_instance_map"]["top"] == [
        {
            "module_name": "leaf",
            "instance_name": "u_leaf",
            "hierarchy_edge_status": "positive_local",
            "hierarchy_gap_codes": [
                "hierarchy_include_evidence_mismatch"
            ],
            "hierarchy_edge_origin": "preprocessed_positive_local",
        }
    ]


def test_incomplete_compile_options_invalidate_conditional_hierarchy(tmp_path):
    source = tmp_path / "top.sv"
    source.write_text(
        "module top;\n"
        "`ifdef FPGA_BUILD\n"
        "  fpga_stub u_selected();\n"
        "`else\n"
        "  dut u_selected();\n"
        "`endif\n"
        "endmodule\n"
    )

    preprocessed = SystemVerilogPreprocessor({}).preprocess(str(source))
    scan = tb_hierarchy_builder.scan_preprocessed_sv(
        str(source),
        preprocessed,
    )

    assert preprocessed.issues == ("compile_options_incomplete",)
    assert preprocessed.hierarchy_evidence_status == "unproved"
    assert preprocessed.trusted_hierarchy_text == ""
    assert scan["module_instance_map"] == {}
    assert scan["structural_modules"] == []
    assert scan["hierarchy_gap_codes"] == [
        "hierarchy_compile_options_incomplete"
    ]


def test_preprocessor_issues_map_to_distinct_hierarchy_gap_codes():
    expected = {
        "compile_options_incomplete": "hierarchy_compile_options_incomplete",
        "conditional_unbalanced": "hierarchy_conditional_unbalanced",
        "include_cycle": "hierarchy_include_cycle",
        "include_depth_exceeded": "hierarchy_include_depth_exceeded",
        "include_evidence_mismatch": "hierarchy_include_evidence_mismatch",
        "include_expression_unresolved": (
            "hierarchy_include_expression_unresolved"
        ),
        "include_path_unresolved": "hierarchy_include_path_unresolved",
        "include_unreadable": "hierarchy_include_unreadable",
        "macro_continuation_unterminated": (
            "hierarchy_macro_continuation_unterminated"
        ),
        "hierarchy_macro_compound_unsupported": (
            "hierarchy_macro_compound_unsupported"
        ),
        "hierarchy_macro_expansion_limit_exceeded": (
            "hierarchy_macro_expansion_limit_exceeded"
        ),
    }

    for issue, gap_code in expected.items():
        assert tb_hierarchy_builder._hierarchy_preprocessor_gap_codes(
            (issue,)
        ) == {gap_code}
    assert tb_hierarchy_builder._hierarchy_preprocessor_gap_codes(
        ("future_unknown_issue",)
    ) == {"hierarchy_preprocessor_incomplete"}


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

    def counted(text: str, **kwargs):
        nonlocal extract_calls
        extract_calls += 1
        return original(text, **kwargs)

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
        {
            "module_name": "leaf",
            "instance_name": "u_leaf",
            "hierarchy_edge_status": "complete",
            "hierarchy_gap_codes": [],
            "hierarchy_edge_origin": "root_lexical",
        }
    ]
    assert extract_calls == 1


def test_changed_preprocessed_text_skips_discarded_root_instance_scan(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "top.sv"
    header = tmp_path / "leaf.svh"
    source.write_text(
        "module leaf; endmodule\n"
        "module top;\n"
        '  `include "leaf.svh"\n'
        "endmodule\n"
    )
    header.write_text("leaf u_leaf();\n")
    preprocessed = SystemVerilogPreprocessor(
        {
            "compile_cwd": str(tmp_path),
            "compile_command": f"vcs -sverilog +incdir+{tmp_path} {source}",
        }
    ).preprocess(str(source))
    extract_calls = 0
    original = tb_hierarchy_builder._extract_module_instances

    def counted(text: str, **kwargs):
        nonlocal extract_calls
        extract_calls += 1
        return original(text, **kwargs)

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
        {
            "module_name": "leaf",
            "instance_name": "u_leaf",
            "hierarchy_edge_status": "complete",
            "hierarchy_gap_codes": [],
            "hierarchy_edge_origin": "preprocessed_lexical",
        }
    ]
    assert extract_calls == 1


def test_unproved_preprocessed_text_skips_discarded_root_instance_scan(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "top.sv"
    source.write_text(
        "module top;\n"
        "`ifdef UNKNOWN\n"
        "  leaf u_unproved();\n"
        "`endif\n"
        "endmodule\n"
    )
    preprocessed = SystemVerilogPreprocessor({}).preprocess(str(source))
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

    assert result["module_instance_map"] == {}
    assert extract_calls == 0


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


def test_comment_mask_fast_path_is_counted_for_slash_free_directive_lines(
    tmp_path,
):
    source = tmp_path / "conditional.sv"
    source.write_text(
        "`ifdef ENABLED\n"
        "module selected; endmodule\n"
        "`endif\n"
    )
    preprocessor = SystemVerilogPreprocessor(
        {
            "compile_cwd": str(tmp_path),
            "compile_command": f"vcs +define+ENABLED {source}",
        }
    )

    result = preprocessor.preprocess(str(source))
    metrics = preprocessor.metrics_snapshot()

    assert "module selected" in result.text
    assert metrics["preprocessor_comment_mask_line_count"] == 3
    assert metrics["preprocessor_comment_mask_fast_path_count"] == 3
    assert metrics["preprocessor_logical_file_expansion_count"] == 1


def test_plain_expansion_lines_skip_directive_and_macro_recognizers(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "conditional.sv"
    source.write_text(
        "`ifdef ENABLED\n"
        "module selected; endmodule\n"
        "`else\n"
        "module inactive; endmodule\n"
        "`endif\n"
    )
    directive_calls = 0
    macro_calls = 0
    directive = sv_preprocessor._DIRECTIVE_RE
    original_macro = sv_preprocessor._expand_hierarchy_macro_line

    class CountedDirective:
        def match(self, value: str):
            nonlocal directive_calls
            directive_calls += 1
            return directive.match(value)

    def counted_macro(line, masked, state):
        nonlocal macro_calls
        macro_calls += 1
        return original_macro(line, masked, state)

    monkeypatch.setattr(sv_preprocessor, "_DIRECTIVE_RE", CountedDirective())
    monkeypatch.setattr(
        sv_preprocessor,
        "_expand_hierarchy_macro_line",
        counted_macro,
    )
    preprocessor = SystemVerilogPreprocessor(
        {
            "compile_cwd": str(tmp_path),
            "compile_command": f"vcs +define+ENABLED {source}",
        }
    )

    result = preprocessor.preprocess(str(source))
    metrics = preprocessor.metrics_snapshot()

    assert "module selected" in result.text
    assert "module inactive" not in result.text
    assert directive_calls == 3
    assert macro_calls == 0
    assert metrics["preprocessor_plain_expansion_line_fast_path_count"] == 2


def test_nested_include_resolution_cache_is_bounded_and_reused(tmp_path):
    shared = tmp_path / "shared.svh"
    wrapper = tmp_path / "wrapper.svh"
    roots = [tmp_path / "first.sv", tmp_path / "second.sv"]
    shared.write_text("leaf u_leaf();\n")
    wrapper.write_text('`include "shared.svh"\n')
    for index, root in enumerate(roots):
        root.write_text(
            '`include "wrapper.svh"\n'
            f"module root_{index}; endmodule\n"
        )
    preprocessor = SystemVerilogPreprocessor(
        {
            "compile_cwd": str(tmp_path),
            "compile_command": (
                f"vcs +incdir+{tmp_path} "
                + " ".join(str(root) for root in roots)
            ),
        },
        include_resolution_cache_entries=2,
    )

    results = [preprocessor.preprocess(str(root)) for root in roots]
    metrics = preprocessor.metrics_snapshot()

    assert all("leaf u_leaf" in result.text for result in results)
    assert metrics["preprocessor_include_resolution_cache_hit_count"] == 1
    assert metrics["preprocessor_include_resolution_cache_miss_count"] == 3
    assert metrics["preprocessor_include_resolution_cache_eviction_count"] == 1
    assert metrics["preprocessor_include_resolution_cache_entry_count"] == 2
    assert metrics["preprocessor_include_resolution_cache_limit_entries"] == 2


def test_missing_include_resolution_is_not_cached(tmp_path):
    source = tmp_path / "top.sv"
    header = tmp_path / "late.svh"
    source.write_text('`include "late.svh"\nmodule top; endmodule\n')
    preprocessor = SystemVerilogPreprocessor(
        {
            "compile_cwd": str(tmp_path),
            "compile_command": f"vcs +incdir+{tmp_path} {source}",
        }
    )

    before = preprocessor.preprocess(str(source))
    header.write_text("leaf u_leaf();\n")
    after = preprocessor.preprocess(str(source))
    metrics = preprocessor.metrics_snapshot()

    assert before.issues == ("include_path_unresolved",)
    assert after.complete is True
    assert "leaf u_leaf" in after.text
    assert metrics["preprocessor_include_resolution_cache_hit_count"] == 0
    assert metrics["preprocessor_include_resolution_cache_miss_count"] == 2
    assert metrics["preprocessor_include_resolution_cache_entry_count"] == 1


def test_unique_recorded_include_basename_skips_directory_search(tmp_path):
    source = tmp_path / "top.sv"
    recorded_dir = tmp_path / "recorded"
    recorded_dir.mkdir()
    recorded = recorded_dir / "defs.svh"
    recorded.write_text("leaf u_leaf();\n")
    source.write_text('`include "defs.svh"\nmodule top; endmodule\n')
    nonexistent_dirs = [tmp_path / f"missing_{index}" for index in range(64)]
    compile_result = {
        "compile_cwd": str(tmp_path),
        "compile_command": (
            "vcs "
            + "+incdir+"
            + "+".join(str(path) for path in nonexistent_dirs)
            + f" {source}"
        ),
        "compile_evidence": {
            "ordered_includes": [
                {"parent": str(source), "path": str(recorded)}
            ]
        },
    }
    preprocessor = SystemVerilogPreprocessor(compile_result)

    result = preprocessor.preprocess(str(source))
    metrics = preprocessor.metrics_snapshot()

    assert result.complete is True
    assert "leaf u_leaf" in result.text
    assert metrics["preprocessor_exact_include_resolution_count"] == 1
    assert metrics["preprocessor_include_resolution_cache_miss_count"] == 1


def test_ambiguous_recorded_basename_preserves_include_dir_order(tmp_path):
    source = tmp_path / "top.sv"
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = first_dir / "defs.svh"
    second = second_dir / "defs.svh"
    first.write_text("leaf u_first();\n")
    second.write_text("leaf u_second();\n")
    source.write_text('`include "defs.svh"\nmodule top; endmodule\n')
    preprocessor = SystemVerilogPreprocessor(
        {
            "compile_cwd": str(tmp_path),
            "compile_command": (
                f"vcs +incdir+{first_dir}+{second_dir} {source}"
            ),
            "compile_evidence": {
                "ordered_includes": [
                    {"parent": str(source), "path": str(first)},
                    {"parent": str(source), "path": str(second)},
                ]
            },
        }
    )

    result = preprocessor.preprocess(str(source))
    metrics = preprocessor.metrics_snapshot()

    assert "u_first" in result.text
    assert "u_second" not in result.text
    assert metrics["preprocessor_exact_include_resolution_count"] == 0
