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
25. `src/hierarchy_handles.py`
26. `src/handle_tools.py`
27. `src/cursor_store.py`
28. `src/timespec.py`
29. `src/verify_condition.py`

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
- `src/cycle_query.py` provides cycle-aligned signal sampling. `get_signals_by_cycle` slices by cycle index (capped); `sample_signals_on_edges` samples every clock edge inside a *time window* (the shared substrate for window-scoped relational analysis like `inspect_handshake`). Both reuse one private edge-sampling core.
- `src/schemas.py` is the single source of truth for tool output contracts.
- `src/problem_hints.py` provides lightweight failure symptom annotations.
- `src/hierarchy_handles.py` owns the in-process `HandleStore` and content-addressed handle derivation. `build_tb_hierarchy` returns a slim payload + `hierarchy_handle`; the full hierarchy is registered here and resolved by the handle tools. Handles are not persisted — server restart drops them.
- `src/handle_tools.py` implements `get_tb_subtree`, `lookup_tb_files`, `find_tb_instance`, `get_tb_file_detail`, `get_tb_class_hierarchy`, `dump_tb_section` as pure functions over a resolved full hierarchy dict. `lookup_tb_files` requires at least one filter; `get_tb_file_detail` returns `did_you_mean` basename suggestions when the path is not in the compile set (multi-version safety net).
- `src/fsdb_parser.py` and `fsdb_wrapper.cpp` define the Python/native FSDB boundary.
- `src/cursor_store.py` owns the in-process `CursorStore` — named, process-scoped time anchors (`cursor_set`/`cursor_list`/`cursor_delete`). Same lifetime semantics as `HandleStore`: not persisted, dropped on restart, no "active cursor" (references are always explicit `@name`).
- `src/timespec.py` resolves a TimeSpec (raw ps int, `@cursor` ref, or unit literal like `12.34ns`) to picoseconds. `server._resolve_time` wires it into every time-taking tool input (`get_signal_at_time`, `get_signal_transitions`, `get_signals_around_time`, `trace_x_source`, `diff_first_divergence`, `period`). Arithmetic (`@c ± cycle(clk)`) is intentionally NOT implemented yet (reserved for a future Lark grammar).
- `src/verify_condition.py` holds the auto-debug verification primitives: `diff_first_divergence` (first time two waveform signals differ; cross-run or within-run), `period` (median edge period + first off-beat), and `inspect_handshake` (cycle-by-cycle valid/ready classification: stalls, long-stall windows, backpressure imbalance, and payload-hold violations during a stall — the protocol-timing class of bug that leaves no value pattern in scoreboard logs; protocol-agnostic over AXI/valid-ready/credit; for AHB pass `valid_htrans` + `htrans_rule` to derive valid from htrans since there is no literal valid signal, echoed as `valid_source`). All three auto-register a cursor (`inspect_handshake` anchors hold-violation > long-stall > longest-stall) and read existing waveforms only. It also contains `diff_value_distribution` (multi-sample fail/pass differential), which is implemented and tested but **deliberately not registered as an MCP tool** — blind A/B pilots showed no clear benefit over baseline on the common scoreboard-data-mismatch flow, so it is kept off the tool surface; re-register in `server.py` if a real use-case appears.
- `src/window_verify.py` provides `verify_window` (P3, the "small templated version" of the propose-and-verify engine): the LLM states a temporal predicate and the tool returns a precise `holds` verdict + a concrete witness/counterexample (cycle + sampled `signal_values`) over a clock window. Deliberately **templates, not a DSL** — a *term* is `{signal, op, value}` (op: eq/ne/gt/ge/lt/le/is_x/is_known), a *predicate* is a term list = implicit AND (no OR/nesting; run two calls for OR), and four *modes*: `always`/`never`/`eventually`/`implication` (A |-> B within N cycles). inspect_handshake/period/diff are conceptually special cases of this; the point is future analysis composes a predicate rather than spawning another bespoke tool. Faithful: x/z cycles → `unknown_cycles` (never silently passed), an implication whose response window runs past end-of-trace → `inconclusive_count` (never silently failed), unresolved signals are loud. Reuses `sample_signals_on_edges` + `_resolve_signal_path`; registers one cursor at the evidence. Decoupled from auto-hypothesis on purpose (hypothesis generation stays the LLM's job — do NOT bolt it into recommend_next_steps).
- `src/handshake_suggest.py` provides `suggest_handshakes` (T2 of the protocol-debug plan, the "self-serve multiplier"): scans the waveform's signal universe via `search_signals` and proposes ready-to-use `inspect_handshake` bundles — pairs `*valid`/`*ready` by scope + stem, locates the clock (same scope or nearest ancestor), and groups channel payload buses (width>1, non-bookkeeping var_types, preferring the channel stem prefix). Core `propose_handshake_bundles` is a pure function over `{path,name,width,var_type}` descriptors (fully unit-tested); the I/O wrapper gathers candidates from a parser. Reuses analyzer's handshake role vocabulary. Does NOT synthesise an AHB "valid" (no literal signal — it is `htrans != IDLE`).
- `src/handshake_sweep.py` provides `sweep_handshakes` (P1 of the enhancement roadmap): a whole-design handshake **anomaly sweep**. Discovers every valid/ready interface via `suggest_handshakes`, runs `inspect_handshake` over each, and returns a **comparative fact table** (`SweptInterface` rows: per-interface stall/deadlock/payload-hold/backpressure facts + factual `flags`) ordered by a transparent mechanical key (ended_in_stall > payload_hold_violations > max_stall_cycles > ready_without_valid). It is a context/round-trip reducer for the M-way fan-out case, NOT a root-cause verdict — every raw fact is exposed so the LLM re-ranks (perception-vs-judgment boundary). On a backpressured pipeline the sort surfaces the propagation *front*, not the root; the root is the stall→starvation boundary, which the LLM derives from the facts. Registers exactly ONE cursor at the top interface's stall begin; no-clock bundles → `skipped`; discovery beyond `max_interfaces` (default 64) sets `truncated=true` with a loud note. Relies on `inspect_handshake`'s `ended_in_stall`/`final_stall_cycles`. Hidden under the same A/B toggle as inspect/suggest.
- `src/txn_reconstruct.py` provides `reconstruct_transactions` (P2, the id-correlated transaction layer — the biggest capability gap vs xwave and the most durable perception@scale primitive): walks a request handshake channel + a completion channel over the whole window, matches accepted beats by an `id` field, and returns per-transaction latency + aggregate facts. **One generic core, not a tool per protocol** (the moat discipline): AXI read = AR→R (`cmp_last`=rlast, id=arid/rid); AXI write = AW→B (id=awid/bid) PLUS an optional unindexed W-data channel (`data_valid`/`data_ready`/`data_last` + `data_fields`; W carries no id so beats attach in order to the oldest data-incomplete request, matching real interconnect); any id'd req/resp; CHI-like. AHB/APB (no id, phase-based) are out of scope. An optional `reset` (`reset_active_low`) clears in-flight state so a txn straddling reset is not a phantom hang (correctness, emits `reset_clears`); `capture_beats` (off by default → only `beat_count`) returns per-beat `data_beats[]` for data-integrity debug. Facts not verdict: `latency` distribution (min/median/max/mean) not an "outlier" label; `outstanding_at_end`/`max_outstanding`/`max_outstanding_per_id`; `reorder_count` (informational, legal in AXI); `timeout_cycles`→`slow_count`; unmatched req/cmp surfaced loudly (the hang signature); one cursor (first never-completed request > peak outstanding). Out-of-order completion across ids via per-id FIFO. Fact set modelled on xwave's C++ `src/axi/axi_analyzer` (verified by reading it — xwave is C++, there is no axi.py); ported the durable-perception parts and deliberately skipped the LLM-territory/human-CLI parts (addr/id getters, cursor paging, resp/burst decode) — the perception-vs-judgment boundary. Reuses `sample_signals_on_edges` + `_resolve_signal_path` + `_hs_truth`/`_hs_repr`.
- `src/usage_telemetry.py` provides passive, local-only usage telemetry: `server.call_tool` (the single dispatch choke point) appends one JSONL line per tool call to `$TRACEWEAVE_CACHE_DIR/telemetry/usage.jsonl` (tool name, arg keys + whitelisted scalar flags only — never values/paths, `result_bytes` token proxy, latency, ok/blocked). A session is anchored to each `get_sim_paths` case identity via `note_session`. Recording is best-effort (never raises into the call path). `aggregate()` is a pure function backing the offline `scripts/telemetry_report.py` CLI; it is NOT an MCP tool. Default-on; opt out with `TRACEWEAVE_TELEMETRY=0`. Exists to quantify how often the auto-debug v2 primitives (cursor/period/diff_first_divergence) actually get used before building more.
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
