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
14. `src/signal_load.py`
15. `src/connectivity_backend.py`
16. `src/verdi_backend.py`
17. `src/verdi_npi_backend.py`
18. `src/kdb_builder.py`
19. `src/waveform_batch.py`
20. `src/structural_scanner.py`
21. `src/x_trace.py`
22. `src/cycle_query.py`
23. `src/schemas.py`
24. `src/problem_hints.py`

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
- `tests/test_signal_load.py`
- `tests/test_connectivity_backend.py`
- `tests/test_verdi_backend.py`
- `tests/test_verdi_npi_backend.py`
- `tests/test_kdb_builder.py`
- `tests/test_waveform_batch.py`
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
- `src/signal_load.py` resolves load/fanout for a signal — the symmetric counterpart to `signal_driver`.
- `src/connectivity_backend.py` defines the `ConnectivityBackend` protocol; `select_backend()` returns the Verdi NPI backend when a KDB is found, otherwise Static. NPI failures degrade transparently; the dispatch layer never sees verdi-specific exceptions.
- `src/verdi_backend.py` probes for Verdi KDB / license environment; emits per-simulator `kdb_hint` when KDB is missing.
- `src/verdi_npi_backend.py` is the NPI-backed implementation of `find_driver` / `find_loads` / `find_path`, plus `collect_instance_src_map` used by `build_tb_hierarchy` to overlay elaborated-netlist `file:line` onto compile-log-derived hierarchy nodes. Lazily loads `pynpi` from `$VERDI_HOME` and caches loaded designs across calls. Uses `NetHdl.fan_in_reg_list` to walk the elaborated netlist across instance boundaries — this is why NPI can resolve drivers that Static source-regex cannot reach. `find_path` wraps `netlist.sig_to_sig_conn_list`; Static has no equivalent and returns `unsupported_reason="static_backend_no_path_api"` (honest no-op rather than a regex approximation).
- `src/kdb_builder.py` provides the `build_kdb` MCP tool: when a Verdi KDB is missing (typical for Xcelium / `xrun` flows), it runs `vericom -kdb` + `elabcom -elab kdb` against the file list parsed from the compile log, caches the result under `$TRACEWEAVE_CACHE_DIR/kdb/<hash>/`, and writes a runnable `build.sh` reproducer. The probe in `verdi_backend.py` picks up the cache transparently as `kdb_flow: "traceweave_cached"`. Default-on; opt out with `TRACEWEAVE_AUTO_KDB=0`.
- `src/waveform_batch.py` exposes `WaveformBatchReader` for time-window multi-signal reads, with FSDB and VCD implementations sharing one shape.
- `src/structural_scanner.py` and `src/x_trace.py` are first-class analysis capabilities.
- `src/cycle_query.py` provides cycle-aligned signal sampling.
- `src/schemas.py` is the single source of truth for tool output contracts.
- `src/problem_hints.py` provides lightweight failure symptom annotations.
- `src/fsdb_parser.py` and `fsdb_wrapper.cpp` define the Python/native FSDB boundary.
- `config.py` centralizes environment-sensitive paths and behavior constants.

## Working Rule

Before making non-trivial changes, build a quick mental model from the files above instead of editing from local assumptions.

## Documentation Rule

When a behavior change requires doc updates, **only touch documents tracked in
git**. Run `git ls-files | grep -E '\.md$'` to see the canonical doc set
(currently `README.md`, `AGENTS.md`, `CLAUDE.md`, `docs/architecture.md`,
`docs/workflow.md`). Untracked files under `docs/` are local drafts, RFCs, and
session notes — do not edit them as part of code changes and do not create new
ones unless the user explicitly asks. This applies to every agent working in
this repository (Claude, Codex, others).
