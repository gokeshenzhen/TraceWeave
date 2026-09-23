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
  - isolated simulation/formal discovery state / prerequisite checks
  - diagnostic snapshot and result caching

Artifact discovery
  src/path_discovery.py          # VCS/Xcelium simulation artifacts
  src/formal_path_discovery.py   # bounded provider boundary; JasperGold first

Core log and failure analysis
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
  src/compile_environment.py      # Bounded log-anchored path recovery shared by hierarchy/KDB/Source Graph

Waveform backends
  src/vcd_parser.py
  src/fsdb_parser.py
  src/fsdb_signal_index.py
  src/cycle_query.py
  src/waveform_batch.py           # FSDB+VCD batch reader (time-window)
  src/waveform_selection.py       # request-local declared-coordinate projections
  src/waveform_hints.py           # producer and exact tool pseudo-signal hints
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
  src/tlul.py                     # explicit A/D mapping over handshake/transaction engines
  src/packed_layout.py            # packed-member export from trusted semantic artifacts
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

- VCD point and window reads bisect the existing ordered transition records;
  separately dumped bits retain their declared indices (`valid[0]`, `valid[1]`)
  and disjoint slices retain their ranges. A unique vector range remains
  accessible by its traditional base name as well as its exact declared range;
  multiple bit/slice declarations never silently overwrite that base name.
  Symbol aliases continue sharing the same transition records. Point/window reads
  do not rebuild a timestamp list or scan the preceding history. Equal
  timestamps keep file order, including rounded sub-ps events. Query work is
  O(log N + returned events) with no persistent duplicate time index; the first
  full-file parse and its memory cost are unchanged. Reproduce reader timings
  with `scripts/benchmark_waveform_reads.py`: generate a deterministic VCD using
  `--generate --wave /tmp/activity.vcd --steps 1000000`, then run the same command
  without `--generate` (optionally `--workload point`, `window`, `around`, or
  `cycle`). `--source-root` selects another checkout for before/after comparison.
  The script checks returned values and reports first-load versus repeated-read
  timing and Linux RSS; it excludes MCP transport, conversion and disk-cold I/O.
- FSDB point output retains the shared 64 MiB capacity for wide values, but
  copies only the actual value plus its NUL terminator. Unused capacity is not
  zero-filled on each read; the allocation and native signal-loading costs are
  unchanged. Native callers still receive a bounded, terminated prefix when
  their buffer is smaller than the value, and nonpositive capacity is rejected.
- Repeated `get_signals_by_cycle` calls reuse a complete compact clock-edge
  vector and its global median period. `src/clock_edge_cache.py` retains one
  clock/edge per parser, with an 8 MiB entry limit, 32 MiB process array budget
  and 64-entry LRU cap. Parser replacement, FSDB close and owner collection
  discard indexes; incomplete native prefixes and oversized indexes bypass
  caching. Cache hits still observe cancellation. The cache bookkeeping lock
  never covers waveform I/O and does not replace the process-global FFR lock.
  Initial full-clock reads and their temporary memory are unchanged; only
  repeated reads avoid that work. Absolute cycle numbers, total edge counts,
  global period and public result schemas remain unchanged.
- Standalone FSDB point reads temporarily restrict FFR loading to the queried
  time. FFR loads whole flush sessions, so a single-session file gains little
  from this restriction. `fsdb_point_read.h` saves the current bounds before
  changing them, releases the traversal and load, then restores those bounds
  on success, failure and exceptions. Missing bounds or unsupported windows
  use the previous loading path; an empty partial read retries the original
  view once. Cleanup failure closes the Python reader before any later query.
  Active transition groups keep their existing load/view; point reads reuse
  group members and reject nonmembers without disturbing the group. Range and
  around-time reads keep their existing predecessor/history behavior. Native
  calls still run under the global FSDB lock and cancellation is observed after
  the native call returns. For a reproducible multi-session fixture, generate
  the benchmark VCD, convert using local Verdi `vcd2fsdb` with
  `FSDB_ENV_WRITER_MEM_LIMIT=4`, and point `TW_POINT_READ_FSDB` at the result when
  running `tests/test_fsdb_point_window.py`. `--wrapper` on the benchmark selects
  a separately compiled baseline library for A/B measurement.
- `server.py` is both the composition root and the workflow gate; tool ordering,
  prerequisite enforcement, session-compatible cache reuse, and in-process
  parsed-log snapshots for same-path simulation reruns live there. Simulation
  and formal discovery use separate state, cache, and provenance identities:
  changing one domain does not invalidate the other, and `get_formal_paths`
  never satisfies a simulation prerequisite. Direct waveform calls remain
  independent of both discovery entry points.
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
  frontend in an isolated one-shot worker. Projection excludes uninstantiated
  generate branches from both instance and assignment facts, independently for
  each parameter specialization. Indexed instance-array elements retain their
  elaborated leaf names. Projector version changes invalidate earlier artifacts.
  An opt-in, default-disabled semantic
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
  `scripts/soak_source_graph_semantic_session.py` exercises this lifecycle with
  20--100 external exact deep queries in fresh one-shot and persistent child
  processes. It admits default-on evidence only when every query selects one
  semantic context, facts/status/coverage remain equal, one frontend launch
  serves the sequence without restart/eviction/failure, latency gates pass,
  and RSS remains bounded. Its report contains only hashes, fixed labels, and
  numeric aggregates. Existing adjacent-artifact reuse is classified as
  `not_needed_existing_artifact_scope`, not as a session hit. Even a passing
  per-design implementation gate cannot authorize default-on without
  representative eligible-design and operational query-frequency evidence.
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
  cannot cancel a worker still needed by another. The finite validated timeout
  remains configurable through `TRACEWEAVE_SOURCE_GRAPH_TIMEOUT` and is echoed
  numerically as `source_graph.effective_timeout_sec`.
  A site may classify an exact, case-sensitive private runtime-only plusarg via
  `TRACEWEAVE_SOURCE_GRAPH_RUNTIME_PLUSARGS_JSON`. The bounded JSON allowlist
  is part of runtime and manifest-cache identity; it cannot classify semantic
  `+define+`, `+incdir+`, or `+libext+` options, never uses prefix/wildcard
  matching, and never exposes configured token text in a public receipt.
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
  bindings and combinational dependencies. Its BFS queue retains only current
  selections. Each first-discovered state stores one predecessor hop, and the
  selected shortest path is reconstructed once with per-hop cancellation
  checks; queue entries never copy their complete path prefix. Positive partial
  results remain partial, while only complete coverage can establish
  `not_connected`; inconclusive/truncated negatives fall through to Static's
  structured unsupported result. `expand_assigns` changes only whether real
  assignment evidence is exposed. `trace_x_source` uses the same memory-first
  artifact runtime and optional exact disk tier while retaining split-phase
  waveform locking and whole-trace restart semantics.
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
  changes the IR or public result schema. A 100,000-instance synthetic lookup
  remained 0.0037 ms median; 4,096/16,384/65,536-bit load queries were
  4.1/16.7/75.6 ms median, while the 65,536-bit public JSON alone was 3.44 MB.
  The measured cost is linear in an extreme-width result rather than a
  hierarchy or stable-ID lookup hotspot. Interval/segment storage therefore
  remains a future migration gated on a representative workload whose IR
  memory or warm latency is dominated by bit tuples.
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
  identity but send only Verilog/SystemVerilog inputs to Slang. Compile-log
  parsing, manifest translation, bounded projection, workers, KDB input
  extraction, and benchmark oracles share `hdl_suffixes.py`: `.v`, `.vh`,
  `.sv`, `.svh`, `.svi`, `.sva`, and `.svl` are text-capable frontend inputs;
  `.svp` remains an ordered, content-fingerprinted frontend/KDB input but is
  never fed to lexical hierarchy or structural-risk scans. Its suffix alone
  contributes `protected_region`, so encrypted internals cannot support an
  exhaustive source-level claim. The worker marks
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
- Handshake discovery retains at most 65,536 signal descriptors per request,
  applying the exact, case-sensitive hierarchy scope before that limit. The
  optional native scope ABI seeks the existing ordered FSDB index with
  `lower_bound`, then returns lexical pages of at most 1,024 records, 1 MiB and
  4,096 visited entries. Actual scope boundaries distinguish escaped names from
  hierarchy separators. Direct ancestor queries skip child subtrees and have a
  separate 4,096-record / 256-level cap. Request totals are bounded by 131,072
  visits, 16 MiB of record bytes and an estimated 64 MiB retained snapshot.
  Native open still builds the design index; VCD still parses the full input
  and lazily sorts references to existing declaration keys for paging.
  Cursors bind the exact scope, file stat identity and parser index generation;
  replacement, mutation, close or reopen cannot continue an old page. This is
  stat identity, not a content hash. No cursor or discovery snapshot is persisted.
  Both sweep families share one read-only snapshot, including empty-result hints,
  and release it before waveform sampling. Public keyword search retains its
  existing case-insensitive substring semantics and ranking. Older wrappers use
  the legacy search fallback and do not claim paged work limits.
  The existing `discovery` receipt adds `mode`, page/visit/byte counts and caps,
  a retained-byte estimate, separate scope/ancestor return counts, `scope_total`
  (null until exhaustion), and `scope_total_lower_bound` (confirmed retained
  scope members only; an unconsumed lexical key is not counted). These are enumeration
  facts, not protocol coverage. Search failures or truncation make a sweep incomplete
  even when its interface cap was not reached. `discovered_count` is then a
  lower bound. Narrowing scope can recover interfaces that a global prefix
  missed; raising `max_interfaces` alone cannot repair discovery truncation.
  Valid/ready bit channels pair by identical index and carry an explicit payload
  mapping need. Port `_i`/`_o` normalization keeps original paths and direction
  evidence; conflicting directions and duplicate normalized roles are not paired.
  Payload requires a same-channel field name and compatible known direction;
  an unnamed channel never absorbs arbitrary register/counter buses. Both generic
  and protocol candidates leave multiple nearest clocks unresolved and exclude
  clock enable/status signals. Req/ack naming returns
  `handshake_semantics="requires_confirmation"`; automatic sweeps skip those
  candidates until valid-hold semantics are supplied explicitly. Idle-ready counts
  remain visible for both families but create no sweep flag or ranking weight.
  Explicit `inspect_handshake` calls retain their previous semantics. Packed-only
  buses require a future field-selection capability; empty discovery cannot
  certify them. Each inspected row retains its actual check coverage.
  Paging and sampling run under the existing wave locks, including the one global
  FSDB lock. Cancellation is checked between pages and after native calls; native
  open and individual native calls are still not interruptible internally. Time
  conversion, four-state values, predecessor and transition truncation are unchanged.
  Reproduce discovery or sweep workloads with
  `python3.11 scripts/benchmark_signal_discovery.py --wave /absolute/run.fsdb
  --mode discovery --scope tb.dut --output /tmp/discovery.json` (omit scope for
  global discovery; use `--mode sweep --start 0 --end 100000 --max-interfaces 64`
  for a sweep). Run each revision serially in fresh processes. Reports include
  first/warm wall and CPU time, peak RSS, native/search call volume, complete
  facts and source/library/input fingerprints. Candidate sets can change with
  role corrections: compare common-interface observations separately from new
  candidates and changed flags, rather than claiming identical whole results.
  The September 18 large SoC replay (18,641 lexical hierarchy nodes, 75,578
  VCD IDs, fresh MCP server, NPI disabled, 64-interface cap) discovered at least
  7,421 interfaces versus 297 in the original evaluation. It explicitly reported
  partial discovery at the signal cap. The previously missed deep stage yielded
  two interfaces in 94 ms; both had zero payload-hold violations. The whole
  first sweep took 4.21 s, including VCD loading, versus the earlier 7.16 s;
  process-tree peak RSS was 791 MiB versus about 711 MiB. These are individual
  case measurements, not full-coverage or general speed/memory guarantees.
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
- `design_identity.py` and `structural_scan_runtime.py` cache the complete
  lexical scan before MCP output trimming. Ordered compile context, exact raw
  content, categories and rule version form the key; digest records are reused
  only after full stat validation (including ctime/inode) and byte hashing;
  even coarsened mount timestamps cannot hide same-size content edits. Resolved literal
  includes and earlier search candidates are checked again before publication
  and reuse. Missing scanned inputs disable caching. Lexical identity covers
  the exact texts consumed by regex rules; unresolved macro/library includes
  separately prohibit treating it as a complete semantic identity.
  The default process-local LRU retains at most eight results / 32 MiB of JSON
  and 16 MiB of digest/include facts, never persistent source bodies. A warm hit
  skips source preload and all rules. Exact concurrent callers share one build;
  one cancellation preserves it, final-waiter cancellation stops it. Results
  computed across a source change are degraded and never published. Set
  `TRACEWEAVE_STRUCTURAL_SCAN_CACHE=0` to bypass this optimization.
  An already joined source lease survives identity capture and transfers into
  the rule build on a miss, even if hierarchy finishes first. Cache hits and
  coalesced callers release unused leases. The source-index A/B benchmark
  disables result caching and uses `fast` so it measures source sharing;
  result-cache byte validation is measured by the separate cache benchmark.
  `scripts/benchmark_structural_cache.py` compares full internal result hashes.
  On the local OpenTitan 1,117-source workload (September 18, sequential calls
  in one fresh process), uncached/cold calls took 4.26/5.99 s; three warm hits
  took 1.03/1.06/1.01 s with zero rule executions and identical full results.
  The stored result occupied 739,443 bytes; cumulative process peak RSS was
  400 MiB. This is a single-process sample with warm filesystem pages; the cold
  identity cost is real and the cache does not accelerate an isolated first scan.
- `structural_semantics.py` scans elaborated active instance bindings and
  specialization templates directly, without a ConnectivityIR build. It reports
  input/open-port facts, constant regions of input concatenations and continuous
  assignments, and named/literal constant comparisons. Statement source and
  enclosing consumer context remain evidence, not defect verdicts. Sequential
  reset/initial values are not treated as permanent ties. Definition buffer
  identity plus all value parameters form template identity; type parameters
  and external hierarchical dependencies conservatively retain separate
  instance templates. The isolated worker has time/RSS and instance/fact/AST/bit
  bounds. `fast` stays lexical; default `auto` reuses a compatible completed
  semantic result or explicitly reports `not_run`. `deep` requests construction.
  `analysis_mode` is a per-invocation MCP argument, defaulting to `auto`; no
  environment setting selects this mode. `deep` permits a cold frontend build
  but still reuses an exact completed result. `semantic_scope` limits basic
  fact extraction, not necessarily frontend parsing/elaboration. Reuse requires
  matching identity, scope, categories and budgets; the category list replaces
  the default selection rather than extending it. See [Structural Scan Invocation](#structural-scan-invocation)
  for JSON examples. The lexical scan still runs on a semantic miss.
  Lexical and semantic coverage are independent; output trimming preserves
  counts and marks `output_truncated`, while computation limits mark partial
  analysis and prevent publishing a completed cache entry.
  `scripts/benchmark_semantic_scan.py` exercises real dispatch. The 18,642 active
  instance SoC case produced 14,475 structural facts in 10.17 s (worker peak
  344 MiB), with no instance/fact/AST cap reached at the default budgets. Coverage
  remained partial for runtime exclusions and incomplete include identity;
  the following `auto` call took 8 ms and honestly reported a semantic miss.
  This normal-design inventory includes legitimate constants and is not a bug
  count. A 301-instance repeated-wrapper regression also checks that three
  definition templates suffice when specialization/context actually match.
- Scoped semantic scans can publish compact query IR from the **same** Slang
  frontend build. This requires an existing current hierarchy, an explicit
  `semantic_scope`, an exact reusable query identity, identical ordered
  compile manifests/frontend arguments, and at most 64 projected instances.
  Projection is still charged to the worker time/RSS budget and capped at
  16 MiB serialized IR. The normal Source Graph loader validates fingerprints,
  schema, scope and capabilities before admission to its existing bounded LRU.
  No full-design IR or retained frontend process is introduced. Deep scan and
  query frontend launches share process-wide cold-build admission; cancellation
  or timeout while waiting cannot release another operation's admission.
  The `semantic.query_artifact_status` receipt distinguishes publication,
  reuse, incompatibility and bypass. An `auto` scoped scan may consume positive
  constant input mappings from a matching query artifact without constructing
  a frontend; it always reports partial coverage and
  `query_artifact_port_bindings_only`, rather than claiming the other semantic
  checks ran. Its current primary-log manifest must also match, so a hierarchy
  enriched by supplemental compile logs cannot silently change scan context.
  `TRACEWEAVE_STRUCTURAL_ARTIFACT_SHARING=0` disables this optimization. No
  incomplete-key handoff is promoted to a reusable entry; new scopes can still
  require construction. NPI retains priority and uses its own KDB. Scan facts
  may guide NPI queries, but are never labeled as NPI facts or converted to KDB.
- `structural_propagation.py` adds explicitly requested `propagated_constant`
  and `constant_control` categories. It inventories writers before solving a
  finite work queue for input/output bindings, continuous assignments, integral
  conversions, static selections/concatenations, basic arithmetic, Boolean and
  bitwise operations, comparisons and conditional expressions. Actual X/Z,
  insufficient evidence and conflicting writers are distinct states. Partial
  vectors produce constant regions, never a whole-vector tie claim. Sequential
  and initial values remain unknown boundaries. Unsupported global write
  effects, force/release, interface/primitive/alias constructs and ambiguous
  net-port reverse drive prevent propagation facts; ordinary unsupported
  expressions remain unknown and mark partial coverage. Procedural assignment
  targets are boundaries, including `always_comb` in this initial pass.
  The writer inventory covers all elaborated tops even for a scoped output:
  an unvisited sibling can write a hierarchical target. A budget interruption
  during inventory or solving emits no propagation prefix. After convergence,
  a fact/output limit can retain a positively proved prefix with partial or
  display-truncated coverage respectively. The default additional limits are
  100,000 bit-weighted work steps and 262,144 signal bits, with configurable hard
  maxima of 1,000,000 and 1,048,576. Instance/AST/fact limits and isolated worker
  time/RSS guards still apply. `semantic.propagation` reports inventory status,
  work, bits, unknown/conflict counts, boundaries and gaps. A complete traversal
  does not imply every signal is constant or every design behavior is covered.
  Tests compare 768 four-state operator cases with Slang's independent
  constant evaluator, plus multilevel, signed/ascending-bit, multiple-writer,
  external hierarchical write, sequential, cancellation and budget controls.
  `scripts/benchmark_structural_propagation.py` completed a generated 18,001
  instance / five-wrapper-level case in 9.92 s with 39,000 facts and 248 MiB
  worker peak RSS (one fresh process, 50,000 fact / 1,000,000 work-step limits).
  This controlled combinational case is not the real SoC. On the 18,642
  instance OpenTitan/PicoRV32 composition, `benchmark_semantic_scan.py
  --propagation` took 10.20 s / 349 MiB and retained 14,475 basic facts, but
  propagation explicitly stopped at unmodeled call write effects. It did not
  prove full-SoC propagation coverage or publish a completed semantic cache hit.
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
- FSDB metadata ABI v1 is separately optional and version-probed. Exact width,
  direction and type lookups use the existing native ordered path map in
  O(log N), without keyword search or a duplicate Python design index. Width
  misses and older wrappers retain the existing exact/suffix search fallback.
  A positive metadata LRU per parser is limited to 1,024 entries and 256 KiB
  of estimated retained bytes; misses are not cached. Immutable header facts
  and bounded summary lists are separate from this LRU. Comparison and window
  validation use `get_header`, with `get_summary` compatibility for other
  parsers. Normal headers do not enumerate signals; the existing zero-duration
  recovery can still inspect up to eight sample signals and remains best effort.
  FSDB open still constructs the native full-design index.
  Every parser access checks real path, device/inode, size and nanosecond
  mtime/ctime; replacement/reopen/close invalidates retained metadata and clock
  indexes. These stat identities are not content hashes. An update during a
  resident group is refused until its normal cleanup; the process-global FSDB
  lock, cancellation checkpoints and native-call cancellation limit remain.
  New summary samples are the first 20 full paths in lexical order, read into
  a bounded 64 KiB buffer. Actual top scopes are collected during the existing
  native tree walk and listed separately (256 entries / 64 KiB). Check the
  additive `metadata_query_mode`, `sample_signals_order` and
  `top_modules_complete` fields: legacy ranked samples cannot establish the
  complete top set, and a capped top listing explicitly reports false.
  Rebuild with `bash scripts/build_wrapper.sh` and reconnect to activate the
  native ABI. `scripts/benchmark_fsdb_metadata.py --help` describes independent
  fresh-process metadata, sweep and diff workloads, loaded module/library
  fingerprints, first preparation, hot queries, RSS and complete fact digests.
  Timings include nested phases; do not sum them. No OS cache flushing or MCP
  transport timing is implied.
- `src/waveform_batch.py` provides `WaveformBatchReader` — a time-window
  multi-signal reader with FSDB and VCD implementations sharing the same
  shape. The FSDB path uses `ffrCreateTimeBasedVCTrvsHdl` for a single
  chronological walk; the VCD path is pure Python.

## Formal Artifact Discovery

`get_formal_paths` is a tool-neutral, artifact-only API backed by
`src/formal_path_discovery.py`. One bounded deterministic filesystem walk owns
canonical root containment, symlink deduplication, explicit override
validation, common VCD/FSDB collection, fixed output caps, and a discovery
coverage receipt. Providers classify only objective layout evidence. The first
provider recognizes JasperGold project markers and log roles; adding VC Formal
or another tool means adding and validating another provider rather than
forking the public API.

The result deliberately omits property status, trace kind, reachability,
assumption, and reset semantics. `coverage.status` describes only whether the
filesystem enumeration was complete. Formal-tool databases, backup sessions,
and engine caches are excluded and never opened. This keeps proof semantics in
the formal tool or MCP client, where the producing command and property context
are available.

When waveforms are found without project entries, both automatic `wave_only`
discovery and explicit exports add one short readback hint to the existing
`hints` list. It points to summary/search as needed and the existing point/batch
readers. The caller selects the waveform, signals and timestamp; discovery does
not open waveforms or construct a query. The coverage receipt and hint cap still
apply, including when a limited scan has found only a prefix of the artifacts.

Exported VCD/FSDB files reuse the existing waveform backends. A recognized
JasperGold VCD `$version` produces only a normalized `producer_hint`; exact
`:jasper_formal_clock` and `:jasper_formal_reset` search rows receive optional
role hints. These are provenance/navigation facts, not semantic
classification. Automatic clock detection prefers a viable real RTL clock,
while callers may explicitly sample the pseudo-clock. Tools that already emit
standard VCD/FSDB need no provider for direct waveform use; unsupported file
formats require a separate waveform backend.

Signal search returns candidates and a conditional readback hint in both its
single-keyword and batch forms. The batch places the shared hint only at the
outer level and preserves each backend's existing hints. Callers choose the
signal set and times: use a point query for one signal, or
`get_signals_around_time(return_mode="values_only", window_ps=0,
extra_transitions=0)` for several signals at the same known time. Cycle queries
require an explicit clock and sampling semantics; check actual sample times
and counts, since an initially high clock does not establish a rising edge at
the initial timestamp. Read a missing initial state separately by time. Keep
glitch and asynchronous investigations on transition/window queries.
`values_only` trims the response after reading; it does not bypass the reader
or clock-detection guardrails and makes no parser-performance guarantee.

The MCP request adapter rejects `search_signals.keyword` lists longer than 16
before any search or parser access. It returns `isError=true` with matching
JSON text and structured content: `error_code="too_many_keywords"`,
`parameter`, `provided_count`, `max_count`, `search_executed=false` and a
batch-splitting `recovery` instruction. No keyword or path contents are echoed.
All other requests retain the SDK's original input validation and error
handling. Neither this precheck nor SDK input-validation failures enter
tool-handler telemetry; client-side rejection may happen before either layer
and cannot be customized or counted by the server.

### Readback Client Check

`scripts/check_waveform_readback.py` launches an isolated stdio server with
telemetry disabled and a neutral VCD, without changing client configuration or
running an EDA tool/model. Its JSON receipt records the launch checkout commit,
dirty state and server-file digest, the MCP handshake version, SDK-client
version, tool names/readback schemas, and actual call outcomes/bytes/latency.
It checks point/batch agreement, initial and uninitialized values, X/Z, missing
signals, short cycle reads and the recoverable keyword limit. These timings
are probe observations, not a performance benchmark.

Use `--work-dir /tmp/traceweave-readback-check` to retain the fixture. The
`ai_client_probe_requests` in the receipt are explicit point/batch calls with
known expected values. In a separate smoke session, ask the target AI client
to execute those exact calls using its configured TraceWeave connection, then
save its version, actually observable tool-catalog evidence, and call results.
Distinguish a rejected call from a tool the model did not invoke. Leave
unobservable catalog/version layers `unknown`; the SDK probe cannot attest to
an AI client's exposure, and neither probe measures natural model adoption.
Keep this session separate from any formal experiment or agent A/B.

An optional `--server-command <executable> <args...>` checks another local
stdio installation without claiming its source commit. The installed-wheel
CI smoke uses the same readback checks, beyond initialize/tools-list alone.

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

Connectivity semantics have a separate opt-in differential harness,
`scripts/benchmark_connectivity_differential_soc.py`. A schema-validated corpus
contains at most 64 exact driver/load/path queries. Compare mode launches one
fresh NPI child and one fresh Source Graph child. The NPI child calls the loaded
KDB core directly, so an internal Static fallback cannot make the oracle arm
look successful. The Source Graph child disables the hierarchy NPI overlay,
builds compile context once, and evaluates each query against exactly one
bounded prepared artifact. It deliberately does not copy production frontier
or fallback orchestration: a missing fact under incomplete projection must
remain measurable as incomplete coverage.

Provider payloads are reduced before leaving each child. The report contains a
query digest, positive/fixed status, exact source-line-and-kind fact digests,
coarser evidence-location and source-file digests, fact counts, coverage and
exhaustiveness labels, numeric/fixed-label driver/load resource bounds,
prepare/query timing, cache aggregates, and RSS. It
contains no signal, instance, source, expression, KDB, or compile-log path.
Driver/load comparison partitions NPI-only facts according to Source Graph
exhaustiveness (`coverage_explained` versus `unexpected`) and keeps Source
Graph-only facts independent. Path comparison uses reachability and hop count,
because internal hop identities are representation-specific. These are offline
measurements only: they neither adjudicate NPI as universally correct nor alter
the trusted-NPI → Source Graph → Static route.

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
    │       → serialize/register official FAN_IN callback
    │       → net.fan_in_reg_list(stop_at_pin, report_primary_port, top_scope_name)
    │       ├── admit ≤ 4,096 native states; return ≤ 32 terminal facts
    │       ├── fan_in succeeds       → exact or partial driver_chain + traversal receipt
    │       └── callback absent/register/fan_in failure
    │                                 → never run unbounded; direct facts are coverage-incomplete
    └── normal driver                 → bounded single-hop format + traversal receipt
```

The injected fallback is Source-Graph-deferred in the production server and
Static for direct/library callers that do not supply one.

Public route after an NPI query:

```text
clean NPI exact or bounded-positive result      → return NPI
degraded resolved/bounded-positive driver /
degraded non-empty loads / found path           → return NPI, coverage=partial
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
- Recursive fan-in is bounded *inside* native traversal, before terminal
  materialization. The official `FAN_IN` callback admits 4,096 states and
  prunes later branches; public output is capped at 32 facts. The callback is
  reset in success, failure, and cancellation paths, while a process lock
  protects pynpi's global callback slot. Missing callback support is a safe
  partial result, never permission to restore the whole-cone call. NPI and
  Source Graph both expose a backend-neutral `traversal` receipt. A positive
  bounded prefix is usable evidence, but only `search_exhaustive=true` supports
  a complete or exclusive driver-set claim; no continuation token is promised.
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

VCS compilation records also support rebuilding from a different terminal.
`compile_environment` infers only uniquely constrained path variables from
simulator-recorded absolute paths and bounded nested filelists, then checks
the replayed project-unit order against the log. The preprocessor and KDB
builder reuse the same inference as Source Graph. No setup script is executed
and no process environment is modified; ambiguous or incomplete option replay
does not authorize conditional hierarchy and fails KDB precheck. Phase-local
KDB options must agree across the compiled sources.

`files.user` is a browsing inventory and may contain module-body or class
includes. With compilation-unit evidence, KDB builds use that ordered evidence
instead; an included file is a separate unit only if the simulator records it
as one. Nested `-f`/`-F` options retain their path bases and include search
order. The original compile cwd is restored as an include directory because
vericom runs under a private cache directory. Legacy caller-built contexts
without compilation-unit records retain their inventory fallback.

```text
build_kdb(compile_log)
├── parse_compile_log → top, files, defines, incdirs, UVM flag
├── versioned hash = sha256(ordered units + top + dependency contents
│                          + ordered defines/incdirs + UVM mode)
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
    ├── revalidate inputs after compile; changed inputs cannot be published
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
  signal triggers `-ntb_opts uvm` for vericom unless the recorded units already
  provide `uvm_pkg.sv`; an explicit package retains its original order/version.
- Cache identity includes source, recorded include, and replayed filelist
  contents, including changes within the same timestamp second. Old cache
  keys are invalidated by the input-version salt. Content reads are streamed
  with cancellation checkpoints; this adds validation I/O to cache lookup.
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

Passive, local-only instrumentation built to answer three operational questions
with data rather than guesses: *how often are the shipped primitives actually
used on real workloads?* and *does opt-in Source Graph disk reuse produce exact
cross-process hits and frontend build skips often enough to justify its lookup,
validation, and storage cost?* The third is whether metric-bearing Source Graph
calls repeat within one case and the default 60-second idle window often enough
to justify retaining a semantic-session frontend process.

- `server.call_tool` wraps `_dispatch` in a `finally` that calls
  `usage_telemetry.record_call(...)`, appending one JSONL line per call to
  `$TRACEWEAVE_CACHE_DIR/telemetry/usage.jsonl`. This covers handler calls only:
  keyword-limit adaptation, SDK input validation, and client-side rejection
  happen before this recorder and are not included in its denominators.
- Each line records: timestamp, anonymous `session_id`, fixed `artifact_domain`
  (`simulation` / `formal` / `unknown`) and `attribution_status`,
  tool name, **argument keys + a small whitelist of fixed decision labels** (never
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
- **Sessions follow discovered artifact owners and observed generations.**
  `src/telemetry_context.py` registers exact files returned by `get_sim_paths`
  and `get_formal_paths`. Simulation uses the selected case; formal uses the
  containing discovered project (deepest when nested), or the export root when
  no project is recognized. A multi-project discovery has no single session.
  Repeated discovery reuses a random process-local id; an observed change to a
  previously registered file starts a new generation and clears that owner's
  older bindings. Returning to an unchanged earlier project reuses its id.
  Paths and `(device, inode, size, mtime_ns, ctime_ns)` remain private in memory;
  stat identity is an ordinary update/replacement check, not a content digest.
  No paths, names, values, identity hashes, or case basenames are added to JSONL;
  production writes `case=null`. Legacy `note_session`/direct recorder callers
  and their old records remain supported.
- Shared readers match the explicit wave path(s); other requests may match
  explicit log/compile paths. No explicit registered artifact means `unknown`,
  including context-only tools that do not supply such a path. Wave ownership
  takes precedence over additional compile context. Unregistered, changed,
  unreadable, cross-owner, and conflicting claims never inherit a recent
  simulation or formal session. The request captures attribution before its
  first await and checks file versions again on completion, so a concurrent
  discovery cannot reassign it. This is accounting only, never a tool gate or
  an inference about proof status, trace kind, or reachability.
- Retention is bounded to 128 owners and 4,096 file bindings (also at most
  4,096 entries examined per discovery). Capacity exhaustion clears the registry
  and latches `unknown/capacity` until server restart; evicting a conflicting
  claim must never make an ambiguous file look uniquely owned. Telemetry off
  skips identity I/O entirely. This registry does not read file contents.
- Explicit point, around-time and cycle readers add a separately allowlisted
  `readback` block: fixed `kind` (`point`, `batch_point`, `cycle`, `window`) plus
  numeric requested-signal, returned-sample and returned-timepoint counts.
  Around-time counts cover center values only; zero-window `values_only` is
  `batch_point`. X/Z cells count as values; missing/uninitialized cells do not.
  Failed calls count zero returned samples. These counters measure returned
  state, not whether the model used it to derive a helper or reach a conclusion.
- Recording is strictly best-effort — every public function swallows its own
  exceptions so telemetry can never break a tool call.
- `aggregate(records)` is a pure function backing the offline
  `scripts/telemetry_report.py` CLI; it is deliberately NOT an MCP tool. In
  addition to per-tool/session usage, its Source Graph block reports calls and
  sessions with metrics, tier counts and p50/p90/p95/max call latency, exact
  hit rate (`hit / (hit + miss)`), validation outcomes, build skips and
  frontend launches, bytes/entries/evictions, internal timing distributions,
  per-tool tier summaries, calls-per-case distribution, and the count of
  adjacent same-case calls within the default 60-second reuse window. The last
  number is explicitly an upper bound: a common case/session does not prove the
  same bounded semantic context is eligible. Missing/invalid timestamps reduce
  and are reported as pair coverage rather than silently becoming misses.
  Zero placeholders for stages that were not
  entered are excluded from timing distributions. The report reads only the
  append-only JSONL file and never discovers or scans artifact-cache entries.
  Its `artifact_usage` block separates formal, simulation, unknown and legacy
  calls, point/batch/cycle/window calls, sessions with returned state, and
  sessions containing only discovery/search/summary. New unattributed calls
  are counted without inventing a shared session; old records keep the legacy
  `(none)` bucket. Readback labels/counts and attribution are filtered again
  during aggregation. Legacy records lack readback counters and cannot establish
  whether state was returned. Session presence is conditional on attributable
  calls, not on all client requests.

`TELEMETRY_ENABLED` defaults to False. Opt in with `TRACEWEAVE_TELEMETRY=1`
(or `true`/`yes`/`on`) and restart/reconnect the MCP server. This switch is
independent of `TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE`: both must be enabled to
measure disk-cache usage, while either feature can operate without the other.
Setting the variable alone does not create a directory; the first recorded
call lazily creates `$TRACEWEAVE_CACHE_DIR/telemetry/` and appends to
`usage.jsonl`. Existing records remain readable, including older records with
no Source Graph diagnostics, which are excluded from Source Graph hit-rate
denominators. Telemetry is local-only; nothing is sent anywhere.


### Divergence evidence and backtrace

`src/divergence_compare.py` owns cursor-free, coverage-aware event comparison;
`src/divergence_clock.py` adds explicitly aligned clock sampling. The server
registers the final cursor after wave work and context validation.

Event comparison uses the private `event_readers` session in
`src/waveform_batch.py` and `src/event_pages.py`. A supported FSDB wrapper exposes
the optional event-page ABI v1: one existing transition group load per file,
one traversal cursor per explicitly supplied signal, and bounded subsequent
pages without repeated seeks. Default pages hold at most 1,024 records and
256 KiB; hard bounds are 4,096 records and 1 MiB. Native cursors are subordinate
to the group and are freed before unloading, including cancellation, timeout,
exceptions and parser close. The global FSDB lock covers the entire session.
Initial FFR loading can still decompress whole flush sessions; fewer returned
events do not prove fewer physical disk reads. Cancellation is checked between
pages and after native returns; opening/loading remains non-preemptible.

Each page keeps a separate strict predecessor and the next unread **raw** time.
The comparator folds a whole raw-time group, carrying only its last value when
the group spans pages. A byte-limited, unrepresentable event marks an incomplete
tail; it cannot establish a difference or equality. Native ticks retain the
header scale, with public ps still converted through `_ToTag` / `_TagToPs`.
VCD pages bisect existing records; only non-integral-ps scales add an 8-byte
raw-tick array per event. Initial VCD parsing and its full-file memory remain.
Internal ordering uses integer fs, so two sub-ps events are never merged just
because their public ps timestamps coincide. `first_divergence_time_fs` carries
the exact positive observation; the compatible integer-ps time/cursor rounds
up. Cursor metadata marks that rounding, and dynamic tracing retains a sampling
frontier for a non-integral-ps difference instead of sampling the rounded value.
Unknown or missing earlier intervals still prevent an earliest-difference proof.

The additive `reading` receipt distinguishes `native_event_pages_v1`,
`vcd_index_pages`, and `legacy_materialized`, with actual page/record counts and
record-byte basis. Native bytes count ABI records, VCD bytes conservatively
estimate records, and unavailable legacy byte counts stay null. Old wrappers,
or a resident-group configuration that cannot admit the pair, use the existing
whole-window fallback; their merge early-exit is **not** streaming I/O. No
multi-pair search is introduced: the comparison proves a result for exactly the
caller-supplied pair, and trace mappings continue to govern dependency pairing.
Clock-aligned root comparison keeps its existing materialized clock/sample
algorithm; event-page gains do not describe that mode.

`src/observation_session.py` reuses immutable windows and samples within one
`trace_divergence` graph attempt. Keys contain file stat identity, parser
generation, real declaration/storage identity, ordered selection bits,
artifact/backend namespace, time coverage, sample time, phase and offset.
Display keys such as `@bits(...)` alone never identify a selection. A complete
covering window may supply a smaller window with a newly sliced predecessor;
truncated/error streams are not retained. The LRU caps both 65,536 retained
events/samples and 32 MiB of conservative Python/key bytes. Oversized entries
bypass caching; eviction never changes unknown/coverage/earliest semantics.
The cumulative transition/time analysis budgets remain independent. Whole-graph
backend/artifact restarts and request exit release cached observations; final
wave/compile validation still discards changed evidence. No cross-request
observation or transaction repository is created. Numeric cache metrics expose
hits, misses, evictions, bypasses and peaks without names/paths/values.

`scripts/benchmark_divergence_reads.py` compares identical inputs against a
specified checkout/native library and records first/warm wall time, CPU,
process peak RSS, read volume, OS I/O counters, cancellation latency and loaded
source/library hashes. Native dlopen and Python imports are outside its timing;
first waveform open/index or VCD parse are inside. It never flushes OS caches.
The existing `benchmark_divergence_trace.py --source-root ...` additionally
checks independent temporal/graph oracles while counting public calls, event
reads, native reads and semantic builds; its VCD workloads have zero native
reads. Hierarchy/scan preparation is outside those trace timings. Reducing
read calls alone is not a claim of lower elapsed time.

`src/divergence_context.py` freezes each side's exact hierarchy, ordered logs,
source snapshot and top. Ready driver relays carry those identities into the
normal public route instead of changing the mutable current session.

`src/dynamic_evidence.py` defines a bounded typed expression contract.
`src/npi_dynamic.py` obtains cell/port, mux polarity and edge facts from NPI and
expands generated nets to declared signals/constants. `src/slang_dynamic.py`
projects typed AST guards, priority and timing into Connectivity IR 1.4;
`src/source_graph_dynamic.py` binds statements and positive port paths to one
prepared artifact. Old IR versions are cache misses, not implicit dynamic proof.
The LSF `dynamic_step` request calls the same NPI core. NPI's active-design
identity is process-wide; fixed elaboration maps distinguish KDB changes while
transient native lock files do not invalidate an unchanged design.

NPI pseudo selections expand the full typed cell output before selecting its
ordered declared bits. Typed arithmetic, variable shifts, vector bitwise,
logical and reduction cells use the shared evaluator. Ambiguous EqComp cells
(`==` versus `===`), generic OpCells and unproven operand order explicitly
restart the whole trace on Source Graph. NPI facts are never relabeled as
Slang evidence. The LSF worker and dynamic step protocols are 2.0.

Slang preserves pure SV operators, casts, packed dimensions, fixed-array leaf
types, packed fields and net declaration initializers. It samples selected
array elements only after exact dump metadata binding; array writes never
reconstruct missing storage. `src/dynamic_selection.py` preserves the entire
arithmetic computation before projecting result bits, including carries.
Only an exact reference can establish passthrough; equal numeric values cannot.
Index differences and changing selected dependencies remain explicit.
Source Graph build/worker is 3.2, projector schema 1.8, query mapping 1.4 and
adapter 3.13, so prior expression/cache capabilities cannot silently carry over.

`src/dynamic_observe.py` separates output observation, triggering edge and strict
predecessor samples; it does not infer simulation scheduling order from integer
picoseconds. `src/divergence_trace.py` pairs active dependencies and stores each
node/edge once. Keys include side design identity, selection, time and phase.
`src/divergence_routing.py` reuses normal backend selection, isolated Source Graph
preparation/single-flight/cache and the exact artifact scope guard. Changes of
backend or artifact discard the graph and restart the original pair with the same
`src/divergence_budget.py` time/work budget. Compile and wave identities are checked
again before return. Wave access uses existing locks; backend work stays outside
them. Metrics contain numeric aggregates only. Static/unsupported semantics and
incomplete positive Source Graph coverage remain explicit frontiers.

## Bounded X History

`trace_x_source` preserves the same-time `snapshot` mode and adds opt-in `history`.
History requires `history_start_ps` and a current hierarchy for `compile_log`;
optional `compile_context` binds an exact hierarchy handle/top as in divergence
tracing. The compile log must agree with the outer request. The caller still
owns the waveform-to-compile version relationship; a matching source snapshot
does not prove a historical waveform came from that compilation. All time
inputs use the existing TimeSpec resolver. `signal_path` names an exact dump
declaration; optional `signal_bits` lists ordered declared coordinates (at most
4,096 bits), preserving generate paths, parameterized ranges and aliases.

The additive `history` payload contains `nodes`, `edges`, `candidates`, `frontier`,
`coverage` and numeric `operation_metrics`. The legacy `propagation_chain` is
empty in history mode; `root_cause` remains null/omitted. A known target returns
`signal_is_clean` for that observation only, with no backend query. Otherwise
observed graphs remain `partial`; missing prerequisites are `blocked` and
missing or invalidated observations are `inconclusive`. There is no claim of an
exclusive source or a proven first origin. Each interval separates the earliest
observed X/Z in the supplied window from the start of the currently active
unknown interval. A dump/window prefix already containing X/Z, missing
predecessor, truncated tail or exhausted window retains its boundary reason.

`src/x_history.py` first locates recorded X/Z, then uses `DynamicRoute` and
`dynamic_observe` to inspect one supported dynamic statement. Combinational
dependencies retain the observation time/phase. Registers use a confirmed
clock edge strictly before a `before` observation, or at/before an `after`
observation, and sample data/guards strictly before that edge. Reset polarity
and timing come from typed backend structure, never names. Unexecuted branches
retain Q; a selected mux's exact ordered Q reference also proves a hold relation.
Equal X values alone do not prove retention. Previous Q, selected controls and
data are exposed separately, so recovered present inputs cannot exclude a past
injection. Same-time changing controls, unknown guards, missing or ambiguous
clocks, different upstream clock domains and unsupported asynchronous
structures stop temporal inference. For same-time data changes at a known
integer-ps scale, a recorded predecessor may extend a bounded candidate path:
the node carries `sampling_status=candidate_predecessor`, the edge uses
`candidate_*`, and `predecessor_sampling_candidate` lists unverified scheduling
and write order. The original `sampling_order_unresolved` frontier remains;
this is not a supported sampling relation. Precision gaps and uncertain
controls cannot use this path. Static constant propagation is not historical
evidence. Full and compact output preserve the same candidate facts and gaps.

History can retain a typed NPI asynchronous boundary with one state driver,
one clock and at most two reset/set controls. Each control carries a direct
single-bit expression, pin kind, active level and assertion edge. The optional
`async_controls` private field is validated at the existing LSF boundary;
older workers without it keep the previous fallback behavior. Pin kind cannot
reveal the original reset/set assignment value, including a source X constant,
so `async_control_value_unmodeled` and temporal/driver frontiers remain. A
history route keeps these positive NPI observations with an inconclusive,
partial receipt instead of discarding them on a fallback. Other dynamic routes
retain their existing fallback policy; one graph still has one provenance.

`src/async_observe.py` reads the typed controls and clock under the same wave
locks and request observation budgets. Each `async_observation` reports its
window, observed assertion count/latest time, control level, clock edges and
whether an edge coincides with the recorded X/Z onset. Missing declarations,
unknown controls, sub-ps ordering, truncated data and an active window prefix
without an observed assertion remain separate gaps. Event coincidence may add
an `async_control_onset_candidate` with executable driver queries for the state
and control; it never adds a supported temporal edge. Assignment evaluation is
`not_run`, the value remains `unmodeled`, and `true_origin_proven` remains false.
Multiple native state writers, unsupported pin types and Source Graph async
timing still stop at their original incomplete boundaries. No state or waveform
is retained beyond the existing request cache lifecycle.

`src/x_history_observe.py` consumes PR 04 private event pages under existing wave
locks. Raw femtoseconds define interval boundaries and timestamp groups; dynamic
sampling at sub-ps resolution remains an explicit frontier, never a rounded
replacement value. Cursors/groups close before Source Graph/Static/NPI work.
Legacy wrappers retain materialized reads, with an explicit mode receipt; an
unavailable exact timescale prevents temporal inference. FSDB still serializes
all handles through its process-global lock and cannot cancel within an active
native call. Initial VCD parsing/FSDB indexing and native signal loading retain
their existing costs; event limits do not promise bounded physical disk reads.

The request never widens its window. Default/hard limits are 20/64 depth,
128/1,024 admitted nodes, 65,536/262,144 read events, 32/128 MiB estimated decoded
event bytes and 30/120 seconds; a node expands at most 16 unknown dependencies.
At most three whole-graph restarts consume the same cumulative budget. Expression limits remain 256 nodes,
32 depth and 4,096 selected bits, including after selection projection. The
ObservationSession cache separately retains its event/byte caps, time coverage
and offset identity. Node/visited identities include design, backend/artifact,
file/parser generation, actual declaration, ordered bits, time and phase.
Every backend/scope-artifact change discards the graph and cached observations
and restarts the original target. Final wave/source/KDB/parser validation drops
all stale evidence even after a timeout. No cross-request history or transaction
cache is introduced.

Partial driver traversal, objective exclusions and testbench/consumer-alias
cross-checks stay on nodes and frontiers. Positive incomplete statements only
create candidate relations; unknown, missing or unexecuted checks are never
negative evidence. Candidate records retain competing hypotheses and explicit
checked/unchecked boundaries. Read `coverage.checks` separately from its gaps.
Metrics count read events, ABI/VCD record bytes, conservative decoded bytes,
wave/page/native calls, backend queries, semantic preparation/builds, elapsed
time and process/frontend RSS. Record bytes are not physical I/O; process peak
RSS is lifetime high-water and includes prior preparation. These measurements
describe bounded work, not an asserted speedup.

## Structural Scan Invocation

`scan_structural_risks` selects its mode per invocation, not through an
MCP client environment variable. Omitting `analysis_mode` is equivalent to:

```json
{"compile_log": "/path/to/build.log", "analysis_mode": "auto"}
```

To allow a semantic build when no compatible result is available:

```json
{
  "compile_log": "/path/to/build.log",
  "analysis_mode": "deep",
  "semantic_scope": "tb.dut.u_block",
  "semantic_timeout_sec": 15,
  "semantic_max_rss_mib": 512
}
```

`fast` runs or reuses lexical rules only. `auto` also reuses compatible semantic
results; on a semantic miss it reports `semantic.status="not_run"` while the
lexical scan still runs or reuses its cache. `deep` permits a cold semantic
build but still reuses compatible completed results. Scope limits basic fact
extraction, not necessarily frontend parsing or elaboration. Time, memory and
coverage limits still apply; these modes do not require a full-design
ConnectivityIR build.

## Client Configuration Reference

The [README](../README.md#client-setup) covers the quick setup. The following
examples retain the detailed EDA environment and LSF configuration options.

### Generic MCP Client

Any MCP client that supports stdio transport can connect to this server. The minimum configuration is:

- Portable PyPI installation: command `traceweave-mcp`, args `[]`
- Repository-local full EDA installation: command `<TRACEWEAVE_HOME>/.venv/bin/python` after running `scripts/install.sh`, args `["<TRACEWEAVE_HOME>/server.py"]`
- EDA env: keep the site-provided Verdi/NPI, VCS/Xcelium, license, and optional LSF variables available to the repository-local MCP process

If the client supports server instructions, it can follow the built-in workflow directly. Otherwise, use the [debug workflow](workflow.md).

### Claude Code

Environment inheritance depends on how the MCP client itself is launched and on
that client's environment policy. In one tested terminal-launched `tcsh`/LSF
setup, Claude Code passed the shell-configured LSF, Verdi, and license variables
to TraceWeave, and remote NPI driver/load/path queries worked without a separate
MCP environment list. An IDE/GUI launch or another client setup may not inherit
the same environment. For a deterministic Claude Code setup, list every variable
the server needs — tool roots plus the `dlopen` chain (`LD_LIBRARY_PATH` is the
one most often missed; missing runtime libraries can prevent NPI from loading,
so inspect `backend_status` when a query falls back).

Add this to `~/.claude.json`:

```json
{
  "mcpServers": {
    "TraceWeave": {
      "command": "<TRACEWEAVE_HOME>/.venv/bin/python",
      "args": ["<TRACEWEAVE_HOME>/server.py"],
      "env": {
        "VERDI_HOME": "<verdi-install>",
        "NOVAS_HOME": "<verdi-install>",
        "VCS_HOME": "<vcs-install>",
        "XLM_ROOT": "<xcelium-install>",
        "CDS_INST_DIR": "<xcelium-install>",
        "SNPSLMD_LICENSE_FILE": "xxxx@s-license.example.com",
        "LM_LICENSE_FILE": "xxxx@s-license-server.example.com",
        "CDS_LICENSE_FILE": "xxxx@c-license.example.com",
        "LD_LIBRARY_PATH": "<library-path>",
        "PATH": "<path>"
      }
    }
  }
}
```

Verify the connection:

```bash
claude mcp list
# Should show TraceWeave (connected)
```

### Codex

Codex supports two ways to provide environment variables to the TraceWeave MCP
server:

- Put fixed values in `[mcp_servers.TraceWeave.env]`. This suits stable tool and
  license locations, or a Codex process that is not launched from a configured
  terminal.
- Use `env_vars` to allow and forward variables already inherited by the Codex
  process. This suits EDA environments managed by `.bashrc`, `.tcshrc`, or a
  site setup script.

Choose one source for each variable; do not configure the same name in both
`env` and `env_vars`. This matches the official
[Codex MCP configuration](https://developers.openai.com/codex/mcp/). The example
below uses fixed values in `~/.codex/config.toml`:

```toml
[mcp_servers.TraceWeave]
command = "<TRACEWEAVE_HOME>/.venv/bin/python"
args = ["<TRACEWEAVE_HOME>/server.py"]
cwd = "<TRACEWEAVE_HOME>"

[mcp_servers.TraceWeave.env]
VERDI_HOME = "<verdi-install>"
NOVAS_HOME = "<verdi-install>"
VCS_HOME = "<vcs-install>"
XLM_ROOT = "<xcelium-install>"
CDS_INST_DIR = "<xcelium-install>"
SNPSLMD_LICENSE_FILE = "xxxx@s-license.example.com"
LM_LICENSE_FILE = "xxxx@s-license-server.example.com"
CDS_LICENSE_FILE = "xxxx@c-license.example.com"
LD_LIBRARY_PATH = "<library-path>"
PATH = "<path>"
```

If a site setup script manages these values, do not copy its expanded values
into `env`. Launch Codex from the configured terminal and use the inherited
environment pattern in the LSF-only section below instead.

Verify the connection:

```bash
codex mcp list
# Should show TraceWeave with Status: enabled
```

### LSF-only NPI licenses

Some EDA sites grant Verdi/NPI licenses only to scheduled compute nodes. NPI
execution remains local by default; opt in to LSF at the **TraceWeave MCP
server process** with:

```bash
export TRACEWEAVE_NPI_EXECUTION=lsf
export TRACEWEAVE_NPI_LSF_QUEUE="digital"
```

Here `digital` is only an example; replace it with the user's licensed team
queue. TraceWeave reads only the namespaced `TRACEWEAVE_NPI_LSF_QUEUE`; it does
not create, overwrite, or interpret a site's generic `LSF_QUEUE`. If the site
already exports `LSF_QUEUE`, the user may map that existing value instead:

```bash
export TRACEWEAVE_NPI_LSF_QUEUE="$LSF_QUEUE"
```

For `tcsh`:

```tcsh
setenv TRACEWEAVE_NPI_EXECUTION lsf
setenv TRACEWEAVE_NPI_LSF_QUEUE "digital"
```

Or, only when `LSF_QUEUE` already exists:

```tcsh
setenv TRACEWEAVE_NPI_LSF_QUEUE "$LSF_QUEUE"
```

Putting these values in `.bashrc` / `.tcshrc` works only when the MCP client
passes that shell environment to the TraceWeave server. In the tested
terminal-launched setup, Claude Code did so and completed LSF-hosted NPI
driver/load/path queries. Codex required the needed site variables to be named
in `env_vars`; without them, the NPI attempt failed.

The following Codex configuration is for an EDA environment already established
by the parent shell. It is an alternative to the fixed-value EDA block in the
Codex section above. The list reflects one tested LSF/EGO site; add or remove
names to match the site's setup, and do not repeat any name under `env`:

```toml
[mcp_servers.TraceWeave]
command = "<TRACEWEAVE_HOME>/.venv/bin/python"
args = ["<TRACEWEAVE_HOME>/server.py"]
cwd = "<TRACEWEAVE_HOME>"
env_vars = [
  "TRACEWEAVE_NPI_LSF_QUEUE",

  "LSF_ENVDIR",
  "LSF_BINDIR",
  "LSF_SERVERDIR",
  "LSF_LIBDIR",
  "PATH",

  "EGO_TOP",
  "EGO_BINDIR",
  "EGO_CONFDIR",
  "EGO_ESRVDIR",
  "EGO_LIBDIR",
  "EGO_LOCAL_CONFDIR",
  "EGO_SERVERDIR",

  "VERDI_HOME",
  "LD_LIBRARY_PATH",

  "LM_LICENSE_FILE",
  "SNPSLMD_LICENSE_FILE",
]

[mcp_servers.TraceWeave.env]
TRACEWEAVE_NPI_EXECUTION = "lsf"
```

Values under `[mcp_servers.TraceWeave.env]` are copied literally by Codex, so do not write
`TRACEWEAVE_NPI_LSF_QUEUE = "$LSF_QUEUE"` there. `env_vars` is the supported
way to forward the value that the user's shell already expanded. If the Codex
parent does not inherit the shell environment, omit the queue from `env_vars`
and put a fixed `TRACEWEAVE_NPI_LSF_QUEUE = "digital"` directly under
`[mcp_servers.TraceWeave.env]` instead. If some EDA values are intentionally
fixed under `env`, omit those same names from `env_vars`.

In the tested terminal-launched Claude Code setup, no extra MCP environment map
was needed when the shell already exported both namespaced values and the full
site environment. For a deterministic setup, or when the client does not inherit
that shell, merge the following fixed values into the existing TraceWeave
server's `"env"` object (replace `digital` with the user's queue):

```json
{
  "TRACEWEAVE_NPI_EXECUTION": "lsf",
  "TRACEWEAVE_NPI_LSF_QUEUE": "digital"
}
```

JSON values are literal too; do not put `"$LSF_QUEUE"` in this static map.

With this mode enabled, explicit connectivity operations
(`explain_signal_driver`, `find_signal_loads`, `trace_signal_path`,
`trace_x_source`) and every `build_kdb` cache miss or forced rebuild submit a
short `bsub -K` worker. Exact KDB cache hits, log parsing, waveform reads,
structural scans, KDB detection, and Static analysis remain local because they
do not invoke a licensed Verdi executable. Connectivity-worker failure or
timeout falls through to the local Source Graph and then to Legacy Static if
that bounded graph is unavailable or inconclusive. A KDB-build worker failure
does **not** fall back to local `vericom`/`elabcom`; `build_kdb` returns a fixed
failure receipt instead. Static still has no
path API, so a final path fallback is explicitly unsupported. Routing is visible through fixed
`backend_status.execution_mode` / `scheduler_status` / `worker_status` /
`fallback_reason` labels; queue, host, command, and license details are not
returned.

After restarting or reconnecting the MCP server, ask the AI agent to run one
explicit connectivity operation and report `backend_status`. A successful LSF
NPI call has `execution_mode="lsf"`, `scheduler_status="completed"`,
`worker_status="completed"`, and `actual_backend="verdi_npi"`. Otherwise inspect
`fallback_reason`; a Static fallback is not an exact NPI result.

For an Xcelium KDB cache miss, `build_kdb` exposes the same top-level
`execution_mode` / `scheduler_status` / `worker_status` / `fallback_reason`
labels. A successful remote build reports `execution_mode="lsf"` and both
statuses as `"completed"`; a cache hit reports both statuses as
`"not_started"` because no license-bearing process ran.

An error-marked KDB may still complete the worker successfully. In that case
`actual_backend="verdi_npi"` is paired with `kdb_degraded=true`; read the NPI
attempt's `coverage_status="partial"` and the `kdb_error_count` /
`kdb_error_log` diagnostics rather than treating scheduler completion alone as
proof of complete elaboration.

Optional settings:

```bash
export TRACEWEAVE_NPI_LSF_TIMEOUT=120
export TRACEWEAVE_NPI_LSF_KDB_TIMEOUT=1260
export TRACEWEAVE_NPI_LSF_BSUB=/path/to/bsub
export TRACEWEAVE_NPI_LSF_BKILL=/path/to/bkill
export TRACEWEAVE_NPI_LSF_PYTHON=/path/to/python3.11
export TRACEWEAVE_NPI_LSF_STAGING_DIR=/shared/private/traceweave-npi
export TRACEWEAVE_NPI_LSF_EXTRA_ARGS_JSON='["-R", "select[...]"]'
```

The compile log, every source/include input, TraceWeave checkout/installation,
staging directory, and `TRACEWEAVE_CACHE_DIR` (including the generated KDB)
must be visible at the same absolute paths on the submission and compute nodes.
After a remote success the parent verifies that the returned KDB path is
visible; otherwise it reports `npi_lsf_artifact_unavailable`. The staging
directory defaults under TraceWeave's cache root; set it explicitly when that
cache is not on a shared filesystem. `TRACEWEAVE_NPI_LSF_TIMEOUT` controls
short connectivity jobs; `TRACEWEAVE_NPI_LSF_KDB_TIMEOUT` separately bounds
queue wait plus both KDB phases (default 1260 seconds). Scheduler options are
JSON argv, not shell text, and are limited to scheduler option/value pairs.

## SV waveform expressions

The existing signal-input union adds `WaveformExpression` alongside strings
and fixed selections. `src/expression_parser.py` performs bounded portable
parsing and type/context lowering; `src/expression_binding.py` binds exact dump
declarations and sparse arrays; `src/expression_values.py` supplies shared
four-state operations. Both frontend projections and public expressions use
`src/dynamic_evidence.py` for values and selected dependency evidence.
Type queries use declaration shapes without reading their contents; an optional
dimension expression is sampled at each observation and retains index evidence.

`src/expression_observe.py` owns request-local derived observations. Point and
cycle reads reuse real backing values; event reads merge dependency events by
raw fs time, completing each time group before evaluation. An event predecessor
is a `dependency_anchor`, not a claimed last output transition. Same-time
internal changes remain explicit and cannot establish an unambiguous derived
clock. FSDB groups include real dependencies and never outlive the global wave
lock; over-capacity groups fall back to bounded sequential declaration reads.
Cancellation and transaction deadlines remain observable inside these scans.

Window terms, protocol roles, transactions, TL-UL, diff and period reuse the
same input layer. Derived findings identify their expression and real
operands; they never manufacture a driver for an `expr@...` display key.
Signed values retain binary bits; protocol IDs and lengths use their unsigned
bit patterns. See [expression usage and bounds](expressions.md).

## Packed waveform selections and TL-UL

`WaveformSelection` is an additive input to point, transition, around-time,
cycle, handshake, and transaction queries. The original string branch retains
its existing behavior. Structured inputs use one exact dump declaration and
either `{path, lsb, width}` or `{path, bits}`. A trailing dump range belongs to
the declaration; it is never interpreted as the requested subfield. `lsb` is
a declared index and width extends toward the left bound. `bits` is an explicit
MSB-first ordering of distinct declared indices. Nonzero and negative bounds,
ascending declarations, existing unambiguous VCD aliases, and separately dumped
bits/slices retain their identities. No suffix search or fragment concatenation
proves an otherwise absent declaration.

`SelectionParser` reuses `BitRange` / `SignalSelection` from connectivity IR.
It checks exact metadata and the parser's file identity before reading, projects
binary four-state strings before any integer conversion, and returns `bin` plus
numeric `hex`/`dec` only when every selected bit is known. Result keys ending in
`@bits(...)` are display keys, not reusable signal paths; the additive
`selections` receipt carries the declaration, range and ordered bits. Driver
followups retain real source bit paths and an explicit `signal_selection`.
Projected transitions coalesce changes in unrelated bits. A pre-window
`predecessor_kind="declaration_anchor"` anchors the backing declaration's last
change, not a proven historical change of the selected field. Around-time
history likewise contains projected backing events, which may repeat a value.

Limits are 128 distinct selections, 4,096 bits per field, 65,536 total selected
bits and backing-declaration width, 1,048,576 projected sample cells, 16 Mi bits
of projected work, and 262,144 projected transition rows per field. Exceeding
projection budgets requests a narrower window instead of silently dropping
fields. Edge sampling reads each backing declaration once and projects its
sampled column; VCD storage aliases share that read even with different declared
coordinates. Only request-local data are retained. A truncated transition
prefix becomes unknown from its final, possibly incomplete time group onward.
Cycle results expose `transition_data_truncated` and affected signals; structured
cycle queries reject an incomplete full-clock read. Window inspections can
instead return partial evidence over a narrower interval.

`resolve_packed_fields` exports named, including nested, members from the
existing Source Graph runtime. It requires a reusable content-anchored build
key, a current compile snapshot, active elaborated instance specialization,
packed-member type facts, and matching dump/source declaration coordinates.
Only explicitly projected instances can supply layouts; ancestor port-binding
fragments cannot. A scoped hierarchy gap alone is acceptable for an explicitly
projected instance with complete type facts; other semantic gaps and blocking
diagnostics require an explicit mapping. Compile identity is checked again
after worker preparation. Missing hierarchy, an unresolved generate edge,
inactive branches, stale source inputs, budget limits, or missing type evidence
return `mapping_required`, never a guessed bus profile. Exported selections are
a snapshot, not a live type handle or proof that a historical waveform was
generated from today's source. Callers establish that association and re-resolve
after changes. Semantic preparation runs outside the wave lock; exact dump
validation uses the normal wave worker and lock.

`inspect_tlul` requires six explicitly mapped controls/IDs:
`a_valid`, `a_ready`, `a_source`, `d_valid`, `d_ready`, `d_source`. Optional
opcode/param/size/address/mask/data/user/sink/error fields use the same selection
contract. It samples the union once, then passes those samples to the existing
`inspect_handshake` and `reconstruct_transactions` engines. A and D payloads
belong to their respective valid producers; ready is the opposite side.
The checks list names acceptance, stall, valid hold, payload hold **during
continuing stall**, source-ID pairing within known history, matched latency,
mapped accepted-field capture and, if supplied, sampled reset boundaries.
It does not add payload-hold checking on the accepting edge, opcode legality,
request/response opcode or size consistency, source uniqueness, mask/alignment,
or integrity-code validation. Unknown payloads are retained as evidence rather
than automatically called illegal data. Missing optional fields are separately
listed in `unmapped_fields`; `complete` covers only the named implemented checks.

Reset and unknown-reset cycles break handshake history. Reset, uncertain
acceptance controls, and accepted unknown IDs break transaction correlation;
the latter conservatively discard both channels' correlation events on that
edge, while top-level known acceptance counts remain separate. `correlation_breaks`
counts uncertain boundaries; `unknown_history_clears` counts boundaries that
actually discarded pending work, separately from observed `reset_clears`.
Same-edge request/response pairs may have zero latency. Without an initial
observed reset, carry-in remains unknown even when all visible endpoints match.
Unmatched responses may predate the window; tail pending requests do not prove
a hang. The 65,536-cycle analysis cap, unknown history and truncated transitions
produce partial coverage, with explicit tail labels. `max_transactions` limits
display, not the checked edge count; all state is bounded by the cycle cap.

These paths keep FSDB's process-global lock, cancellation checkpoints, native
time conversion, strict windows and predecessor semantics. No new native ABI,
cross-request waveform result cache or licensed frontend is introduced. The
existing discovery scope/page budgets, partial coverage, clock ambiguity,
payload ownership and unconfirmed req/ack exclusions are unchanged. A clean
mapped interface does not override partial or flagged global sweep evidence.

## Compact evidence output

The inspected waveform, discovery, comparison, X-trace, structural-scan and
diagnostic tools accept `output_format="compact"`. The default `"full"` retains
the original JSON shape and formatting. `src/schemas.py` owns the supported tool
set, `EvidenceOutputOptions`, `CompactEvidenceResult`, the fixed selection
reference contract, and the shared transaction/TL-UL scalar defaults and bounds.
Registration derives those properties from the models; dispatch validates them
before waveform work. The transaction display cap remains 1..65,536 (default
256), and TL-UL retains its 65,536-cycle default/maximum. Invalid enum/range/type
values are errors even when an incomplete field mapping would return early.

Compact output is an explicit wire-format change for clients that opt in:

```json
{"wave_path":"/path/to/run.vcd","output_format":"compact"}
```

Pass those arguments to `get_waveform_summary`. A TL-UL response uses the same
envelope contract:

```json
{
  "format": "traceweave.compact.v1",
  "tool": "inspect_tlul",
  "result": {"selections": [], "channels": {}, "coverage_status": "zero_coverage"},
  "references": []
}
```

The abbreviated example illustrates the envelope, not a complete TL-UL result.
The actual `result` retains all fields of the full response except exactly
identical nested selection receipts. For example,
`{"path":"/result/channels/a/selections","target":"/result/selections"}`
means the omitted channel receipt is the complete list at the target JSON
pointer. Only the two channel receipts and the nested transaction receipt can
be factored; different paths, declared ranges, bit order or contents stay inline.
There are at most three references, no chains, and no separate evidence table.
All other result families keep their entire original JSON value within `result`.
Whitespace is removed; nulls, empty evidence, unknown values, unexecuted checks,
frontiers, identities and both sides of a comparison retain their original wire
semantics. Tool errors and missing-prerequisite responses keep their original
error shape, including on compact requests.

Clients must expand references before validating against the original result
schema. `src.evidence_output.expand_compact_result(response)` restores that
exact JSON value and rejects malformed, duplicate or dangling references. This
is lossless with respect to the already bounded public result, not the entire
waveform. Analysis limits and existing display caps still apply: read coverage,
gaps, stop reasons and display truncation independently. Compact mode neither
recovers omitted transactions nor turns a bounded prefix into complete evidence.
Request narrower scopes/windows or increase the existing display cap when the
tool's next action permits it; insufficient retained facts require a new call.

Projection runs after analysis in the existing cancellable worker without a
waveform lock. It checks cancellation around projection/encoding and between the
three factoring candidates; an individual Python encoding call is not
preemptible. It makes no additional waveform/native reads. Temporary JSON
objects live only for that response; there is no result repository, persistence,
request coalescing, cache admission, eviction or new pagination API. References
resolve offline within the captured response after restart, but do not prove
that its waveform or compiled artifact is still current. Existing `@time`
cursors and hierarchy handles retain their separate process-scoped lifetimes.
The existing subtree/file/instance tools remain the way to browse hierarchy.

Diagnostic summaries preserve lexical coverage separately from semantic status,
scope, checked categories, gaps, propagation and query-artifact disposition.
Risk totals and returned-risk counts are separate; the legacy `high_risk_count`
is explicitly labelled as counting displayed risks. Structural output trimming
preserves the complete checked-category list. Protocol summaries retain discovery
coverage separately from sweep coverage, window/scope, transition truncation,
skips, finding summaries and executable next actions. `semantic.status=not_run`,
partial discovery and zero coverage never become a clean scan. Existing soft
output-budget accounting uses UTF-8 bytes rather than Unicode character count.
The budget is a soft limit on minified schema JSON, not a hard transport cap;
the default indented response can be larger. Diagnostic summary fields are
additive. Unicode-heavy results can now reach the existing display downgrade
earlier; their analyzed counts and coverage are still retained.

Reproduce projection costs with `scripts/benchmark_evidence_output.py`: generate
a packed VCD with `--generate --wave /tmp/packed.vcd --count 2048 --output
/tmp/unused.json`, then query it using `--source-root`, `--cap`, `--queries` and
`--output-format full|compact`. Each source/format belongs in a fresh process;
the script checks independent accepted-edge and payload timing expectations,
records loaded source/native hashes, parser-open time, sampling/analysis,
projection, serialization, output rows/bytes, native calls and peak RSS. It does
not flush OS caches, and excludes imports, conversion and MCP transport.
Fewer output bytes do not imply fewer reads or lower analysis memory; compact
projection allocates temporary Python objects and can cost additional CPU/RSS.

## Transaction sampling and bounded facts

`reconstruct_transactions` consumes the existing compact columns through
`cycle_query.iter_sample_rows`. Each iteration borrows one reusable signal view;
it does not allocate a signal dictionary or normalized value for every cycle.
`inspect_tlul` shares those same columns with both handshake inspections and the
transaction core. Two byte vectors carry reset/uncertain-history boundaries,
without copying samples or inventing a reset signal. Its private
`TransactionSampleInput` binds the original parser/epoch, file identity, actual
declarations and ordered selected bits, roles and ordered payload fields,
clock/edge/window, fixed 1 ps sample offset, reset polarity and semantic version.
Identity is captured before sampling and checked again before correlation;
untyped sample dictionaries, changed files, reopened parsers and incompatible
mappings cannot be reused. Semantic/layout export remains a separate capability;
these waveform samples cannot establish a compile-to-waveform association.

`transaction_sampling` adapts bounded event pages to the existing sampler.
It reads one declaration at a time with the existing `event_readers` context,
closing the cursor before unloading its FSDB group. Clock transitions are
released after edge extraction; compact columns keep only sampled value
references. A per-read normalization table reuses identical four-state values,
with at most 256 entries and 256 KiB of accounted storage. It is discarded at
the end of that read, has no cross-request hits or eviction lifecycle, and
reports hit/miss counts and peak size. Native page reads and every consumption
loop retain cancellation checks. Missing predecessor values stay unknown;
unfinished equal-time groups are masked rather than extrapolated. No new native
ABI or global lock/worker model is introduced.

The fixed transaction budgets are 128 sampled signals plus the clock,
262,144 samples, 2,097,152 sample
cells, 1,000,000 admitted events, 256 MiB of conservatively accounted decoded
events, 16,384 pending requests, 16,384 early data beats, 32 MiB of accounted
correlation state and a 30 s cooperative analysis deadline. Existing structured
selection cell/bit limits can lower the sample cap; TL-UL also keeps its 65,536
cycle cap. Event pages retain the existing 1,024-event/256-KiB limits; a read
budget may consume one final page whose unused events are reported as read but
not admitted. File parsing/index memory, FFR residency and the legacy reader's
single-call decode scratch are outside these accounting limits. Old wrappers
use the original materialized read and report `legacy_reads`; the new budgets
bound admission and retained analysis data, not that legacy first-read scratch.

Per-ID FIFO deques, ordered pending/data targets and an early-data deque retain
same-ID ordering, cross-ID completion order, FIFO mode, W-before-AW, sampled
reset segmentation, LAST and length checks, and four-state values. Exact latency
statistics use a bounded histogram. Completed counts, unmatched counts, peaks,
ordering, length anomalies and timeout-threshold counts cover every analyzed
edge, including edges after the display cap. Required state is never cleared to
continue after a budget failure. Read exhaustion admits only the common proven
column prefix; state exhaustion stops before the next edge and preserves pending
endpoint evidence. `coverage_status`, `gaps`, `analysis.stop_reason`,
`analyzed_samples` and `last_time_ps` distinguish partial analysis from a full
window. Unknown controls/IDs make coverage partial even if every edge was read.
Tail pending requests and responses without an in-window request do not prove
a hang or an illegal response. Unknown LAST/length values are not clean-burst
evidence.

Normalized `TransactionFacts` freeze aggregates and a bounded evidence prefix
before display and cursor projection. `max_transactions` (1..65,536, default
256) limits only retained/displayed records; the default shape and correlation
parameters remain compatible. Transaction record storage has a separate 16 MiB
accounting cap. Exhausting it marks `analysis.display_status="partial"` and
`display_stop_reason="result_byte_budget"` while full-window counting continues;
it does not turn complete counts into an analysis prefix. A larger projection
of insufficient retained facts reports `retained_fact_prefix` and requires a
new analysis. Display truncation and cursor naming never change aggregate
conclusions or the cursor anchor. Unmatched evidence remains limited to 32
endpoints per side while the counts are complete for the analyzed prefix.

The additive `analysis` receipt reports available/analyzed samples, read versus
admitted events, binary value bytes, page output bytes, conservative decoded
bytes, read/page/legacy counts, state/result/table peaks and sampling,
reconstruction and projection timings. These bytes describe different layers,
not interchangeable process RSS measures. Benchmark RSS separately with
`scripts/benchmark_transactions.py`: generate a VCD with `--generate --wave
/tmp/transactions.vcd --workload dense --size 16384`, then run without
`--generate`; `--source-root` selects an isolated checkout. The harness also
provides long-idle, high-outstanding, multibeat and early-write workloads with
independent arithmetic expectations, first/hot requests, parser-open cost,
native call counts and serialization timing. OS caches are not flushed and
these measurements exclude MCP transport.

There is no cross-call transaction result repository or persistent transaction
cache. The shared TL-UL input and bounded normalization table have request-local
lifetimes. Repeated benchmark calls alone are not evidence of an actual repeated
analysis workflow, and a short mapped sample cannot establish a cache benefit
for long multibeat/high-outstanding workloads. A future cross-call cache needs
its own source/artifact/selection/semantic identity and lifecycle evidence;
Source Graph and structural-scan cache keys do not supply that contract.

### Protocol acceptance sampling

`inspect_handshake`, `sweep_handshakes`, `reconstruct_transactions` and
`inspect_tlul` sample controls, reset and payload strictly **before** each selected
physical clock edge. Their additive `sampling_phase="before"` receipt describes
this corrected default. An NBA update at the accepting edge cannot erase a
transfer or create a false premature deassertion. Every data event at the same
timestamp is excluded, including groups spanning event pages. This models
synchronous setup values; the waveform does not prove simulator delta ordering
for a testbench that drives inputs with blocking assignments at the clock edge.

The private sampler uses raw femtoseconds and retains separate physical edges
even when their public ceiling-to-picoseconds labels coincide. Missing prefix
values stay unknown; no point query fills them with a later value. An unknown
clock or conflicting clock events at one timestamp stops analysis at the proven
prefix. `transition_data_truncated` remains the compatibility partial-coverage
flag; warnings or gaps identify `clock_unknown`, `clock_event_order_unresolved`
or `legacy_sub_ps_order_unavailable`. Old wrappers retain before-edge support
for integral-ps scales; without raw event paging, sub-ps ordering is unprovable
and returns partial coverage. No empty result in that path proves a clean bus.

Protocol reads reuse existing native groups and close event cursors before
unloading, including cancellation and errors. Standalone handshake reads cap
each signal at 1,000,000 events / 64 MiB decoded accounting; transaction/TL-UL
reads retain their request budgets above. This bounds retained inputs, not FFR
residency or parser/index memory. Shared columns and reuse identities include
the sampling phase. Display caps and cursor names never affect acceptance.
Sweep metrics distinguish event open/page/close call counts and elapsed ABI
time from legacy profiled transition reads. Page volume contributes to native
transition/output totals; unavailable internal seek/traverse phases are not
inferred from page wall time.
The general `get_signals_by_cycle` and `verify_window` sampling defaults remain
unchanged; callers can still inspect post-edge state through those tools.

Dynamic driver observation resolves typed source references to exact dump
declarations before reading values. A missing FSDB vector suffix is recovered
only from the source's declared coordinates and exact declaration metadata;
there is no basename search. Ordered bits, ascending/negative ranges and aliases
retain their declared meaning. Read paths are local bindings: dependency names
and A/B mappings retain source identities. X history shares this declaration
resolver. Missing declarations, invalid coordinates and cancellation retain
their existing distinct outcomes. Binding does not prove delta-cycle order,
asynchronous behavior or completeness of the driver inventory.
