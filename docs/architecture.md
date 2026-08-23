# Architecture

## System Shape

TraceWeave is a workflow-oriented debug server. The core architecture is not
just "parse log + parse wave"; it combines workflow gating, source-aware
analysis, waveform backends, and extended debug capabilities.

## Layering

```text
MCP interface and workflow gate
  server.py
  - tool registry and schema
  - session state / prerequisite checks
  - diagnostic snapshot and result caching

Core log and failure analysis
  src/path_discovery.py
  src/compile_log_parser.py
  src/log_parser.py
  src/analyzer.py

Source-aware structure analysis
  src/tb_hierarchy_builder.py
  src/signal_driver.py
  src/signal_load.py

Connectivity backends (driver/load/path resolution)
  src/connectivity_backend.py     # protocol + Static + select_backend
  src/source_graph_adapter.py     # compile/hierarchy identity + proved query scope
  src/source_graph_compile_projection.py # large-manifest dependency closure planner
  src/source_graph_runtime.py     # isolated worker + bounded memory/disk lifecycle
  src/source_graph_worker.py      # optional Slang frontend process
  src/verdi_npi_backend.py        # Verdi NPI backend, lazy, license-tolerant
  src/npi_lsf.py                  # optional LSF transport + exact worker protocol
  src/npi_worker.py               # short-lived compute-node NPI entry point
  src/verdi_backend.py            # KDB / license probe, kdb_hint generator
  src/kdb_builder.py              # Auto-build Verdi KDB (vericom + elabcom) for Xcelium

Waveform backends
  src/vcd_parser.py
  src/fsdb_parser.py
  src/fsdb_signal_index.py
  src/cycle_query.py
  src/waveform_batch.py           # FSDB+VCD batch reader (time-window)
  src/cancellation.py             # cooperative cancel checkpoints for worker-thread scans
  src/operation_metrics.py        # privacy-safe lock/discovery/cancel timings

Extended analysis capabilities
  src/structural_scanner.py
  src/x_trace.py

Auto-debug primitives (cursors + verification)
  src/cursor_store.py             # named, process-scoped time anchors (cursor_set/list/delete)
  src/timespec.py                 # resolve @cursor / unit literals (12.34ns) to ps on time inputs
  src/verify_condition.py         # diff_first_divergence, period, inspect_handshake (registered);
                                  # diff_value_distribution (implemented, NOT registered)
  src/window_verify.py            # verify_window: temporal predicate over a clock window
  src/handshake_suggest.py        # suggest_handshakes / suggest_protocol_bundles
  src/handshake_sweep.py          # sweep_handshakes: whole-design handshake anomaly sweep
  src/txn_reconstruct.py          # reconstruct_transactions: id-correlated transaction layer

Native integration
  libfsdb_wrapper.so
  fsdb_wrapper.cpp
  Verdi ffrAPI/libs or repo-local runtime symlinks

Config and support
  config.py
  custom_patterns.yaml
  src/problem_hints.py
  src/schemas.py

Verification
  tests/*
```

## Notes

- `server.py` is both the composition root and the workflow gate; tool ordering,
  prerequisite enforcement, session-compatible cache reuse, and in-process
  parsed-log snapshots for same-path simulation reruns live there.
- Sibling-file rerun hints are conservative and bounded: `src/log_parser.py`
  samples only fixed-size head/tail windows, rejects compile/elaboration/build
  names and compiler-only content, and ranks evidence-backed simulation logs by
  filename affinity before mtime. Same-path snapshots remain independent of
  sibling-file discovery.
- Structural scan results carry an additive coverage receipt. Only
  `coverage_status=complete` with `total_risks=0` supports a clean-scan
  observation; `zero_coverage` and `degraded` explicitly preserve uncertainty
  when no supported source or only a partial/parser-degraded source set was read.
- Protocol coverage and retry actionability are separate. Server routing relays
  only a sweep action that expands scope, narrows the time window, changes the
  edge, or raises a truncated interface cap. An unscoped zero-interface sweep
  and a degraded sweep with no such action retain their warning/coverage receipt
  but do not generate an identical required call. Recommendation output carries
  `runtime_protocol_coverage` even when no interface finding exists.
- Wave-touching tool bodies (`get_signal_*`, `get_signals_*`, `search_signals`,
  `get_waveform_summary`, `period`/`diff_first_divergence`,
  `suggest_*`, `sweep_handshakes`, `inspect_handshake`, `verify_window`,
  `reconstruct_transactions`) are synchronous and CPU-bound, so `_dispatch`
  runs them in a worker thread via `_run_in_wave_thread` instead of inline in
  the async coroutine — otherwise one heavy scan starves the event loop
  (head-of-line blocking of every queued request) and client cancellation can
  never be delivered. Parser access is serialized by wave locks acquired
  inside the worker: one global lock for ALL FSDB work (the Verdi ffr API
  makes no thread-safety promise even across handles), a per-path lock for
  VCD. Cancellation is cooperative (`src/cancellation.py`): the dispatch layer
  arms a per-call `threading.Event` when the request task is cancelled
  (client `notifications/cancelled` or disconnect), and the scan loops in
  `cycle_query` / `handshake_sweep` / `verify_condition` / `window_verify` /
  `txn_reconstruct` call `check_cancelled()` at stride checkpoints. Handshake
  discovery also checks immediately before and after every `search_signals`
  call and between its valid/ready and AHB phases; `OperationCancelled` is
  explicitly re-raised rather than swallowed by best-effort discovery error
  handling. Thus an abandoned multi-minute sweep stops at the next Python
  checkpoint. A synchronous native search cannot yet be interrupted from
  inside that call; cancellation is observed as soon as it returns. A call
  cancelled while still queued on a wave lock gives up
  without ever touching the parser. A client-side read timeout is not
  guaranteed to emit an MCP cancellation notification, so an interactive
  FSDB call waiting behind background `sweep_handshakes` also arms the sweep's
  cooperative cancel event. The sweep releases the global lock at its next
  checkpoint and the interactive call proceeds; FFR access remains globally
  serialized and never overlaps. Loop-side state (`_result_cache`,
  `_session_state`, provenance) is still written only on the event-loop
  thread; the worker computes, the loop remains the single writer.
  `trace_x_source` is the deliberate split-phase exception: its async
  orchestrator takes the wave lock only for value reads and upstream-path
  resolution, releases it before every connectivity-backend query, and then
  merges those facts into the next X/Z frontier. Static connectivity scans and
  `scan_structural_risks` run in lock-free cancellable workers so they do not
  block the event loop; local NPI retains its existing synchronous execution
  model, while LSF keeps
  using its existing worker path. If NPI internally falls back on any driver
  lookup, the partial chain is discarded and the whole trace restarts with a
  bounded Source Graph artifact. The same whole-trace restart occurs when a
  degraded KDB returns an unresolved/non-positive driver result: a partial
  elaboration can prove a returned edge but cannot prove that no omitted edge
  exists. Every Source Graph node in one attempt is
  supported by that single proved artifact. A newly discovered target outside
  its projection expands only the exact hierarchy ancestor union and restarts
  from the original signal; the smaller artifact's chain is discarded. An
  unsafe/inconclusive negative or build/query blocker restarts the whole trace
  with Static, so one returned propagation chain never mixes provenance.
  `backend_status` records selected versus actual backend and the execution
  receipt; `trace_restarted` records whether that whole-trace retry occurred.
  Driver-level NPI evidence (`source_line`, `testbench_driven`, and its
  driver-vs-load cross-check) remains attached to the terminal trace node.
  The public driver/load/path tools have a separate production orchestrator:
  trustworthy NPI results return directly; NPI unavailability/failure defers
  to an on-demand Source Graph; a Source Graph blocker or inconclusive no-match
  triggers a whole-result Legacy Static recomputation. The Source Graph runtime
  is created lazily once per server process, keeps a bounded in-memory scoped
  IR cache, admits at most one cold build per process, and executes its optional
  frontend in an isolated one-shot worker. An opt-in, default-disabled semantic
  session can instead retain one exact, bounded Slang compilation/root in that
  isolated child and project several narrow scoped IR artifacts without a
  second parse/elaboration. The broader proved context is part of artifact
  build semantics, but only compact scoped IR is cacheable; AST state never
  enters the server or disk. Context changes restart the child. Idle TTL, live
  and reported-peak RSS caps, timeout, cancellation, crash, and protocol errors
  destroy the whole session before any partial artifact can publish. A runtime
  frontier outside the context takes the historical one-shot route without
  discarding the still-useful parent session. An opt-in, default-disabled disk
  tier stores only canonical ConnectivityIR JSON plus a versioned manifest
  under `TRACEWEAVE_CACHE_DIR/source_graph/disk-v1`. It performs a direct
  exact-identity lookup only after a memory miss, never scans at startup, and
  never bypasses fresh adapter content validation. A verified hit constructs
  an independent query engine; corruption is a safe miss and failed/cancelled
  builds are never published.
  In-memory dominance is proved across dependency-closure identities when the
  immutable full design identity is exact, the available ordered projection
  inputs contain the requested projection, and the available explicit
  hierarchy scope dominates the requested scope with identical objective
  exclusions. This is one-artifact reuse, not IR composition: no facts from two
  builds are merged. A projection subset cannot dominate a larger request, and
  an artifact with a compile projection must carry the
  `compile_projection_pruned_inputs` exclusion. Its coverage therefore remains
  inconclusive and reuse is limited to proved positive facts. Disk lookup stays
  exact-only with no index scan.
  Exact overlapping preparations use a process-local flight identity over the
  artifact digest and effective worker timeout. This also coalesces an
  incomplete/non-cacheable identity while the worker is live, without upgrading
  it to memory or disk reuse. A successful content-anchored incomplete result
  may publish one exact, one-shot session handoff for the next request; the
  handoff is consumed on lookup, is never considered for dominating-scope reuse,
  expires after 60 seconds, and is bounded to one entry and 512 MiB. It retains
  `bypass_incomplete_key` semantics and is identified separately as the
  `handoff` tier. Missing/changed identity evidence, unsafe scope, capacity,
  failure, timeout, and cancellation cannot publish it. The flight itself is
  removed on success, failure, timeout, or final-waiter cancellation. One waiter
  cannot cancel a worker still needed by another. The finite validated timeout remains configurable through
  `TRACEWEAVE_SOURCE_GRAPH_TIMEOUT` and is echoed numerically as
  `source_graph.effective_timeout_sec`.
  Adapter and graph queries use the
  lock-free cancellable worker path, so neither build nor query holds a waveform
  lock or blocks light event-loop calls. Cancellation terminates the request
  without entering the next fallback. `backend_status` preserves the ordered
  attempt chain, fixed fallback/blocker labels, coverage and fingerprints while
  the result payload contains facts from exactly one backend. For
  `trace_signal_path`, the adapter proves both endpoint ancestor chains share a
  top and projects only their ancestor union through the LCA. Artifact identity
  is target-independent, so a dominating proved artifact may serve a covered
  endpoint while QueryIdentity remains target-specific.
  The deterministic shortest-hop query traverses only supported structural IR
  bindings and combinational dependencies. Positive partial results remain
  partial, while only complete coverage can establish `not_connected`;
  inconclusive/truncated negatives fall through to Static's structured
  unsupported result. `expand_assigns` changes only whether real assignment
  evidence is exposed. `trace_x_source` uses the same memory-first artifact
  runtime and optional exact disk tier while retaining split-phase waveform
  locking and whole-trace restart semantics.
  Driver/load traversal has a second resource boundary independent of artifact
  preparation: 4,096 states, 16,384 inspected IR edges, 256 unique matches,
  and 4,096 expansion frontiers by default. Index lists are canonicalized once
  when the query engine is constructed; state/edge loops contain cooperative
  cancellation checkpoints. A state, edge, match, or frontier cap adds a
  fixed `query_*_limit` gap and makes coverage inconclusive. Positive facts
  survive with their fact confidence, but a capped result is non-exhaustive and
  cannot prove uniqueness or absence. No work-limit frontier is fed back into
  scope expansion, preventing a same-artifact rebuild loop. The fixed-budget
  mapping is versioned independently from artifact identity, so existing IR
  cache entries remain valid while new query semantics apply consistently.
  Hierarchical endpoint resolution walks dotted prefixes right-to-left and
  probes the instance dictionary, making lookup proportional to path depth
  rather than projected instance count. Ordered wide-bit mappings remain
  tuples for exact ascending-range/concat semantics, but per-match membership
  indexes are built once instead of once per candidate bit. Neither optimization
  changes the IR or public result schema; interval/segment storage remains a
  separately measured future migration rather than a prerequisite for indexed
  warm queries.
  The two default compile-source consumers use a separate process-session
  `CompileSourceIndexRuntime`. An exact compile-log snapshot, simulator,
  ordered source list, and resource policy identify one active session.
  Concurrent hierarchy and structural calls single-flight a bounded preload,
  then reuse immutable decoded text and digest/stat/marker facts derived from
  the same raw bytes. The final lease clears source bodies immediately; no
  handoff or disk tier retains them. A whole-set capacity miss bypasses sharing
  rather than publishing a partial preload. Cancellation follows waiter
  ownership: one cancelled waiter does not stop work needed by another, while
  the final waiter arms cooperative cancellation. A bounded bootstrap is
  reuse-only: it may join an active exact index but never starts a full-design
  preload itself.
  While hierarchy preprocessing already has source/include bytes open, it
  captures a private immutable compile-session snapshot of digest, stat, size,
  and fixed-label marker facts; source bodies are not retained. On the first
  request for that hierarchy handle, the adapter reuses records whose full stat
  identity is still current and hashes only unseen support inputs. Replay-only
  simulator/frontend tool-library inputs (such as the `uvm_pkg.sv` expansion
  of VCS `-ntb_opts uvm`) are fresh-hashed support facts because they did not
  participate in project hierarchy construction; every original project input
  remains snapshot-required and fail-closed. A bounded process-memory manifest
  cache then serves later requests. Its key includes
  compile-log metadata, normalized parser output, and the hierarchy content
  snapshot fingerprint, matching the handle's rebuild boundary. Re-running the
  compile and `build_tb_hierarchy` invalidates it. Metadata is checked around
  both capture and manifest construction. A changed record produces the fixed
  `compile_session_snapshot_changed` blocker rather than mixing a stale
  hierarchy with fresh source content.
  On a large, complete Verilog/SystemVerilog manifest,
  `source_graph_compile_projection.py` may replace full frontend replay with an
  ordered hierarchy-dependency closure. It seeds the exact ancestor module /
  interface definitions plus every explicit compile top (including bind tops),
  then closes over exact package providers and compile-order macro state
  mutations (`define`, `undef`, `undefineall`, and conditional uses). A
  simulator-added tool package can be recovered by basename only when that
  input was absent from the project hierarchy scan. Ambiguous definitions,
  unresolved imports, duplicate/canonical-colliding inputs, incomplete or VHDL
  manifests, missing scan evidence, and insufficient reduction all retain the
  historical full replay. Admission is bounded: at least 64 full inputs and 32
  exclusions are required, the closure must contain at most 512 inputs and no
  more than half of the manifest.
  The planner never replaces Slang. The complete manifest, all content digests,
  options, tops, and compile/hierarchy snapshots remain in artifact identity;
  changing an omitted file still invalidates the artifact. The projection is
  also part of build semantics, so exact/dominating cache reuse cannot mix two
  different closures. The worker preserves selected input order, compiles the
  seeded definitions, and elaborates only the hierarchy-selected top. An
  applied projection is always accompanied by the objective exclusion and gap
  `compile_projection_pruned_inputs`. Consequently a proved positive edge or
  path is usable, but coverage remains `inconclusive` and no negative claim is
  complete. Privacy-safe adapter telemetry reports only mode and aggregate
  input/exclusion/seed/dependency counts plus a fixed fallback reason.
  Deep single-endpoint queries have a bounded first-artifact admission step.
  A recursive driver or load depth above one may include the target leaf's
  hierarchy-proved adjacent siblings before launching the worker, but only for
  a dependency-projected large manifest. Admission requires no more than 32 new
  instances, no more than 24 added ordered closure inputs, and no more than 25%
  input growth once the base closure contains at least 32 inputs; the configured
  frontier instance cap is an additional bound. Full-manifest, shallow,
  bootstrap, unresolved, or over-budget cases retain the exact ancestor plan.
  The admitted parent is carried as a private expansion anchor into any later
  runtime frontier union. Thus a second expansion cannot shrink the first
  artifact's scope, while the returned payload still comes wholly from the
  final artifact. Coverage boundaries and public contracts are unchanged.
  A split VCS build may add ordered `supplementary_compile_logs` at the
  hierarchy boundary. Parse results are merged with separate phase commands;
  the handle/snapshot identity covers every log while one-log callers retain
  their old identity. Mixed-language manifests retain VHDL files in that
  identity but send only Verilog/SystemVerilog inputs to Slang. The worker marks
  VHDL as `opaque_vhdl_boundary`: blocking frontend diagnostics or missing VHDL
  projection make negative claims inconclusive, but an IR-proved positive fact
  still returns from Source Graph and does not enter Static.
  Privacy-safe operation metrics make full-sweep cost attributable without
  recording project identities: discovery/search timing, total sweep time,
  planned/attempted/completed interface counts, unique clock/signal counts,
  aggregate/max inspect time, clock-vs-signal transition read count/total/max,
  edge-extraction/value-sampling time, shared-clock/shared-signal reuse-hit
  counts, and transition-truncated interface count. The FSDB path additionally
  reports aggregate native lookup/load/seek/traverse/unload phases, group-load
  use/fallback counts, transition/output volume, sampling shape, cache peaks,
  result build/serialization cost, and process RSS start/peak/end. All fields
  are numeric or fixed-label aggregates; paths, scopes, signal names, values,
  and search keywords are never recorded.
- A full `sweep_handshakes` does not independently reread and re-extract the
  same clock for every interface. `handshake_sweep` groups discovered bundles
  by clock and creates one private `EdgeSamplingSession` per group;
  `cycle_query` reads the clock transitions, extracts edges, and builds sample
  times once, then reuses them for every interface in that group. Signals used
  by more than one interface are also reused, with a remaining-consumer count
  that evicts each transition list immediately after its final consumer.
  Unique payload signals are never retained. Groups are consumed one at a time,
  so the implementation does not keep all design clocks in memory. This is an
  internal execution optimization: MCP inputs/results, coverage semantics,
  cancellation checkpoints, and the process-global FSDB lock are unchanged.
  FSDB and VCD transition lists obey the same strict closed-window contract.
  Each parser returns the last value-change before the window separately as
  `predecessor`; edge extraction seeds its previous value from that receipt so
  an edge exactly at the window start is not lost. Signal sampling consumes the
  same receipt before considering a point-query fallback, which is required to
  keep an active FSDB transition group resident (a nested point query would
  otherwise load/unload native signals mid-group). Around-time history is also
  separated from the strict window and normalized to chronological order.
  `scripts/benchmark_sweep_shared_clock.py` is the reproducible structural
  benchmark for this path. On a warmed generated VCD with 32 independent
  valid/ready interfaces, one shared clock, 20,000 cycles, and three repeats,
  the same workload measured 28,492.5 ms median before grouping and 13,986.8 ms
  after (50.9% lower). Clock reads/edge extractions fell from 32 to 1; maximum
  incremental `tracemalloc` peak changed from 32.45 to 33.13 MiB (+0.68 MiB,
  +2.1%). This validates the repeated-clock optimization, not a 5-minute promise
  for a proprietary FSDB whose native signal reads may have a different cost
  profile.
- Full sweeps use a private column-oriented sampler: one edge-time vector and
  one value-reference column per signal. They do not materialize `time_ns`, a
  per-edge `signals` dictionary, or a normalized `{bin,hex,dec}` copy for every
  sampled value. Standalone sampling tools retain their existing row-oriented
  result schema. `inspect_handshake` consumes either representation with
  identical facts, and advances the AHB write-data hold state machine in the
  same pass as the main handshake state machine. Signal lookup uses a monotonic
  transition cursor (with a bisect fallback only for unexpected decreasing
  sample times), preserving duplicate-timestamp and pre-first-transition
  behavior. On the same generated 32-interface/one-clock/20,000-cycle VCD
  benchmark above, three runs measured 11,473.8 ms median and 19.80 MiB maximum
  incremental `tracemalloc` peak: 18.0% lower elapsed time and 40.2% lower peak
  than the previous 13,986.8 ms / 33.13 MiB grouped implementation. A separate
  1,000,000-sample/50,000-transition lookup benchmark measured 3.3x speedup
  over the former per-sample bisect oracle with equal values.
- For FSDB clock groups, `FSDBParser.transition_group()` uses an optional native
  ABI to add the group's resolved signals once, call `ffrLoadSignals()` once,
  read each signal independently through the existing reusable 64 MiB per-call
  output buffer, and unload in a `finally` block. This removes repeated
  per-signal load/unload without adopting the multi-signal batch output format
  or changing truncation receipts. The default
  native group limit is 16 signals to bound resident FFR data on multi-GB waves;
  the sweep scheduler first-fit packs complete small clock units into that
  bound. An oversized clock unit is split only at interface boundaries while
  retaining one `EdgeSamplingSession`: the first chunk reads/caches the clock,
  later chunks load only their signal subset, and a single interface that
  itself exceeds the bound falls back honestly. This removes whole-group
  oversized fallback without raising the resident-signal limit. Packs remain
  serial under the process-global FSDB lock and every native group unloads in
  its existing `finally` path.
  `TRACEWEAVE_FSDB_GROUP_MAX_SIGNALS` can set 1..256 after RSS review. Oversized
  groups, begin failures, and older wrappers automatically use the legacy
  per-signal path. Cancellation between interfaces unwinds the context before
  releasing the process-global FSDB lock. On the bundled warmed wide-bus FSDB
  fixture (7 signals, 50 load/read/unload iterations per repeat, 7 alternating
  repeats), the grouped median was 23.644 ms versus 25.773 ms legacy (8.3%
  lower), with identical transition counts and truncation receipts. This
  validates the mechanism only; the one-run metrics are required to judge a
  proprietary workload.
- `scripts/benchmark_sweep_fsdb_group.py` compares complete sweep results while
  alternating forced-legacy and grouped runs, and emits aggregates only. On a
  local 34,874-byte AHB repro FSDB (634 signals, four discovered interfaces,
  5 repeats), both paths returned byte-equivalent fact tables with complete
  coverage and no transition truncation. The packed/grouped median was
  47.807 ms versus 50.523 ms forced-legacy (5.4% lower); four clock units fit
  into three native packs (maximum 16 resident signals) with no fallback. This
  is a protocol/compatibility sample, not evidence about multi-GB scaling.
- `src/path_discovery.py`, `src/compile_log_parser.py`, `src/log_parser.py`, and
  `src/analyzer.py` form the main failure-analysis path from artifacts to
  normalized failures and recommended next steps.
- `src/tb_hierarchy_builder.py`, `src/signal_driver.py` and `src/signal_load.py`
  turn the system into a source-aware debug assistant rather than a parser-only
  tool. `signal_driver` traces back to drivers; `signal_load` finds the
  consumers (fanout) of a signal.
- `src/structural_scanner.py` performs the independent default-flow structural
  risk pass. Narrow-condition brace containment is indexed from one lexical
  brace-event sweep rather than rescanning a file around every zero literal;
  magic-condition analysis sends only mechanically eligible lines through the
  unchanged line-local matcher; and a compact file-local newline index serves
  all source anchors without repeated prefix scans. The scan is offloaded from
  the event loop and checks cooperative cancellation between files and within
  its long Python loops. `scripts/benchmark_structural_scan.py` reports
  privacy-safe timing/RSS/I/O aggregates and a full-result equivalence hash.
- `src/connectivity_backend.py` defines a `ConnectivityBackend` protocol with
  `find_driver`, `find_loads`, and `find_path` methods. `select_backend()`
  returns local `VerdiNpiBackend` when a Verdi KDB is available, or
  `LsfConnectivityBackend` when `TRACEWEAVE_NPI_EXECUTION=lsf`; its queue comes
  only from the namespaced `TRACEWEAVE_NPI_LSF_QUEUE`. Users normally set that
  variable directly; an already-existing site/team variable may optionally be
  mapped to it in the launching shell. TraceWeave never creates or interprets a
  generic scheduler queue variable. Without a KDB it returns the static
  source-regex backend directly. Both NPI execution
  policies wrap Static at the parent: local NPI failures degrade in-process,
  while an LSF worker returns NPI-only results or a fixed failure receipt and the
  login-node parent performs the fallback. `find_path` is NPI-only: Static returns
  `unsupported_reason="static_backend_no_path_api"` rather than approximating
  with regex, since `sig_to_sig_conn_list` walks the elaborated netlist
  across assigns / interfaces / generates that source-regex cannot follow
  reliably.
- `src/npi_lsf.py` owns the versioned Verdi/NPI worker protocol and the
  optional `bsub -K` transport. It covers both explicit NPI connectivity
  queries and `build_kdb` cache misses/forced rebuilds. It writes one private
  request under a
  shared staging root, submits an identity-free random job name, validates the
  response against the existing operation schema, and exposes only fixed
  execution labels. Remote stdout/stderr is fixed to `/dev/null` so LSF does
  not email native license output; the local scheduler client output is held
  only in the private request directory. The synchronous scheduler wait runs in
  `server._run_in_cancellable_thread`; cancellation or timeout performs a
  bounded `bkill -J` followed by termination of the local `bsub` waiter.
  `src/npi_worker.py` invokes either the local NPI core directly (never Static)
  or the exact `vericom` + `elabcom` KDB builder. A compute-node failure cannot
  be mistaken for a successful answer, and a failed KDB job never falls back to
  a login-node licensed build. Exact KDB cache hits remain parent-side
  filesystem reads and submit no job. The NPI success envelope also carries
  only the clean/degraded load quality; the
  parent reads bounded error metadata from the shared KDB and keeps it outside
  the operation-result schema.
  The hierarchy source overlay remains local/optional in the initial scope and
  does not implicitly submit a batch job.
- `src/verdi_backend.py` is a pure-detection probe: it locates KDB at
  `simv.daidir/kdb.elab++` (VCS two-step) or via `synopsys_sim.setup` work-lib
  mappings (three-step / vericom standalone) and emits a per-simulator
  `kdb_hint` (e.g. the exact `vcs -kdb=only` command for a VCS user, the
  `vericom -kdb` command for an Xcelium user) when KDB is missing. Clean
  elaborated candidates win; otherwise an error-marked `kdb.elab++` remains a
  degraded candidate by default, with bounded error-count/log diagnostics.
- `src/verdi_npi_backend.py` lazily imports `pynpi` from `$VERDI_HOME` (zero
  hardcoded prefixes), holds a single design across calls keyed on
  `kdb_path`, and re-issues `npisys.load_design` to switch cases within one
  session. At the native boundary, an artifact path ending in `kdb.elab++`
  is converted to its containing simulation database directory for
  `-simflow -dbdir` (required by Verdi 2020); the original artifact path stays
  the cache identity. Synthesized PinHdl paths
  (`scope:Construct#Op:line:line:Cell.Port`)
  are normalized to FSDB-visible scopes; raw form is preserved in `expr` for
  diagnostics. NPI's `find_path` wraps `sig_to_sig_conn_list` and remains the
  highest-priority implementation for the `trace_signal_path` MCP tool; the
  bounded Source Graph is its production fallback. Another NPI-only capability,
  `collect_instance_src_map`, overlays elaborated `file:line` onto
  compile-log-derived hierarchy nodes. Production overlay calls use exact
  `netlist.get_inst()` lookups over already proved paths; the recursive
  `get_top_inst_list()` walk remains only as a legacy explicit-call mode.
  `LoadHop` / `DriverChainHop` / hierarchy nodes carry a
  `source_info_origin` field (`"compile_log"` vs `"npi"`) so consumers can
  tell which provenance produced each `file:line`.
- NPI load lookup uses `net.load_list()` as its direct-consumer primitive. A
  child's outward-facing output port is treated as a transparent hierarchy
  boundary: `connected_pin().connected_net()` steps to the parent net and runs
  another direct lookup under 64-state / 16,384-handle work limits. It never
  calls native `fan_out_reg_list()`, which materialises a whole combinational
  cone before Python can apply an output slice. All backends cap public load
  output at 256 and populate the backend-neutral `enumeration` receipt;
  `search_exhaustive=false` and fixed incomplete reasons keep a bounded prefix
  distinct from a complete list. Continuation is explicitly unsupported until
  a backend-neutral cursor can preserve artifact/work identity safely.
  `load_design == 1` establishes a clean load. `load_design == 0` is accepted
  only for an error-marked artifact when degraded mode is enabled and a
  non-empty, requested-top-matching top-instance self-check passes. No error
  count threshold is used. Degraded query routing is positive-only: resolved
  drivers, non-empty loads, and found paths are usable partial evidence;
  unresolved/empty/negative results continue to Source Graph and Static.
- `src/structural_scanner.py` and `src/x_trace.py` are first-class extended
  analysis capabilities and should not be treated as optional side scripts.
- `src/schemas.py` and `src/problem_hints.py` are support layers for structured
  output contracts and lightweight analysis annotations.
- `src/hierarchy_handles.py` owns the in-process `HandleStore` and
  content-addressed handle derivation for the slim `build_tb_hierarchy`
  payload. `src/handle_tools.py` implements the six handle tools
  (get_tb_subtree, lookup_tb_files, find_tb_instance, get_tb_file_detail,
  get_tb_class_hierarchy, dump_tb_section) as pure functions over a
  resolved full hierarchy dict.
- `src/fsdb_parser.py` is the Python/native boundary and resolves FSDB runtime
  from repo-local links first, then `VERDI_HOME`. Time contract at this
  boundary: FSDB tags are tick counts, real time = tick × header scale
  (`ffrGetScaleUnit()`, read once at `fsdb_open`). All tick↔ps conversion is
  collared in two `fsdb_wrapper.cpp` helpers (`_ToTag` floor / `_TagToPs`
  ceil, integer-fs base), so every timestamp crossing into Python is real
  picoseconds. Unknown scale → time-based calls refuse
  (`FSDB_ERR_SCALE_UNKNOWN`) rather than assume 1ps. Native text buffers also
  reserve space for an
  `@TRUNCATED` receipt. `get_transitions` propagates that receipt through
  edge sampling and handshake inspection; a sweep with any partial transition
  prefix cannot report `coverage_status="complete"`. `get_waveform_summary`
  exposes `scale_unit`/`scale_fs_per_tick` for self-check. The transition-group
  ABI is optional and detected by symbol presence, so an old locally built
  wrapper remains functional through the legacy path; rebuilding the wrapper
  and reconnecting the server is required to activate group loading.
- `src/waveform_batch.py` provides `WaveformBatchReader` — a time-window
  multi-signal reader with FSDB and VCD implementations sharing the same
  shape. The FSDB path uses `ffrCreateTimeBasedVCTrvsHdl` for a single
  chronological walk; the VCD path is pure Python.

## Handle-based Hierarchy Access

`build_tb_hierarchy` generates a full hierarchy result server-side (project
metadata, grouped file list, complete `component_tree`, `class_hierarchy`,
raw `compile_result`, compact per-file scan results, and a private immutable
content snapshot) but returns only a **slim payload** to the LLM: project,
stats, depth-2 `tree_skeleton`, interfaces, `ambiguous_basenames`, numeric
`build_metrics`, and a content-addressed `hierarchy_handle`. Compile transcripts
are consumed as streams. A source body exists only while its file is being
scanned; cross-file facts plus digest/stat/marker records are derived at that
point and `source_text` is removed before the result enters the handle store.
Retained hierarchy memory therefore scales with extracted metadata and one
fixed-size record per file, not the sum of source bytes. The full result is
registered in an in-process `HandleStore` (`src/hierarchy_handles.py`) keyed by
the handle.

Repeated module and UVM descendants use an internal template object DAG: every
logical instance edge keeps the same compatibility dict fields, but identical
descendant mappings in the same recursion context are retained once. Public
stats summarize shared mappings with memoized logical counts, so a repeated
subtree still contributes once per instance path without first allocating a
flat node list. Handle tools remain read-only over the same nested-dict schema.
The optional NPI `file:line` overlay detects aliases and applies path-specific
facts with copy-on-write, cloning only mappings/nodes on annotated paths; an
annotation can therefore never bleed into a sibling instance that shares the
same definition template. Numeric `build_metrics` distinguish logical nodes,
reachable physical nodes, allocations, cache hits, and reused nodes.

Connectivity planning consumes this compatibility representation through
`src/hierarchy_provider.py`, not by depending on nested dictionaries directly.
The provider contract exposes O(depth) target resolution, exact
instance-to-definition bindings, and bounded direct-child reads. The lexical
provider wraps `component_tree` and remains the default, so basic hierarchy
construction has no optional-frontend dependency. A prepared Connectivity IR
creates a semantic provider lazily over the query engine's existing immutable
instance/definition indexes. Its parent links preserve generate-scope path
atoms and specialization IDs without materializing a second full tree. Stable
instance IDs are local to one immutable design identity; public hierarchy and
Source Graph receipt schemas remain unchanged.

The local NPI backend also exposes an explicit, target-bounded semantic
provider for offline differential evaluation. Before loading a KDB it derives
at most 256 dotted target prefixes (hard maximum 1,024), then calls only exact
`netlist.get_inst()` and direct `def_name()`/source accessors. Missing generate
pseudo-prefixes are safe misses; a later full generated instance path can still
form a proved ancestor binding. The fragment never enumerates siblings, marks
direct-child coverage as truncated, and carries
`npi_hierarchy_fragment_bounded`, so it cannot establish an exhaustive negative
hierarchy claim. It is not invoked by the ordinary hierarchy build and does not
change NPI/Source Graph/Static routing. The opt-in
`scripts/benchmark_hierarchy_provider_soc.py` runs NPI and Slang in fresh
processes and compares only identity-hashed binding facts plus numeric resource
measurements.

Include preprocessing distinguishes complete context from locally proved
structural evidence. If an include cannot be resolved, hierarchy facts emitted
before that uncertainty boundary remain available, while later text is excluded
because the missing header could have changed macro state. An
`include_evidence_mismatch` is a scoped coverage exclusion and does not erase
otherwise positive local facts. `build_metrics` reports the fixed-label
`include_resolution_issue_categories` and `include_context_complete`; individual
scan records report `hierarchy_evidence_status`. Instance candidates additionally
carry `hierarchy_edge_origin`, `hierarchy_edge_status`, and fixed
`hierarchy_gap_codes`. The `component_tree` is the stronger proof boundary: only
`complete` or `positive_local` edges whose type has scanned module/interface
evidence are admitted. Explicit and implicit generate controls, instance arrays,
and bind statements remain diagnostic candidates with independent gaps instead
of being flattened into fictitious instance paths. A parameter override keeps
the direct edge, but records that the compatibility tree did not materialize a
specialization. A duplicate module/interface definition admits only the parent
edge as `hierarchy_definition_status="ambiguous"`, with no guessed source or
descendants. Raw candidates remain in compact scan metadata for diagnostics.

Source Graph accumulates the gaps on each requested ancestor chain into its
adapter receipt and coverage boundary. Query-affecting gaps become objective
exclusions, so they cannot support an exhaustive negative result. Parameter
specialization is informational at that boundary because the isolated Slang
frontend performs the actual specialization; generate/array/bind/include/macro
and duplicate-definition gaps remain exclusions. Bounded bootstrap applies the
same positive-only edge rule and returns
`bootstrap_hierarchy_edge_unproved` instead of rebuilding a guessed chain.
Numeric build metrics expose candidate, unresolved-edge, duplicate-symbol, and
gap-code counts without source paths or source text.

The preprocessor retains bounded physical-work indexes inside one hierarchy
build. Its raw/masked source cache remains byte-bounded; positive include
resolution adds a separate 4,096-entry LRU keyed by parent, literal/macro-
resolved name, and ordered include directories. Missing includes are not
cached. Simulator-recorded include edges also form a unique-basename index;
ambiguous names deliberately fall through to the historical ordered directory
search. Comment masking has a strict slash-free-line fast path, structural
tokenization removes strings with the same lexical grammar before one token
`findall`, and definition patterns use horizontal indentation rather than
cross-line `\s*`. A comment-aware expansion line without a backtick bypasses
the directive/macro recognizers, and file metadata regexes are admitted only by
necessary literal prefilters. When an expanded or trusted structural view will
replace root-local instances, the scanner suppresses that otherwise discarded
root instance parse. These are compilation-unit-local execution optimizations:
macro/conditional state is still replayed independently, incomplete evidence
retains the same proof boundary, and no preprocessed text enters the handle.
`build_metrics` exposes only numeric cache/load/expansion/mask counts and fixed
limits so physical versus logical work is attributable without revealing paths,
include names, macros, or source content.

Six handle tools (`src/handle_tools.py`) resolve a handle and return
targeted slices:

| Tool | Returns |
|---|---|
| `get_tb_subtree` | Slice of `component_tree` rooted at a dotted instance path |
| `lookup_tb_files` | Compiled-file query by objective scan facts (basename, file_type, contains_uvm, has_module, ...) |
| `find_tb_instance` | Instance lookup by exact path or by module name |
| `get_tb_file_detail` | Symbols defined in a single compiled file |
| `get_tb_class_hierarchy` | UVM/SV class inheritance tree |
| `dump_tb_section` | Raw section escape hatch (`compile_result`, `include_tree`, ...) |

Handle format: `tbh_<sha8>` derived from absolute compile_log path,
simulator, and compile_log mtime. Recompilation changes mtime and
therefore the handle, automatically invalidating prior references.

Lifecycle:

- Handles live only in-process (no persistence). Server restart drops every
  handle.
- `_invalidate_downstream("build_tb_hierarchy")` and `_clear_result_state()`
  both call `_handle_store.invalidate()`, so cache invalidation is symmetric.
- Unknown handles return `HandleErrorResult{error: "handle_expired"}` with
  HTTP 200 so the LLM can read and react.
- Optional `TRACEWEAVE_HIERARCHY_TIMEOUT` and
  `TRACEWEAVE_HIERARCHY_MAX_SOURCE_BYTES` guards are disabled by default. A hit
  returns `build_status="blocked"` plus a fixed-label blocker and never
  registers a partial handle. The already-parsed compact compile context stays
  in a four-entry process cache for an explicitly requested bounded bootstrap.
- Transient source sharing is default-on and independently bounded by
  `TRACEWEAVE_COMPILE_SOURCE_INDEX_MAX_BYTES` (128 MiB) and
  `TRACEWEAVE_COMPILE_SOURCE_INDEX_MAX_FILES` (32,768). Set
  `TRACEWEAVE_COMPILE_SOURCE_INDEX=0` to disable it. Invalid/non-positive limits
  disable only this optimization and surface a fixed disposition; hierarchy and
  structural behavior continue through their original readers.
- Local NPI `file:line` enrichment has a separate admission boundary. The
  default `TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY=auto` accepts only clean
  KDBs with at most 4,096 compile-proved instance paths and queries those paths
  directly; degraded or larger designs retain compile-log provenance without
  loading NPI. `force` raises only the automatic admission boundary (the
  100,000-path hard cap remains), while `off` disables the optional pass.
  Driver/load/path backend selection is unaffected.

Why this shape:

- The file list is still served (`lookup_tb_files`), because only the
  compile log is the source of truth for which version of `xxx.v` was
  actually built. Hiding it would break multi-version disambiguation.
- The tree is no longer returned in full; the depth-2 skeleton gives the
  LLM a navigable starting point and `child_count` tells it where to
  drill.
- Downstream Python tools (`analyzer`, `signal_driver`, etc.) re-parse the
  compile log via `parse_compile_log`; they do not consume the LLM-facing
  payload, so shrinking it does not break them.

The legacy full-fat payload remains accessible behind
`TRACEWEAVE_LEGACY_HIERARCHY_PAYLOAD=1` as a one-release migration safety
net, validated against `BuildTbHierarchyResultLegacy`.

## Bounded Hierarchy Bootstrap

`src/bounded_hierarchy_bootstrap.py` is a single-endpoint escape path for
`explain_signal_driver` and `find_signal_loads` when no full hierarchy handle
exists and the caller sets `allow_bounded_bootstrap=true`. It does not serve
hierarchy browsing, path queries, or X tracing.

The bootstrap starts from the parsed compile context and simulator-recorded
ordered inputs. It never searches the filesystem. A capped lexical inventory
is used only when the compile transcript does not pair a definition name with
its source file. The resolver requires a unique top/module/interface
definition, walks direct instances one ancestor at a time, closes selected
package/include dependencies, and rejects unproved preprocessor context. It
reconstructs the active text with command/filelist defines and include paths,
honoring `ifdef`/`ifndef`/`elsif`/`else`/`endif` and bounded nested includes for
both VCS and Xcelium transcripts. The lightweight instance tokenizer preserves
all operators so assignments, casts, constructors, and UVM calls cannot be
collapsed into instance syntax. A separate bounded path expands only a
standalone object-like or function-like macro whose replacement is proved to be
exactly one HDL instance (maximum 4096 expansions and 16 KiB per retained macro
body). It never expands `uvm_*` / `m_uvm_*` macros; a compound instance macro or
an exceeded bound makes bootstrap coverage unproved rather than fabricating a
tree. Generate-controlled, arrayed, or bound lexical candidates likewise cannot
be promoted into a proved ancestor; a direct match returns the fixed
`bootstrap_hierarchy_edge_unproved` blocker and its privacy-safe hierarchy gap.
It never replays the simulator's full UVM library; a `uvm_pkg` import
adds the existing `uvm_dynamic_connectivity` exclusion and lets only unrelated
IR-proved local positives survive. Bootstrap compile replay retains defines and
include options but removes broad `-v`/`-y` library search options, recording
`bootstrap_library_context_scoped`. Time, selected-input count/bytes, inventory count/bytes, include depth,
and hierarchy depth all have hard limits. A limit or ambiguity produces only a
fixed blocker plus numeric metrics.

The resulting compile subset is content-fingerprinted but intentionally marks
the manifest incomplete and non-reusable across requests, with
`bootstrap_hierarchy_scoped` and `bootstrap_compile_inputs_scoped` objective
exclusions. Only an IR-proved positive fact may leave this route. A no-match,
blocker, dependency failure, or timeout returns no connectivity fact with
`exhaustive_search=false` and `negative_claim_allowed=false`; it does not start
a whole-source Legacy Static recomputation. This is the one deliberate
exception to the normal fallback chain below, because such a recomputation
would defeat the bootstrap's resource bound.

## Connectivity Backend Cooperation (NPI, Source Graph, Static)

NPI is the deepest path, Source Graph is the bounded semantic fallback, and
Static is the normal final source-regex fallback. A clean KDB can support both
positive and negative NPI conclusions. A degraded KDB supports positive facts
only; an incomplete or negative answer advances the route. The explicit
bootstrap-only exception above stops before Static when no full hierarchy
exists.

```text
select_backend(probe_status)
├── KDB present + execution=local → VerdiNpiBackend(fallback=Static)
├── KDB present + execution=lsf   → LsfConnectivityBackend(parent fallback=Static)
└── KDB absent/disabled            → public router starts at Source Graph

LsfConnectivityBackend.find_driver / find_loads / find_path
├── invalid config / missing KDB or top → deferred fallback, no submission
├── bsub timeout/failure/bad response   → deferred fallback + fixed receipt
├── worker reports npi_unavailable      → deferred fallback + fixed receipt
└── NPI-only worker result              → validate operation schema + load quality

build_kdb with execution=lsf
├── exact shared-cache hit               → local read, no submission/license
├── invalid config                       → fixed failure, no local build
├── bsub timeout/failure/bad response    → fixed failure, no local build
├── worker build failure                 → preserve build phase/result receipt
├── success but parent cannot see KDB    → npi_lsf_artifact_unavailable
└── shared KDB visible                   → publish completed execution receipt

VerdiNpiBackend.find_driver / find_loads / find_path
├── parse_compile_log fails / no kdb_path / no top   → injected fallback
├── _ensure_loaded clean success (load_design rc == 1)
├── _ensure_loaded degraded success
│   └── rc == 0 + error marker + enabled policy + non-empty/top-matching netlist
├── _ensure_loaded fails (import/init, other rc, failed self-check)
│                                                    → injected fallback
├── top-level exception                              → injected fallback
└── _npi_find_driver  (NPI happy path; backend="verdi_npi" in every branch)
    ├── net resolve fails             → backend="verdi_npi", stopped_at="signal_path_unresolved_in_npi"
    ├── driver_list raises            → backend="verdi_npi", stopped_at="npi_driver_list_failed"
    ├── driver_list empty             → backend="verdi_npi", stopped_at="no_npi_drivers"
    ├── [pre-check] driver_list head is a LOAD of this net (load-alias) & no genuine RTL driver
    │       → driver_status="testbench_driven"  (keyed on driver_list, BEFORE fan-in → covers recursive=True)
    │         (if a genuine RTL driver remains among the candidates → promote it to head, continue)
    ├── boundary-only drivers OR recursive=True
    │       → net.fan_in_reg_list(stop_at_pin, report_primary_port, top_scope_name)
    │       ├── fan_in succeeds       → build driver_chain (+ 2nd load-alias check on the fan-in head)
    │       └── fan_in raises         → fall through to single-hop formatting (still NPI)
    └── normal driver                 → single-hop format
```

The injected fallback is Source-Graph-deferred in the production server and
Static for direct/library callers that do not supply one.

Public route after an NPI query:

```text
clean NPI usable result                         → return NPI
degraded resolved driver / non-empty loads /
degraded found path                            → return NPI, coverage=partial
degraded incomplete or negative result         → Source Graph → Static
NPI load/query/worker failure                   → Source Graph → Static
no full hierarchy + allow_bounded_bootstrap     → bounded Source Graph positive
                                                → otherwise scoped no-fact receipt
```

**Key properties:**

- Payload facts always come from exactly one backend. NPI and Source Graph
  attempts survive only as identity-safe receipts when a later backend wins.
- Slang's `WildcardPortConnection` (`.*`) is represented as an implicit named
  binding, never positional. The resolved per-port mappings remain the
  connectivity facts. UVM packages/classes are not projected as hierarchy
  instances. If module RTL assigns a value from an opaque DPI call, a call
  resolved under `uvm_pkg`, a `uvm_hdl_*` API, or a selected runtime system
  call, the assignment location remains terminal driver evidence but the
  call's arguments are not asserted as structural dependencies of its return
  value. Coverage records `dpi_runtime_not_modeled`,
  `uvm_dynamic_call_not_modeled`, or `runtime_system_call_not_modeled` as
  applicable; ordinary local helper-call arguments keep their existing partial
  dependency behavior.
- Source Graph adapter receipts expose a privacy-safe hierarchy-resolution
  summary (counts and stop depth, never instance names). A dotted suffix is
  initially `deferred` because it may be a legal interface or packed member.
  If the hierarchy scan independently proves that the missing segment is a
  child instance, the adapter stops before launching the frontend with
  `instance_not_in_projected_scope`. Otherwise the IR gets the final say: only
  when it also rejects the suffix root as a declared signal/member does the
  query use that blocker and add `hierarchy_ancestor_chain_truncated`; ordinary
  bad leaf names remain `signal_not_declared`.
- `TRACEWEAVE_NPI_ALLOW_DEGRADED_KDB=0` restores the clean-KDB-only admission
  policy. The probe retains `kdb_validation_status="elaboration_error"` and a
  fixed `npi_degraded_kdb_disabled` routing reason.
- The "boundary-only" detection upgrades dead-end results (where
  `driver_list` returns the queried net's own hierarchy port — i.e. no
  synthesized cell tag, no `:` in the name) to a `fan_in_reg_list` walk,
  which transparently crosses module port boundaries on the elaborated
  netlist. This is why NPI can resolve drivers that Static cannot reach.
- `top_scope_name` for fan-in is derived from `signal_path.split(".", 1)[0]`
  — driven by the query, not by project-specific config — so the bound is
  correct across designs without hardcoding any top name.
- **Driver-vs-loads cross-check.** A net cannot be both driven by and read
  into the same elaborated pin, so when the reported driver's raw identity
  (modulo bit-indexing) is byte-identical to one of the net's own loads, that
  "driver" is a load-alias (interface slice / a register reading the net), not
  the source. NPI's register fan-in cannot see a procedural UVM driver (virtual
  interface + clocking block), so on such a net it can walk to a nearby LOAD
  register inside the DUT and mislabel it the driver (the AHB-master-HTRANS →
  matrix `lock_owner` misattribution). The cross-check (`driver_is_load_alias`
  + `_loadcheck_head`, fed by the net's own `load_list()`) promotes a genuine
  RTL driver if one remains, else returns `driver_status="testbench_driven"`
  with a `cross_check.conflict` receipt — never a load named as an `exact`
  driver. Byte-identical matching keeps it FP-safe: a real `q <= q + 1`
  counter loads into a distinct `Add`/`Assignment` cell, so it never matches.
  The decision is keyed on the **original `driver_list`** and short-circuits
  *before* fan-in, so it covers `recursive=True` too — under recursion fan-in
  walks to a downstream LOAD register (the matrix `lock_owner` that reads the
  net), which is in the net's fan-OUT, not its `load_list`, so a fan-in-keyed
  compare would miss; widening the load set to fan-OUT is wrong because a
  self-counter's own `Reg` is in its fan-out (the feedback).
- For Xcelium / `xrun` flows there is no KDB by default. NPI requires a
  separate `vericom -kdb` + `elabcom -elab kdb` pass over the same
  sources. When `AUTO_KDB_BUILD` is on (default), TraceWeave's
  `build_kdb` MCP tool will run those two commands for the user; the
  Static fallback is only used while no KDB exists yet.

## Auto-KDB build for Xcelium (`build_kdb` tool)

When the active simulator is Xcelium and the KDB probe finds nothing,
the diagnostic snapshot lists `build_kdb` in `missing_steps`. Calling
`build_kdb(compile_log=...)` runs vericom + elabcom against the file
list, defines, and include paths parsed out of the compile log, and
caches the resulting KDB under a project-agnostic cache root. Local execution
remains the default. With `TRACEWEAVE_NPI_EXECUTION=lsf`, every cache miss or
forced rebuild runs in the same LSF policy used by NPI queries; no failure path
silently starts a licensed local build.

```text
build_kdb(compile_log)
├── parse_compile_log → top, files, defines, incdirs, UVM flag
├── hash = sha256(top + sorted(files + mtimes) + sorted(defines)
│                 + sorted(incdirs) + uvm_bit)
├── cache_dir = $TRACEWEAVE_CACHE_DIR/kdb/<hash>/
├── if cache_dir/state.json says ok → return cached, no Verdi spawn
├── execution=lsf → submit versioned private worker request via bsub -K
│   ├── worker builds under the same absolute cache path
│   └── parent verifies returned kdb_path is visible
└── execution=local, or inside LSF worker
    → build in $TRACEWEAVE_CACHE_DIR/kdb/.tmp-<hash>-<pid>/
    ├── write build.sh (regenerated every rebuild; runnable standalone)
    ├── vericom -sv -kdb [-ntb_opts uvm] [+define+...] [+incdir+...]
    │           <files in compile order> -top <top>
    │   → vericom.log
    ├── elabcom -lib work.lib++ -elab kdb -top <top>
    │   → elabcom.log
    ├── on success: rename tmp → cache_dir (atomic, replaces stale entry)
    └── on failure: rename tmp → .failed-<hash>/ (preserved for inspection;
                      existing cache_dir untouched)
```

Degraded-KDB consumption does not change this producer contract: the first
implementation can consume an already-existing error-marked project/user KDB,
but `build_kdb` still quarantines non-zero `elabcom` builds and does not publish
them as normal cache entries.

Cache layout under `$TRACEWEAVE_CACHE_DIR/kdb/<hash>/`:

| File / dir | Purpose |
|---|---|
| `kdb.elab++/` | Elaborated KDB artifact. NPI receives the containing cache directory as its `-simflow -dbdir`; this artifact path remains the probe/cache identity. |
| `work.lib++/` | vericom source-lib output. |
| `build.sh` | Runnable reproducer; written every build. Lets users see/run the exact vericom+elabcom commands TraceWeave invoked. |
| `vericom.log` | stdout+stderr of vericom phase. |
| `elabcom.log` | stdout+stderr of elabcom phase. |
| `state.json` | Inputs hash, status (`ok`/`failed`), timestamps. |

The probe picks up these cached KDBs automatically (`kdb_flow:
"traceweave_cached"`), so the same find_driver / find_loads call that
falls back to Static today starts answering through NPI after one
`build_kdb` invocation. A clean user-managed KDB
(`simv.daidir/kdb.elab++` or `vericom`-built `*.lib++`) wins. When the local
candidate is degraded and the exact TraceWeave cache contains a clean KDB, the
clean cached artifact wins; otherwise the degraded local artifact is retained.

Cross-environment generality:

- All inputs (top, files, defines, incdirs) come from the generic
  `compile_result` shape, not from any project-specific paths.
- Include-path syntax `+incdir+<path>` (VCS) **and** `-incdir <path>`
  (xrun) are both extracted.
- UVM detection is heuristic: `-ntb_opts uvm`, `-uvm`,
  `+define+UVM*`, or any source path containing `uvm`. Any one
  signal triggers `-ntb_opts uvm` for vericom.
- Top-module selection prefers names not matching
  `uvm_custom_install*` (Synopsys recorder shims), falling back to
  the first listed top.
- `VERDI_HOME` provides tool paths; no hardcoded install prefixes.
- Cache root honours `TRACEWEAVE_CACHE_DIR`, then `XDG_CACHE_HOME`,
  then `~/.cache/traceweave/`.
- In LSF mode the compile log, source/include inputs, TraceWeave checkout,
  staging root, and cache root must be shared at identical absolute paths.
  `TRACEWEAVE_NPI_LSF_KDB_TIMEOUT` separately bounds queue wait plus both KDB
  phases so the shorter connectivity timeout does not terminate normal builds.

`AUTO_KDB_BUILD` defaults to True. Set `TRACEWEAVE_AUTO_KDB=0` (or
`false`/`no`/`off`) to disable the snapshot suggestion. The
`build_kdb` MCP tool itself is always callable.

VCS flows are not auto-built. Recompiling with `-kdb=only` is a
one-line change to the existing compile command and reuses the VCS
license token, so the verdi_backend hint surfaces that command
verbatim instead of suggesting `build_kdb`.

## Usage Telemetry (`src/usage_telemetry.py`)

Passive, local-only instrumentation built to answer two operational questions
with data rather than guesses: *how often are the shipped primitives actually
used on real workloads?* and *does opt-in Source Graph disk reuse produce exact
cross-process hits and frontend build skips often enough to justify its lookup,
validation, and storage cost?*

- `server.call_tool` is the single choke point every tool call passes
  through. It wraps `_dispatch` in a `finally` that calls
  `usage_telemetry.record_call(...)`, appending one JSONL line per call to
  `$TRACEWEAVE_CACHE_DIR/telemetry/usage.jsonl`.
- Each line records: timestamp, `session_id`, `case` (case-dir basename),
  tool name, **argument keys + a small whitelist of scalar flags** (never
  argument values or paths — noise + privacy), `ok`/`blocked`, `result_bytes`
  (a token proxy), and `latency_ms`. Failed calls additionally carry a
  classification `error_code` (a code such as `missing_prerequisite` or the
  exception class name — never the message, which can embed paths), so
  failure telemetry is analyzable without guessing from byte sizes. Long wave
  operations additionally attach a strictly whitelisted `diagnostics` block:
  wave-lock wait, fixed sweep phase, aggregate search count/total/max duration,
  discovery phase durations, and preemption-to-cancel latency.
- Source Graph calls use a second, independently enforced persistent allowlist.
  It accepts only finite non-negative numeric aggregates plus fixed phase,
  `memory`/`disk`/`build`/`handoff` tier, and disk-validation labels. Numeric fields cover
  adapter/prepare/build/load/query and disk lookup/read/validate/write/publish/
  eviction timing; exact hit/miss/corrupt/build-skip and frontend-launch counts;
  IR/cache/disk bytes, entries and evictions; bounded resource peaks; and
  X-trace artifact-attempt/restart counts. The recorder reapplies this allowlist
  before writing, and aggregation reapplies it to loaded JSONL as defense in
  depth. Artifact fingerprints/digests, cache/source/wave paths,
  signal/scope/value content, free-form diagnostics, and exception text cannot
  enter this block.
- **A session = a `get_sim_paths` case.** The get_sim_paths handler calls
  `note_session(identity)`; a new case identity mints a new `session_id`,
  re-discovering the same case keeps it. This makes "sessions in which a
  primitive was used at least once" a meaningful presence metric.
- Recording is strictly best-effort — every public function swallows its own
  exceptions so telemetry can never break a tool call.
- `aggregate(records)` is a pure function backing the offline
  `scripts/telemetry_report.py` CLI; it is deliberately NOT an MCP tool. In
  addition to per-tool/session usage, its Source Graph block reports calls and
  sessions with metrics, tier counts and p50/p90/p95/max call latency, exact
  hit rate (`hit / (hit + miss)`), validation outcomes, build skips and
  frontend launches, bytes/entries/evictions, internal timing distributions,
  and per-tool tier summaries. Zero placeholders for stages that were not
  entered are excluded from timing distributions. The report reads only the
  append-only JSONL file and never discovers or scans artifact-cache entries.

`TELEMETRY_ENABLED` defaults to False. Opt in with `TRACEWEAVE_TELEMETRY=1`
(or `true`/`yes`/`on`) and restart/reconnect the MCP server. This switch is
independent of `TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE`: both must be enabled to
measure disk-cache usage, while either feature can operate without the other.
Setting the variable alone does not create a directory; the first recorded
call lazily creates `$TRACEWEAVE_CACHE_DIR/telemetry/` and appends to
`usage.jsonl`. Existing records remain readable, including older records with
no Source Graph diagnostics, which are excluded from Source Graph hit-rate
denominators. Telemetry is local-only; nothing is sent anywhere.
