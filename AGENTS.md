# Repository Guidance

## TraceWeave Usage

When the task involves simulation logs or waveforms (VCS/Xcelium logs, FSDB/VCD), the default toolchain is:

`get_sim_paths -> build_tb_hierarchy + scan_structural_risks -> parse_sim_log -> recommend_failure_debug_next_steps`

Rules:

- `build_tb_hierarchy` and `scan_structural_risks` must run in parallel on the same `compile_log`
- `scan_structural_risks` should not be skipped by default
- It may only be skipped if the user explicitly asks to skip it
- Do not analyze or recommend fixes before MCP output is available

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
16. `src/cycle_query.py`
17. `src/schemas.py`
18. `src/problem_hints.py`

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
- `tests/test_cycle_query.py`
- `tests/test_schemas.py`
- `tests/test_problem_hints.py`
- `tests/test_server.py`
- `tests/test_diagnostic_snapshot.py`

## Repository Focus

- `server.py` is the composition root and MCP entry point.
- `src/path_discovery.py` owns compile/sim/wave path discovery.
- `src/compile_log_parser.py` and `src/tb_hierarchy_builder.py` drive compile-log-based hierarchy extraction.
- `src/analyzer.py` and `src/log_parser.py` contain the core failure analysis logic.
- `src/signal_driver.py` backtracks RTL drivers from waveform signal paths.
- `src/structural_scanner.py` and `src/x_trace.py` are first-class analysis capabilities.
- `src/cycle_query.py` provides cycle-aligned signal sampling.
- `src/schemas.py` is the single source of truth for tool output contracts.
- `src/problem_hints.py` provides lightweight failure symptom annotations.
- `src/fsdb_parser.py` and `fsdb_wrapper.cpp` define the Python/native FSDB boundary.
- `config.py` centralizes environment-sensitive paths and behavior constants.

## Working Rule

Before making non-trivial changes, build a quick mental model from the files above instead of editing from local assumptions.
