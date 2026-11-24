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

When a user supplies this repository URL and explicitly asks to install
TraceWeave, treat that as the repository-local full EDA profile unless the user
explicitly requests the portable PyPI profile. First run the read-only check:

```bash
bash scripts/install.sh --check
```

If the full profile is missing or incompatible, run `bash scripts/install.sh`
only as part of that requested installation. It orchestrates the existing
`setup_source_graph.sh -> setup_fsdb.sh -> verify_fsdb.sh` workflow, then checks
the repository MCP runtime. It never edits shell startup files or Codex,
Claude, or Copilot configuration; `--print-config` only prints an absolute-path
template. Do not run repository FSDB scripts to extend a PyPI installation. A
portable installation uses `pip install traceweave-mcp` or
`pip install "traceweave-mcp[source-graph]"`, does not include the FSDB wrapper,
and is not the default when the repository URL was supplied. Do not install
dependencies during ordinary analysis/debug tasks, and do not modify a user's
MCP client configuration unless the user explicitly requests that additional
action.

## TraceWeave Usage

For simulation logs and waveforms, a starting route is:

`get_sim_paths -> scan_structural_risks / parse_sim_log -> targeted queries or sweep_handshakes`

Choose tool order and investigation scope from the current run, failure phase and
known target, subject to actual input prerequisites. Recommendation can run before
a sweep. Build/elaboration diagnostics and waveforms from earlier runs retain
separate evidence identities.

Rules:

- Query dumped signals directly using their waveform paths. Before opening an RTL/TB file named in a log, verify its compiled path with `lookup_tb_files(compile_log=..., basename=...)`. Name and path filters read compile metadata.
- For connectivity and scoped hierarchy queries, pass `compile_log` or `kdb_path` + `top_hint` directly. Healthy NPI needs no full source capture. Permitted fallback prepares source server-side and returns `source_design_handle`. Explicit `design_handle` keeps strict validation. Changed or expired source observations require explicit preparation.
- Call `build_tb_hierarchy` only for an explicitly requested full RTL/TB view or legacy hierarchy handle. If the same compile log also needs a structural scan, run both in parallel.
- `scan_structural_risks` is enabled in the default workflow. The user may skip it
  explicitly or set `TRACEWEAVE_STRUCTURAL_SCAN_IN_WORKFLOW=0` in the MCP server
  environment and reconnect. With this setting, do not automatically call or
  resume the scan; explicit structural-scan requests still run normally. Skipping
  means unchecked, never passed, and does not change optional handshake investigation.
- `scan_structural_risks.analysis_mode` is per call, default `auto`. `fast` runs lexical rules only: no NPI discovery/load/LSF or Slang. `auto` first tries existing/cached KDBs through NPI, then only reuses compatible source semantic results/IR for missing categories. `deep` also permits a budgeted Slang build. NPI can request a local/LSF license; scanning never builds a KDB. Use `top_hint` for missing/ambiguous compile tops. Read `semantic.category_coverage`, `result_groups`, `native_execution` and `termination_reason`: native input-port and source domains/identities stay separate. Empty parent actuals have unverified default semantics; unknown forms and unsupported categories remain gaps. One semantic deadline covers discovery, load, queue, traversal, supplementation and validation. Cancellation, total timeout, identity or policy changes never advance fallback. `not_run`, zero facts and cache hits are not semantic passes.
- Read `lexical_coverage_status`, `semantic.status/gaps` and `semantic.propagation` separately. `semantic_scope` selects basic semantic facts; frontend elaboration may still span the compile context. Explicit `propagated_constant` / `constant_control` categories require a bounded full writer inventory even for scoped output. Missing drivers, sequential/initial state and conflicting or unmodeled writes cannot become permanent ties. Facts are investigation evidence, not confirmed defects; display truncation is distinct from analysis limits.
- Explicit `multiple_driver` / `undriven_signal` categories use NPI or Slang bit-level static inventories; defaults stay unchanged. Multiple known sources are positive evidence, not runtime contention. Undriven facts require a complete empty inventory for consumed bits; unknown/limited inventories never prove absence. Slang inventories all elaborated tops even for scoped output. Read `target_bits`, `driver_inventory_complete`, category coverage and gaps; native continuation also preserves signal/bit positions.
- For bounded native scans, inspect `result_groups[].gap_summary` and `scan_progress`. Continue pending work with `semantic.suggested_next_action` / `semantic_continuation`, preserving log/top/scope/categories; do not restart the same prefix. Counts are cumulative, facts belong to the current page. Tokens expire after 10 idle minutes or server restart. Frontier completion does not clear unsupported-expression/category gaps; if continuation is blocked, report the reason and choose a relevant smaller scope rather than looping.
- Structural scans cache exact source/context results before output trimming. Scoped deep scans may publish compatible compact IR for Source Graph queries through the normal identity/scope/capability checks; `semantic.query_artifact_status` reports reuse or bypass. A scan runs independently of the full hierarchy and does not force full-design IR. NPI retains its KDB and priority; scan/Source Graph facts do not become NPI facts. See `README.zh.md` and `docs/architecture.md` for per-call examples and budgets.
- `sweep_handshakes` is optional: consider it for timeout/hang or compare symptoms when the responsible interface is unknown. Use a known module scope/time window, or inspect a known interface/signal directly. An unperformed sweep means unchecked, and never blocks other investigation. It returns AHB and valid/ready facts, not a verdict. Always inspect `coverage_status`: `zero_coverage` checked nothing; `truncated`/`degraded` is partial, so `flagged_count=0` is not clean. Retry only a `suggested_next_action` that changes scope/window/edge/interface cap; otherwise report the missing prerequisite instead of replaying the same call. Use `finding_summary` before opening all rows. Never collapse global findings plus a clean targeted interface into “protocol clean”; state both. Any `transition_data_truncated` row forces non-complete coverage; narrow the time window and never treat prefix zero counts as clean.
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
36. `src/hierarchy_provider.py`
37. `src/hierarchy_handles.py`
38. `src/handle_tools.py`
39. `src/cursor_store.py`
40. `src/timespec.py`
41. `src/verify_condition.py`
42. `src/cancellation.py`
43. `src/operation_metrics.py`
44. `src/compile_source_index.py`
45. `src/compile_source_runtime.py`
46. `src/compile_session_snapshot.py`
47. `src/design_identity.py`
48. `src/structural_scan_runtime.py`
49. `src/structural_semantics.py`
50. `src/structural_semantic_runtime.py`
51. `src/structural_semantic_worker.py`
52. `src/structural_artifact.py`
53. `src/structural_propagation.py`
54. `src/structural_semantic_router.py`
55. `src/structural_native_runtime.py`
56. `src/npi_structural_scan.py`
57. `src/npi_structural_lsf.py`

If the task involves FSDB or native integration, also read:

- `native/fsdb/fsdb_wrapper.cpp`
- `native/fsdb/fsdb_point_read.h`
- `scripts/build_wrapper.sh`

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
- `tests/test_structural_scan_runtime.py`
- `tests/test_structural_semantics.py`
- `tests/test_structural_artifact.py`
- `tests/test_structural_propagation.py`
- `tests/test_x_trace.py`
- `tests/test_cycle_query.py`
- `tests/test_schemas.py`
- `tests/test_problem_hints.py`
- `tests/test_server.py`
- `tests/test_server_concurrency.py`
- `tests/test_diagnostic_snapshot.py`
- `tests/test_operation_metrics.py`

## Repository Focus

Detailed implementation contracts and resource limits live in `docs/architecture.md`;
workflow and evidence interpretation live in `docs/workflow.md` and
`docs/debug-discipline.md`. Keep this file focused on invariants that edits must preserve.

- `server.py` composes tools and owns dispatch/session publication on the event loop.
  `native_query.py` / `native_dispatch.py` route compile metadata;
  `source_fallback_runtime.py` retains bounded references and live flights.
- Connectivity routes through trusted Verdi NPI, bounded on-demand Source Graph,
  then Legacy Static. Facts belong to one backend/artifact; receipts retain attempts,
  fixed reasons, fingerprints, coverage and effective timeout. Source association
  is separate from KDB identity. Cancellation never advances fallback.
- Source Graph uses isolated workers, bounded compact IR, one-build admission and
  exact-key single-flight. Rebuild a legacy hierarchy when its compile snapshot
  has changed or is incomplete. An incomplete key shares only its live flight,
  with at most one consumed-once 60s/512MiB exact handoff: never cache it or broaden its scope.
  Persistent semantic sessions are default-off and require the soak gate. The
  optional disk cache does no startup scan, validates fresh ordered inputs/options/
  tops/snapshots before every hit, keeps entries private and treats corruption as a miss.
- `cancellation.py` and `_run_in_wave_thread` keep wave scans off the event loop.
  Acquire locks inside workers: one process-global FSDB lock, one per VCD path,
  dual-path locks in stable order. Cancelled waiters never touch parsers. Interactive
  FSDB work may preempt background sweeps at checkpoints, never overlap FFR access.
  Native calls checkpoint on return; do not promise interruption inside them.
  Static/Source Graph work is lock-free and cancellable; local NPI remains synchronous,
  LSF uses workers. Preserve `test_server_concurrency.py` and public trace routing tests.
- `trace_x_source` releases wave locks before connectivity work. Backend/artifact
  changes discard the partial chain and restart at the original signal; new X targets
  outside an artifact rebuild the exact ancestor union. Incomplete positive edges
  remain partial; only complete negatives terminate. Bounded driver traversal stops
  with `driver_traversal_incomplete`, never an exclusive root-cause claim.
- `operation_metrics.py` and telemetry accept only whitelisted numeric aggregates
  and fixed phases/labels, never paths, scope/signal names, values, commands, hosts,
  queues or license/exception text. Preserve aggregate sweep/native timings, volume,
  cache/RSS measurements and their privacy boundary; see architecture for the allowlist.
- `path_discovery.py` owns compile/sim/wave discovery; `compile_log_parser.py` parses
  compile evidence. `analyzer.py` / `log_parser.py` own failure analysis;
  `signal_driver.py` / `signal_load.py` are symmetric driver/load resolvers.
- `compile_source_index.py` / `compile_source_runtime.py` share a transient exact
  compile preload between parallel hierarchy/scans (default 128MiB/32,768 files).
  Text and raw digest/stat/marker evidence are immutable; final lease release clears
  source bodies. Capacity/config failure bypasses this optimization. One waiter
  cancellation preserves others; final-waiter cancellation stops preload. Bounded
  bootstrap may join an active exact index but cannot create a full-design preload.
- `tb_hierarchy_builder.py` preserves the nested-dict schema while repeated module/UVM
  descendants share immutable child mappings. Stats distinguish logical/physical
  nodes, allocations and reuse. Only complete positive local instance edges enter
  the tree: unresolved generate, instance-array and bind edges stay diagnostic;
  duplicate definitions stop at ambiguity. Parameter overrides retain safe direct
  edges while Slang owns specialization. Relevant gaps become Source Graph objective
  exclusions; parameter-only gaps are informational. Optional NPI source overlays
  detect aliases and copy on write so annotations never bleed across instances.
  Hierarchy materialization and real-design A/B benchmark scripts are the scaling oracles.
- `hierarchy_provider.py` offers exact child lookup over compile trees, immutable
  Connectivity IR indexes and an explicit development NPI differential provider.
  NPI prefix lookup is capped before load (default 256, max 1,024), never walks tops/
  siblings and always reports `npi_hierarchy_fragment_bounded`. IDs are design scoped.
  Basic hierarchy builds neither import Slang nor invoke a native semantic provider.
- `connectivity_backend.py` selects local/LSF NPI for an existing KDB, otherwise Static.
  Backend-specific failures degrade through the parent router. `verdi_backend.py`
  discovers KDB/license candidates and simulator-specific missing-KDB hints.
- `npi_lsf.py` / workers use only `TRACEWEAVE_NPI_EXECUTION` and namespaced
  `TRACEWEAVE_NPI_LSF_QUEUE`; never interpret generic scheduler variables.
  Versioned requests use private shared staging and `bsub -K` with `shell=False`.
  Workers execute their exact native operation, never Static/Slang or parent fallback.
  Cancellation/timeout uses a random identity-free job name for bounded `bkill -J`
  and waiter termination. Parent waits use cancellable workers. Full hierarchy's
  optional source overlay must not implicitly submit jobs.
- `kdb_builder.py` explicitly runs `vericom -kdb` + `elabcom`, caches under
  `$TRACEWEAVE_CACHE_DIR/kdb/<hash>/` and writes `build.sh`. In LSF mode all cache
  misses/forced rebuilds run remotely; exact hits remain local filesystem reads.
  Preserve auto-KDB opt-out and cached-KDB discovery. Analysis must not rebuild an
  original user project to obtain a missing KDB.
- `verdi_npi_backend.py` lazily loads pynpi and reuses bound designs. Preserve the
  testbench-driver guard: compare ORIGINAL `driver_list` against the SAME net's
  `load_list`, normalizing bit indexes only, before recursive fan-in. A matching
  consumer alias is not a source; prefer a genuine runtime driver, otherwise return
  `testbench_driven`, `cross_check.conflict`, and
  `driver_is_load_real_driver_is_testbench`. Initial-value blocks do not qualify.
  Never use recursive fan-in or fan-out for this decision (downstream alias misses /
  self-feedback false positives). Cover the logic without KDB, including recursive=True.
- Recursive native fan-in uses the official global FAN_IN callback, capped at 4,096
  states / 32 public facts. Serialize registration/traversal/reset and reset on every
  exit. Missing callback support never enables unbounded traversal. NPI/Source Graph
  share the additive traversal receipt: positive prefixes remain authoritative partial
  facts; zero-fact incomplete negatives continue fallback.
- `connectivity_query.py` uses deterministic BFS with one selection/predecessor per
  discovered state and reconstructs once with checkpoints. Never queue full path
  prefixes; preserve ordering, caps, coverage, schemas and fingerprints.
- `waveform_batch.py` gives FSDB/VCD window reads a shared shape. `schemas.py` is
  the public output contract; `problem_hints.py` adds lightweight symptom annotations.
- FSDB tags are TICKS, not ps. Read scale at open through `ffrGetScaleUnit()`.
  All conversions use `_ToTag` (floor input) and `_TagToPs` (ceil output) in
  `native/fsdb/fsdb_wrapper.cpp`, using integer fs internally; never hand-roll shifts.
  Unknown scale rejects time queries with `FSDB_ERR_SCALE_UNKNOWN`. Summary exposes
  scale; preserve cross-scale regressions in `tests/test_fsdb_timescale.py`.
- FSDB/VCD transition lists are strict closed windows; predecessor is separate.
  `cycle_query.py` seeds first-edge direction/pre-sample values from predecessor
  before point queries, especially in active FSDB groups where independent reads
  invalidate resident state. Around-time likewise separates in-window and prior history.
  Compatibility groups load once, use the 64MiB buffer and unload in finally, inside
  the global lock. Default group cap 16 (clamped 1..256); oversized/unsupported/error
  groups fall back to per-signal loading. Cancellation must still unload.
- Cycle sweeps reuse one `EdgeSamplingSession` per clock group, cache shared signals
  only and evict after final use; groups stay serial. Around-time transient annotation
  flags only dip-and-return X→glitch→X, never arbitrary edge changes.
- `fsdb_sweep_stream.py` limits pages to 1,024 records/256KiB and blocks to 2,048
  edges/8MiB logical bytes. `HandshakeConsumer` retains protocol state, findings and
  counters across blocks; finish at request end or explicit stop. Preserve raw-fs /
  strict-before ordering, same-tick groups, cancellation and global lock. Work caps
  are 50M records/32GiB logical work/300s; stops stay partial. Unsupported groups
  use compatibility sampling. Native FFR session RSS may still grow.
- Hierarchy handles and cursor anchors are process scoped, never persisted.
  `handle_tools.py` resolves registered hierarchy dictionaries. File lookup requires
  a filter; detail lookup suggests basenames for wrong compiled versions. Cursors
  are always explicit @name references, with no active cursor. `timespec.py` resolves
  ps integers, units and cursors; cursor/cycle arithmetic remains unimplemented.
- `verify_condition.py` provides divergence/period/handshake facts and evidence cursors.
  AHB address payload excludes HWDATA/HRDATA; write-data holds require a confirmed
  initiator bundle. X-while-valid applies to AHB control, not literal-valid data.
  Payload hold, X control and premature deassertion are producer violations and
  exonerate ready; a plain stall stays two-sided. Read coverage, protocol_semantics,
  accepted_before_deassert and violating_signal. UVM producers may be testbench driven;
  do not reinterpret DUT loads as sources. Do not register diff_value_distribution
  without a demonstrated use case.
- `window_verify.py` uses templated predicates/modes, not a DSL. Hold/stability needs
  overlap=false and within_cycles>=1; vacuous passes never exclude a hypothesis.
  Sequence modulo/restart/gating preserves predecessor semantics; X/Z breaks continuity.
  Unknown/end-window cases remain explicit. Driver links are evidence, not attribution.
- `handshake_suggest.py` discovers bounded scope pages (legacy search fallback).
  Pair valid/ready by scope/stem and mechanical direction; ambiguous clocks/payload
  stay unresolved. AHB uses HTRANS address controls only, offers HWDATA checks only
  on confirmed initiators, and includes a ready-to-call inspect relay. APB discovery
  does not pretend the missing derived-valid check ran. Discovery locates; inspection
  analyzes. Req/ack requires explicit hold semantics. Never infer direction from names.
- `handshake_sweep.py` scans AHB and valid/ready with one shared discovery snapshot
  and one cursor; it excludes clocking mirrors, APB and legal idle-ready flags/ranking.
  Top-ranked stalls may be propagation fronts, not causes. Preserve bounded pages,
  exact scope/index identity, total versus lower bound, memory/visit caps and fallback.
  zero_coverage is not pass; truncated/degraded stays partial. Transition prefixes force
  non-complete coverage and transition_truncated_count. Retry only with changed scope/
  window/edge/cap. Shared sampling, FSDB groups, lock and cancellation remain mandatory.
- `txn_reconstruct.py` uses one generic id-correlated request/completion core, with
  optional in-order W data for AXI writes and FIFO matching when both ids are absent.
  Preserve reset-cleared in-flight state, optional beat capture, per-id out-of-order
  completion and one cursor prioritizing uncompleted requests. Req_len checks beat_count
  against len+1; unknown/absent len is not checked, not clean. Latency/reorder/outstanding
  are facts, not outlier/legal/root-cause verdicts. Reuse existing edge sampling helpers.
- `usage_telemetry.py` is local-only, best-effort, default-off; opt in with
  TRACEWEAVE_TELEMETRY=1 and reconnect. No argument values or exception text enter JSONL.
  `telemetry_context.py` pins exact artifact/stat ownership, rechecks at completion and
  never assigns unknown/conflicting calls to the latest case. Retention caps 128 owners /
  4,096 bindings, then unknown until restart. Readback counts do not prove model use;
  unknown calls have no session denominator. Session opportunities are upper bounds,
  not semantic reuse proof. `config.py` centralizes environment-sensitive behavior.

## Working Rule

Before making non-trivial changes, build a quick mental model from the files above instead of editing from local assumptions.

## Documentation Rule

For product doc updates, **only edit documents tracked in git**. Use
`git ls-files '*.md'` to inspect the canonical set; under `docs/`, only
`architecture.md`, `expressions.md`, `debug-discipline.md` and `workflow.md`
are public. Ignored task files are local drafts and evidence; edit or create
them only when the user requests local records. This applies to every agent.

Commit only necessary product docs: stable behavior, interfaces, usage and limits.
Keep development plans, dated measurements, profiling breakdowns, experiments,
rejected approaches and session notes in ignored task directories under `docs/`;
never include them in tracked docs or force-add them when committing code.

`CLAUDE.md` links to `AGENTS.md`; after changing either, resolve the target and require `wc -m` to be at most 40,000 characters.
