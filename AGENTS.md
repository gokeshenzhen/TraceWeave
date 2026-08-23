# Repository Guidance

## Domain Context and Authorized Scope

TraceWeave is a local EDA/RTL simulation-debugging project for semiconductor designs and simulation artifacts that the user owns or is authorized to analyze.

`VCD` and `FSDB` are local digital simulation waveform databases.

In this repository, terms such as `scan`, `sweep`, `handshake`, `protocol`, `interface`, `payload`, `trace`, `driver`, `load`, and `connectivity` refer to digital-logic signals, bus behavior, waveform transitions, and elaborated-netlist relationships.

`protected IP` means semiconductor intellectual property. `narrow_condition_injection` is an RTL structural-analysis category for control-logic conditions.

Repository tasks are limited to local source code, local waveform and log files, test fixtures, and locally configured EDA tools. Do not access unrelated external systems or resources.

For performance optimizations:

1. Establish a measurable baseline before making substantial implementation changes when practical.

2. Prefer preserving public MCP tool interfaces and output schemas. Intentional interface or schema changes are allowed when explicitly requested or when they provide a justified architectural benefit; update callers, tests, documentation, and compatibility handling accordingly.

3. Preserve FSDB thread-safety and observable cancellation and timeout behavior. Changes to locking, scheduling, worker, or execution models are allowed when justified by evidence and covered by focused regression tests.

4. Add regression tests appropriate to the behavioral risk of the change, and use reproducible before/after benchmarks for performance-sensitive changes.

5. Report the tested workload, benchmark conditions, measured results, memory impact when relevant, and any behavioral or compatibility trade-offs before claiming a performance improvement.

## Installation Discipline

When a user explicitly asks to install TraceWeave, first run the read-only check:

```bash
bash scripts/setup_source_graph.sh --check
```

If the repository-local environment is missing or incompatible, run `bash scripts/setup_source_graph.sh` only as part of that requested installation. The setup creates/updates `.venv`, installs `requirements-source-graph.txt`, verifies the pinned `pyslang` frontend, and prints MCP registration commands. It never edits shell startup files, Codex configuration, or Claude configuration. Do not run dependency installation on ordinary analysis/debug tasks, and do not modify a user's MCP client configuration unless the user explicitly requests that additional action.

## TraceWeave Usage

When the task involves simulation logs or waveforms (VCS/Xcelium logs, FSDB/VCD), the default toolchain is:

`get_sim_paths -> build_tb_hierarchy + scan_structural_risks -> parse_sim_log -> sweep_handshakes -> recommend_failure_debug_next_steps`

Rules:

- `build_tb_hierarchy` and `scan_structural_risks` must run in parallel on the same `compile_log`
- `scan_structural_risks` should not be skipped by default
- It may only be skipped if the user explicitly asks to skip it
- `sweep_handshakes` is the runtime-layer counterpart of `scan_structural_risks`: a default-flow protocol-health scan. Run it after `parse_sim_log` whenever a waveform exists and the run failed; like the structural scan it should not be skipped by default (skip only if there is no waveform or the user asks). It returns a per-interface stall/deadlock/payload-hold fact table over every AHB and valid/ready interface — facts the LLM judges, not a verdict. A scoreboard/data-compare failure is frequently a protocol symptom, so this is the cheap one-call check before reading RTL line-by-line. Always read `coverage_status`: `zero_coverage` means no protocol interfaces were checked and is not a pass; `truncated`/`degraded` means partial coverage, so `flagged_count=0` is not a clean-protocol conclusion. When `coverage_status` is `truncated`, the result includes `suggested_next_actions` with a one-click retry; follow it to complete coverage. Workflow routing relays a retry only when it changes scope, time window, edge, or interface cap. In particular, an unscoped `zero_coverage` result with zero discovered interfaces and no parameter-changing action is not blindly replayed: it remains explicitly "not a protocol pass", but the same call cannot add facts. A `degraded` result without an actionable retry reports the missing dump/clock/window prerequisite instead of replaying the same sweep. The compact `finding_summary` (by_flag, by_channel_hint, top_scopes) surfaces which channels have findings before opening the full interface list. **Critical**: do not collapse global findings from the sweep + targeted clean checks on one interface into a false "protocol is clean" verdict. State both facts: "global findings exist on W-channel, targeted Master0 R-channel checks are clean" → points to next layer (HVL/BFM). FSDB transition reads are bounded by the native output buffer; if any clock or sampled signal returns only a prefix, the row sets `transition_data_truncated=true`, the sweep increments `transition_truncated_count`, and coverage cannot be `complete`. Narrow the time window for a complete targeted check; never treat zero counts from a truncated prefix as clean.
- Do not analyze or recommend fixes before MCP output is available
- On protocol or scoreboard mismatches, carry at least two competing hypotheses and verify the opposite side with waveform evidence before assigning root cause; state which sides were checked. See `docs/workflow.md`.

## LSF-Only NPI User Setup

When a user says Verdi/NPI licenses are available only on LSF execution nodes, explain that TraceWeave remains local by default and give this direct setup (replace `digital` with the user's licensed queue):

```bash
export TRACEWEAVE_NPI_EXECUTION=lsf
export TRACEWEAVE_NPI_LSF_QUEUE="digital"
```

Do not tell the user to create or overwrite a generic `LSF_QUEUE`. TraceWeave does not read it. Only when the site already exports `LSF_QUEUE` may the user optionally map that existing value:

```bash
export TRACEWEAVE_NPI_LSF_QUEUE="$LSF_QUEUE"
```

For a terminal-launched Codex whose parent already contains the namespaced queue, forward it with `env_vars = ["TRACEWEAVE_NPI_LSF_QUEUE"]` and put `TRACEWEAVE_NPI_EXECUTION = "lsf"` under `[mcp_servers.TraceWeave.env]`. If the Codex parent does not inherit that environment, omit the queue from `env_vars` and set a fixed `TRACEWEAVE_NPI_LSF_QUEUE = "digital"` under the same `env` table. For Claude Code, put both fixed namespaced values directly in the TraceWeave server's `"env"` object. Restart or reconnect the MCP server after changing environment/configuration. The compile log, source/include inputs, TraceWeave installation, staging directory, and `TRACEWEAVE_CACHE_DIR` (including the generated KDB) must be visible at the same absolute paths on submission and compute nodes. After setup, run an explicit driver/load/path query and confirm `backend_status` reports `execution_mode="lsf"`, `scheduler_status="completed"`, `worker_status="completed"`, and `actual_backend="verdi_npi"`; otherwise inspect `fallback_reason`. On an Xcelium cache miss, also run `build_kdb` and confirm its top-level execution receipt reports the same LSF completion labels. See `README.md#lsf-only-npi-licenses` or `README.zh.md#仅执行节点可用的-npi-license` for complete bash/tcsh examples and optional settings.

## Debug Discipline

`docs/debug-discipline.md` is the module-type-agnostic debug discipline — a reusable, copy-paste prompt for any failure (protocol/bus, datapath/algorithm, or control/FSM), not only protocol mismatches. Follow it before assigning a root cause. Its through-line: good discipline, not more tool output, is what turns correct perception into a correct root cause — so ground every claim in a trace/log fact, backtrace the symptom to its originating driver, respect a tool's objective exclusions, keep two hypotheses alive and check the opposite side, and label uncertainty honestly. The doc holds the canonical five rules, maps each to the relevant TraceWeave tools, and specializes "the opposite side" per module type; the protocol-specific form of the same discipline lives in `docs/workflow.md`.

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
16. `src/connectivity_ir.py`
17. `src/connectivity_query.py`
18. `src/source_graph_adapter.py`
19. `src/source_graph_backend.py`
20. `src/source_graph_contract.py`
21. `src/source_graph_production.py`
22. `src/source_graph_runtime.py`
23. `src/source_graph_worker.py`
24. `src/slang_connectivity_projector.py`
25. `src/verdi_backend.py`
26. `src/verdi_npi_backend.py`
27. `src/npi_lsf.py`
28. `src/npi_worker.py`
29. `src/kdb_builder.py`
30. `src/waveform_batch.py`
31. `src/structural_scanner.py`
32. `src/x_trace.py`
33. `src/cycle_query.py`
34. `src/schemas.py`
35. `src/problem_hints.py`
36. `src/hierarchy_handles.py`
37. `src/handle_tools.py`
38. `src/cursor_store.py`
39. `src/timespec.py`
40. `src/verify_condition.py`
41. `src/cancellation.py`
42. `src/operation_metrics.py`
43. `src/compile_source_index.py`
44. `src/compile_source_runtime.py`
45. `src/compile_session_snapshot.py`

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
- `tests/test_npi_lsf.py`
- `tests/test_kdb_builder.py`
- `tests/test_waveform_batch.py`
- `tests/test_structural_scanner.py`
- `tests/test_x_trace.py`
- `tests/test_cycle_query.py`
- `tests/test_schemas.py`
- `tests/test_problem_hints.py`
- `tests/test_server.py`
- `tests/test_server_concurrency.py`
- `tests/test_diagnostic_snapshot.py`
- `tests/test_operation_metrics.py`

## Repository Focus

- `server.py` is the composition root and MCP entry point.
- `explain_signal_driver`, `find_signal_loads`, `trace_signal_path`, and `trace_x_source` use the production route `trusted Verdi NPI -> bounded on-demand Source Graph -> Legacy Static`. `src/source_graph_adapter.py` constructs conservative content-fingerprinted requests from the cached compile/hierarchy context without enumerating the full design. While building the full hierarchy, `src/compile_session_snapshot.py` captures private digest/stat/marker facts through the same source/include reads. The first Source Graph call reuses every still-current captured digest and hashes only unseen support inputs (`miss_reused_compile_session`); later calls may reuse a bounded process-memory manifest (`hit_session_snapshot`). A changed/incomplete capture blocks the stale pair as `compile_session_snapshot_changed`; rebuild and refresh `build_tb_hierarchy` after source changes. `src/source_graph_production.py` owns one lazy process-session runtime; the optional frontend stays in isolated one-shot workers, successful reusable IR first enters a bounded process-memory cache, and exact same-identity cold calls are single-flight under a process-wide one-build admission limit. An incomplete identity may share its live exact flight (including the same effective timeout); a successful content-anchored result may then serve exactly one next exact request through a 60-second, one-entry, 512-MiB session handoff. The handoff is consumed on lookup, never participates in dominating-scope search, retains `bypass_incomplete_key`, and never enters memory/disk cache; missing identity evidence, unsafe scope, oversize, failure, timeout, or cancellation cannot publish it. Cancelling one live waiter preserves a worker needed by others, while final-waiter cancellation stops it. Adapter/query/Static work uses the lock-free cancellable worker path and never holds an FSDB/VCD lock. The exact content-addressed disk tier is opt-in with `TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE=1` and remains default-disabled. It uses `TRACEWEAVE_CACHE_DIR/source_graph/disk-v1`, performs no startup scan, is consulted only after a memory miss, and never substitutes a persisted manifest for fresh ordered-input/options/top/compile/hierarchy content validation. A verified hit skips the frontend worker, constructs a new query engine, and enters memory; corrupt entries are safe misses. The persisted IR may contain protected-IP-derived facts, so entry permissions remain private. Cancellation terminates the request without advancing the fallback chain. Public receipts preserve selected/attempted/actual backend, fixed fallback reasons, coverage and fingerprints, plus the numeric `effective_timeout_sec`; payload facts always come from exactly one backend. For `trace_x_source`, one proved artifact serves every driver lookup in an attempt. A newly encountered X-bearing target outside that artifact triggers an exact hierarchy-ancestor-union rebuild and a restart from the original signal; no node from the smaller artifact survives.
- `src/cancellation.py` + `server._run_in_wave_thread`: wave-touching tool bodies (signal queries, search, summary, period/diff, suggest/sweep/inspect, verify_window, reconstruct_transactions) are synchronous CPU-bound scans, so `_dispatch` offloads them to a worker thread — the event loop stays free (no head-of-line blocking of light calls behind a heavy sweep) and client cancellation can actually be delivered. Parser access is serialized by wave locks acquired INSIDE the worker: one global lock for ALL FSDB work (the Verdi ffr API makes no thread-safety promise even across handles), a per-path lock for VCD; `diff_first_divergence` takes both paths' locks in a stable global order (no deadlock). `trace_x_source` is the split-phase exception: each waveform read/path-resolution phase runs through `_run_in_wave_thread`, then releases the wave lock before its connectivity-backend query. Static and Source Graph queries run in the lock-free cancellable worker; local NPI retains its existing synchronous execution model; LSF uses its existing worker path. An internal NPI fallback discards the partial chain and restarts from the original signal with Source Graph. A Source Graph blocker, build/query failure, coverage-incomplete negative, or unsafe scope explanation similarly discards its entire chain and restarts with Static. Positive edges under incomplete coverage stay partial; only a complete negative may terminate the Source Graph trace. Cancellation at any stage propagates without entering the next backend. Cancellation is cooperative: cancelling the request task (client `notifications/cancelled` or disconnect) arms a per-call `threading.Event`, and the scan loops in `cycle_query`/`handshake_sweep`/`verify_condition`/`window_verify`/`txn_reconstruct` hit `check_cancelled()` stride checkpoints (`CANCEL_CHECK_STRIDE`). Handshake discovery checks before/after every `search_signals` call and between valid/ready and AHB phases; it explicitly re-raises `OperationCancelled` instead of swallowing it in best-effort search handling. A synchronous native search is not interruptible inside that call yet, so cancellation is observed immediately after it returns. A call cancelled while still queued on a wave lock gives up without ever touching the parser. Because a client-side read timeout may not emit `notifications/cancelled`, an interactive FSDB call queued behind background `sweep_handshakes` also arms the sweep's cancel event; the sweep releases the global lock at its next checkpoint, so the light query proceeds without ever overlapping FFR access. `src/operation_metrics.py` records only whitelisted numeric timings/counts and a fixed phase label (lock wait, discovery/search costs, preemption-to-cancel latency); it never records paths, scopes, keywords, signal names, or values. Loop-side dispatch state (`_result_cache`/`_session_state`/provenance) is still written only on the event-loop thread — the worker computes, the loop remains the single writer. Regression coverage: `tests/test_server_concurrency.py` and `tests/test_source_graph_trace_public_routing.py`.
- Full-sweep operation metrics add only privacy-safe aggregates: total sweep time, planned/attempted/completed interface counts, unique clock/signal counts, aggregate/max inspect time, clock-vs-signal transition read count/total/max, edge-extraction/value-sampling time, and transition-truncated interface count. FSDB sweeps also expose aggregate native phase timings, group/fallback counts, transition/output volume, sampling shape, cache peaks, result build/serialization cost, and process RSS start/peak/end. They remain numeric/fixed-label only. They never record paths, scopes, signal names, search keywords, or values.
- `src/path_discovery.py` owns compile/sim/wave path discovery.
- `src/compile_source_index.py` + `src/compile_source_runtime.py` provide the
  transient compile-source sharing layer used by the default parallel
  `build_tb_hierarchy` / `scan_structural_risks` workflow. Exact compile
  identities single-flight one bounded preload (128 MiB / 32,768 files by
  default); both consumers reuse immutable decoded text plus its exact raw-byte
  digest/stat/marker evidence, and the final lease immediately clears all
  source bodies. Capacity/config failure is an optimization bypass, not a
  functional blocker. Cancellation of one waiter preserves other consumers;
  final-waiter cancellation stops preload. An explicit bounded bootstrap may
  join an already-active exact index but never creates a full-design preload on
  a miss. Public metrics contain numeric counters and fixed dispositions only.
- `src/compile_log_parser.py` and `src/tb_hierarchy_builder.py` drive compile-log-based hierarchy extraction.
- `src/analyzer.py` and `src/log_parser.py` contain the core failure analysis logic.
- `src/signal_driver.py` backtracks RTL drivers from waveform signal paths.
- `src/signal_load.py` resolves load/fanout for a signal — the symmetric counterpart to `signal_driver`.
- `src/connectivity_backend.py` defines the `ConnectivityBackend` protocol; `select_backend()` returns local Verdi NPI when a KDB is found, optional LSF NPI when `TRACEWEAVE_NPI_EXECUTION=lsf`, otherwise Static. NPI/worker failures degrade transparently; the dispatch layer never sees Verdi- or scheduler-specific exceptions.
- `src/verdi_backend.py` probes for Verdi KDB / license environment; emits per-simulator `kdb_hint` when KDB is missing.
- `src/npi_lsf.py` + `src/npi_worker.py` implement opt-in LSF placement for licensed Verdi/NPI work: explicit NPI driver/load/path queries plus `build_kdb` cache misses and forced rebuilds. Default execution is `local`; `lsf` reads its queue only from the namespaced `TRACEWEAVE_NPI_LSF_QUEUE`. Sites may map a team variable in shell startup (for example `export TRACEWEAVE_NPI_LSF_QUEUE="$LSF_QUEUE"`), but TraceWeave does not interpret generic scheduler variables. The parent writes a versioned request in a private shared staging directory and submits one `bsub -K` worker with `shell=False`. Connectivity workers call the exact local NPI core and never Static; only the parent falls back. KDB workers run the exact `vericom` + `elabcom` builder and never fall back to a local licensed build. Exact KDB cache hits remain local filesystem reads and submit no job. Scheduler wait runs under `server._run_in_cancellable_thread`, and cancellation/timeout uses an identity-free random job name for bounded `bkill -J` plus local waiter termination. Public receipts contain fixed `execution_mode`/`scheduler_status`/`worker_status`/`fallback_reason` labels only — never queue, host, command, or license text. The initial scope deliberately excludes the optional hierarchy source overlay so `build_tb_hierarchy` does not submit an implicit batch job.
- `src/verdi_npi_backend.py` is the NPI-backed implementation of `find_driver` / `find_loads` / `find_path`, plus `collect_instance_src_map` used by `build_tb_hierarchy` to overlay elaborated-netlist `file:line` onto compile-log-derived hierarchy nodes. Lazily loads `pynpi` from `$VERDI_HOME` and caches loaded designs across calls. Uses `NetHdl.fan_in_reg_list` to walk the elaborated netlist across instance boundaries — this is why NPI can resolve drivers that Static source-regex cannot reach. `find_path` wraps `netlist.sig_to_sig_conn_list`; Static has no equivalent and returns `unsupported_reason="static_backend_no_path_api"` (honest no-op rather than a regex approximation). **Driver-vs-loads cross-check (TB-driver misattribution guard):** when NPI's `find_driver` would report a "driver" whose raw NPI identity (modulo bit-indexing, via `_norm_raw`/`driver_is_load_alias`) is byte-identical to a LOAD of the *same* net — an interface-slice alias of the net's own consumer, or a register that reads the net — that "driver" cannot be the source (a net cannot be both driven by and read into the same pin). It then prefers a genuine RTL driver among the remaining candidates (`_loadcheck_head`), and if none exists returns an honest no-op `driver_status="testbench_driven"` + `cross_check.conflict` receipt + `unsupported_reason="driver_is_load_real_driver_is_testbench"` instead of naming the load as an `exact` driver. This kills the failure where NPI's register fan-in, unable to see a procedural UVM driver (virtual interface + clocking block), walked the net to a nearby LOAD register inside the DUT (e.g. an AHB matrix `lock_owner`) and confidently mislabeled it the driver of a master's HTRANS — pushing the model toward the interconnect instead of the TB master driver. It is FP-safe by construction: the discriminator is *byte-identical* driver==load (an aliasing artifact), so a legitimate self-referential counter (`q <= q + 1`, whose net loads into a distinct `Add`/`Assignment` cell, not the `Reg`) never matches; an initial-value block is not treated as a genuine runtime driver (`_is_genuine_runtime_driver`). **The decision is keyed on NPI's *original* `driver_list` (what it claims drives the net) and short-circuits BEFORE fan-in — this is what covers `recursive=True`. Keying it on the fan-in result instead would miss the misattribution: under `recursive=True` fan-in walks across the boundary to a downstream LOAD register (the AHB matrix `lock_owner` that merely READS the net), which lives in the net's fan-OUT, not its `load_list`, so the comparison would not match. And widening the load set to include fan-OUT is wrong — a self-counter's own `Reg` IS in its fan-out (the feedback), so that would false-positive.** Reads the net's own `load_list()` (no second dispatch). The contradiction logic is pure and unit-tested without a live KDB (incl. the `recursive=True` shape).
- `src/kdb_builder.py` provides the `build_kdb` MCP tool: when a Verdi KDB is missing (typical for Xcelium / `xrun` flows), it runs `vericom -kdb` + `elabcom -elab kdb` against the file list parsed from the compile log, caches the result under `$TRACEWEAVE_CACHE_DIR/kdb/<hash>/`, and writes a runnable `build.sh` reproducer. In `TRACEWEAVE_NPI_EXECUTION=lsf` mode, a cache miss or forced rebuild is sent through the LSF worker and cannot silently build locally; a cache hit stays local because it invokes no Verdi executable or license. The probe in `verdi_backend.py` picks up the cache transparently as `kdb_flow: "traceweave_cached"`. Default-on; opt out with `TRACEWEAVE_AUTO_KDB=0`.
- `src/waveform_batch.py` exposes `WaveformBatchReader` for time-window multi-signal reads, with FSDB and VCD implementations sharing one shape.
- `src/structural_scanner.py` and `src/x_trace.py` are first-class analysis capabilities.
- `src/cycle_query.py` provides cycle-aligned signal sampling. `get_signals_by_cycle` slices by cycle index (capped); `sample_signals_on_edges` samples every clock edge inside a *time window* (the shared substrate for window-scoped relational analysis like `inspect_handshake`). Both reuse one private edge-sampling core. A full sweep may pass a private `EdgeSamplingSession`: all interfaces in one clock group reuse the clock transition list, extracted edges, and sample-time vector; only signals with multiple consumers are cached, and a remaining-consumer count evicts each cached signal immediately after its last use. Sessions are consumed one clock group at a time so large transition lists do not accumulate across clocks. This internal path preserves standalone sampler behavior, cancellation checkpoints, transition-truncation propagation, and public schemas. It also provides `annotate_center_transients`: a pure post-process over a `get_signals_around_time` result that flags a `value_at_center` which is a **sub-cycle transient** (the unmistakable dip-and-return `X→glitch→X` signature of a combinational mux re-settling to idle for ~1ns at the clock edge), setting `transient_note` + per-signal `center_transient`/`center_settles_to`/`center_settle_ps`. `server.call_tool` runs it on every `get_signals_around_time` result so a point sample at the edge is not misread as the settled protocol value (the failure mode that led a model to blame an interconnect mux for a 1ns glitch). Zero-FP: only the dip-and-return pattern is flagged.
- `src/schemas.py` is the single source of truth for tool output contracts.
- `src/problem_hints.py` provides lightweight failure symptom annotations.
- `src/hierarchy_handles.py` owns the in-process `HandleStore` and content-addressed handle derivation. `build_tb_hierarchy` returns a slim payload + `hierarchy_handle`; the full hierarchy is registered here and resolved by the handle tools. Handles are not persisted — server restart drops them.
- `src/handle_tools.py` implements `get_tb_subtree`, `lookup_tb_files`, `find_tb_instance`, `get_tb_file_detail`, `get_tb_class_hierarchy`, `dump_tb_section` as pure functions over a resolved full hierarchy dict. `lookup_tb_files` requires at least one filter; `get_tb_file_detail` returns `did_you_mean` basename suggestions when the path is not in the compile set (multi-version safety net).
- `src/fsdb_parser.py` and `fsdb_wrapper.cpp` define the Python/native FSDB boundary. FSDB tags are **tick counts, not picoseconds** (real time = tick x header scale, read at `fsdb_open` via `ffrGetScaleUnit()`, e.g. `100fs`); every tick<->ps conversion goes through exactly two helpers in `fsdb_wrapper.cpp` (`_ToTag` floor on input / `_TagToPs` ceil on output, integer-fs internal base so sub-ps scales lose no precision) — never hand-roll a `<<32|` time conversion. An unreadable scale refuses time-based queries (`FSDB_ERR_SCALE_UNKNOWN`) instead of assuming 1ps; `get_waveform_summary` exposes `scale_unit`/`scale_fs_per_tick` as the self-check. Public `get_signal_transitions.transitions` is a strict closed-window list on both FSDB and VCD; the last value-change strictly before the start is a separate `predecessor` field. `cycle_query` must seed first-edge direction and pre-first-sample values from that field before falling back to a point query — especially inside an active FSDB group, where an independent point query would load/unload and invalidate resident group state. `get_signals_around_time` likewise keeps strict in-window transitions separate from chronological pre-window history. For full sweeps, the optional transition-group ABI adds resolved signals and calls `ffrLoadSignals()` once per bounded clock group, reads every signal independently through the existing reusable 64 MiB per-call buffer, and unloads in `finally`; the default group cap is 16 (`TRACEWEAVE_FSDB_GROUP_MAX_SIGNALS`, clamped 1..256). Oversized groups, begin errors, and old wrappers fall back to legacy per-signal loading. Cancellation and exceptions must still unload, and no native group may outlive the process-global FSDB lock. Cross-scale regression lives in `tests/test_fsdb_timescale.py` (fixtures `scale_100fs.fsdb`/`scale_1ns.fsdb`).
- `src/cursor_store.py` owns the in-process `CursorStore` — named, process-scoped time anchors (`cursor_set`/`cursor_list`/`cursor_delete`). Same lifetime semantics as `HandleStore`: not persisted, dropped on restart, no "active cursor" (references are always explicit `@name`).
- `src/timespec.py` resolves a TimeSpec (raw ps int, `@cursor` ref, or unit literal like `12.34ns`) to picoseconds. `server._resolve_time` wires it into every time-taking tool input (`get_signal_at_time`, `get_signal_transitions`, `get_signals_around_time`, `trace_x_source`, `diff_first_divergence`, `period`). Arithmetic (`@c ± cycle(clk)`) is intentionally NOT implemented yet (reserved for a future Lark grammar).
- `src/verify_condition.py` implements `diff_first_divergence`, `period`, and `inspect_handshake`; all read existing waveforms and register an evidence cursor. `inspect_handshake` reports cycle-level stalls/backpressure, payload-hold violations, and premature valid/htrans deassertion before acceptance. For AHB, derive valid with `valid_htrans` + `htrans_rule`; its payload is address-phase control only. `x_while_valid` applies only to AHB control, not literal-valid data lanes that may legally be X. Check HWDATA via the separate `hwrite` + `write_data` data-phase hold check, and only on a mechanically confirmed initiator/producer bundle. Read `protocol_semantics`, `coverage`, `accepted_before_deassert`, and `violating_signal`; coverage names only checks actually run. X-while-valid, payload-hold, and premature deassertion are one-sided producer/valid-driver violations: the ready driver is exonerated, so follow `next_actions` to the producer (master on AXI AW/AR/W, slave on R/B; AHB HTRANS is master-driven). A UVM producer may correctly resolve as `testbench_driven`; do not reinterpret a landed DUT load as its driver. A plain stall remains two-sided and targets `ready`. `diff_value_distribution` remains intentionally unregistered; do not expose it without a demonstrated use case.
- `src/window_verify.py` implements `verify_window`: templated temporal checks, not a DSL. Terms are `{signal,op,value}`, predicates are implicit AND, and modes are `always`, `never`, `eventually`, `implication`, and `sequence`. Use `overlap=false` (`|=>`, `within_cycles>=1`) for hold/stability properties; the default overlapping form can pass vacuously on the antecedent cycle, so never use a reported `vacuous=true`/`VACUOUS PASS` as exclusion evidence. `sequence.delta` supports caller-supplied `modulo` for WRAP and `restart_when` for burst starts; first/restart beats seed, gate-false waits preserve the predecessor, and X/Z breaks continuity. Unknown and end-window implication cases remain explicit (`unknown_cycles`/`inconclusive_count`). A sequence violation links its `violating_signal` to `explain_signal_driver`; protocol-side attribution and hypothesis generation remain the LLM's job.
- `src/handshake_suggest.py` provides `suggest_handshakes` (T2 of the protocol-debug plan, the "self-serve multiplier"): scans the waveform's signal universe via `search_signals` and proposes ready-to-use `inspect_handshake` bundles — pairs `*valid`/`*ready` by scope + stem, locates the clock (same scope or nearest ancestor), and groups channel payload buses (width>1, non-bookkeeping var_types, preferring the channel stem prefix). It also provides `suggest_protocol_bundles` for AHB/APB discovery: AHB returns `valid_htrans`-based `inspect_handshake` args (payload = address-phase control only — HADDR/HWRITE/HSIZE/HBURST/HPROT; HWDATA/HRDATA are excluded as data-phase, so payload-hold cannot false-positive on the address/data phase offset — HWDATA is instead surfaced as `hwrite`+`write_data` for the write data-phase hold check, but ONLY on a mechanically-confirmed initiator-side interface: on a responder/consumer interface HWDATA is a combinational interconnect-mux output that glitches to its idle value for ~1 cycle at each clock edge, which the edge sampler reads as a spurious change, so hwrite/write_data are withheld on responder/unknown interfaces to keep the check zero-FP); APB returns `psel`/`penable`/`pready` facts and marks the missing derived-valid step. AHB results also carry a `next_step` field with a copy-paste-ready `inspect_handshake(...)` call per candidate (via `_inspect_handshake_relay`) — discovery only LOCATES the interface, `inspect_handshake` is the analysis; weak models stop at discovery unless the next call is spelled out at the one point its args first exist (here, not at parse time — parse has no signal paths). Direction tags are emitted only from discovery-layer mechanical evidence and degrade to `unknown` rather than guessing. Core proposal functions are pure over `{path,name,width,var_type}` descriptors (fully unit-tested).
- `src/handshake_sweep.py` implements `sweep_handshakes`, the whole-design anomaly scan over every discovered valid/ready and AHB interface (APB lacks the required derived-valid form and is excluded). Clocking-block `*_cb` mirrors are dropped. It returns an ordered comparative fact table, not a root-cause verdict: on a backpressured pipeline the top row may be the propagation front, while the cause is at the stall-to-starvation boundary. AHB `ready_without_valid` is idle-bus behavior, so it is excluded from flags and ranking. Preserve one cursor, public schemas, shared per-clock sampling, bounded FSDB grouping, the process-global FSDB lock, and cancellation checks. Always interpret `coverage_status`: `zero_coverage` checked nothing; `truncated` exceeded `max_interfaces`; `degraded` has skipped/incomplete rows; only `complete` supports a clean conclusion. Retry only a suggested action that changes scope/window/edge/interface cap; otherwise report the missing prerequisite instead of replaying the same call.
- A bounded FSDB transition prefix sets per-row `transition_data_truncated=true`, increments top-level `transition_truncated_count`, and forces non-`complete` sweep coverage.
- `src/txn_reconstruct.py` provides `reconstruct_transactions` (the id-correlated transaction layer): walks a request handshake channel + a completion channel over the whole window, matches accepted beats by an `id` field, and returns per-transaction latency + aggregate facts. **One generic core, not a tool per protocol**: AXI read = AR→R (`cmp_last`=rlast, id=arid/rid); AXI write = AW→B (id=awid/bid) PLUS an optional unindexed W-data channel (`data_valid`/`data_ready`/`data_last` + `data_fields`; W carries no id so beats attach in order to the oldest data-incomplete request, matching real interconnect); any id'd req/resp; CHI-like. `req_id`/`cmp_id` are optional — omit both for an unindexed in-order stream (AXI-Lite, APB), which pairs requests and completions in FIFO order and reports txn id as null. AHB/APB phase tracking is otherwise out of scope. An optional `reset` (`reset_active_low`) clears in-flight state so a txn straddling reset is not a phantom hang (correctness, emits `reset_clears`); `capture_beats` (off by default → only `beat_count`) returns per-beat `data_beats[]` for data-integrity debug. An optional `req_len` (AxLEN = arlen/awlen) checks each txn's observed `beat_count` against `req_len+1`: a mismatch (early/late LAST, dropped/extra beat) is a real burst-length violation surfaced per-txn (`expected_beats`, `beat_count_mismatch`) and as `beat_count_mismatch_count` (x/z len → not checked, never a FP; with no `req_len` the count is 0 = "not checked", not a clean verdict). Facts not verdict: `latency` distribution (min/median/max/mean) not an "outlier" label; `outstanding_at_end`/`max_outstanding`/`max_outstanding_per_id`; `reorder_count` (informational, legal in AXI); `timeout_cycles`→`slow_count`; unmatched req/cmp surfaced loudly (the hang signature); one cursor (first never-completed request > peak outstanding). Out-of-order completion across ids via per-id FIFO. Returns objective facts only and leaves protocol-semantic interpretation (response-code decode, burst-type decode, outlier judgement) to the caller. Reuses `sample_signals_on_edges` + `_resolve_signal_path` + `_hs_truth`/`_hs_repr`.
- `src/usage_telemetry.py` provides passive, local-only usage telemetry: when explicitly enabled, `server.call_tool` (the single dispatch choke point) appends one JSONL line per call to `$TRACEWEAVE_CACHE_DIR/telemetry/usage.jsonl` (tool name, arg keys + whitelisted scalar flags only — never values/paths, `result_bytes` token proxy, latency, ok/blocked, and on failed calls a classification `error_code` — a code or exception class name, never the message). Long wave calls may include the privacy-safe `operation_metrics` diagnostics listed above. When the opt-in Source Graph disk cache is enabled, a second independent persistent allowlist accepts only its numeric timings/counts/bytes plus fixed phase/cache-tier/disk-validation labels; it rejects fingerprints, cache/source/wave paths, signal/scope/value content, diagnostics and exception text. `aggregate()` and the offline `scripts/telemetry_report.py` CLI report tier/tool counts, exact disk hit rate, validation outcomes, build skips, capacity/evictions and p50/p95 tier latency without scanning the artifact cache. A session is anchored to each `get_sim_paths` case identity via `note_session`. Recording is best-effort (never raises into the call path), default-off, and never an MCP tool; opt in with `TRACEWEAVE_TELEMETRY=1` and restart/reconnect the server.
- `config.py` centralizes environment-sensitive paths and behavior constants.

## Working Rule

Before making non-trivial changes, build a quick mental model from the files above instead of editing from local assumptions.

## Documentation Rule

When a behavior change requires doc updates, **only touch documents tracked in git**. Run `git ls-files | grep -E '\.md$'` to see the canonical doc set (currently `README.md`, `README.zh.md`, `AGENTS.md`, `CLAUDE.md`, `docs/architecture.md`, `docs/workflow.md`). Untracked files under `docs/` are local drafts, RFCs, and session notes — do not edit them as part of code changes and do not create new ones unless the user explicitly asks. This applies to every agent working in this repository (Claude, Codex, others).
