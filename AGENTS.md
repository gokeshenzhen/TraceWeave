# Repository Guidance

## Waveform MCP Usage

当任务涉及仿真日志或波形（VCS/Xcelium log，FSDB/VCD）时，默认工具链使用：

`get_sim_paths -> build_tb_hierarchy + scan_structural_risks -> parse_sim_log -> recommend_failure_debug_next_steps`

要求：

- `build_tb_hierarchy` 和 `scan_structural_risks` 基于同一个 `compile_log` 并行执行
- 默认不应省略 `scan_structural_risks`
- 仅当用户显式要求跳过时，才允许跳过
- 在获得 MCP 输出后再进行分析或给出修复建议

## First-Read Files

For any new session, read these files first to build the project map:

1. `docs/architecture.md`
2. `README.md`
3. `server.py`
4. `config.py`
5. `src/path_discovery.py`
6. `src/compile_log_parser.py`
7. `src/tb_hierarchy_builder.py`
8. `src/analyzer.py`
9. `src/log_parser.py`
10. `src/fsdb_parser.py`
11. `src/vcd_parser.py`
12. `src/fsdb_signal_index.py`
13. `src/signal_driver.py`
14. `src/structural_scanner.py`
15. `src/x_trace.py`
16. `src/schemas.py`
17. `src/problem_hints.py`

If the task involves FSDB or native integration, also read:

- `fsdb_wrapper.cpp`
- `build_wrapper.sh`

If the task involves behavior validation or regression checks, also read:

- `tests/test_log_parser.py`
- `tests/test_compile_log_parser.py`
- `tests/test_fsdb_parser.py`
- `tests/test_fsdb_runtime.py`
- `tests/test_vcd_parser.py`
- `tests/test_tb_hierarchy_builder.py`
- `tests/test_path_discovery.py`
- `tests/test_analyzer.py`
- `tests/test_signal_driver.py`
- `tests/test_structural_scanner.py`
- `tests/test_x_trace.py`
- `tests/test_schemas.py`
- `tests/test_problem_hints.py`
- `tests/test_server.py`
- `tests/test_diagnostic_snapshot.py`

## Repository Focus

- `server.py` is the composition root and MCP entry point.
- `src/path_discovery.py` owns compile/sim/wave path discovery.
- `src/compile_log_parser.py` and `src/tb_hierarchy_builder.py` drive compile-log-based hierarchy extraction.
- `src/analyzer.py` and `src/log_parser.py` contain the core failure analysis logic.
- `src/signal_driver.py` backtracks RTL driver from waveform signal paths.
- `src/structural_scanner.py` and `src/x_trace.py` are first-class extended analysis capabilities.
- `src/schemas.py` is the single source of truth for all tool output contracts.
- `src/problem_hints.py` provides lightweight failure symptom annotations.
- `src/fsdb_parser.py` and `fsdb_wrapper.cpp` define the Python/native FSDB boundary.
- `config.py` centralizes environment-sensitive paths and discovery/behavior constants.

## Working Rule

Before making non-trivial changes, build a quick mental model from the files above instead of editing from local assumptions.
