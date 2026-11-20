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
  src/path_discovery.py          # VCS/Xcelium/Verilator simulation artifacts
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
  src/fst_parser.py              # optional basic digital queries with gap receipts
  src/fst_runtime.py             # bounded, cancellable process transport
  src/fst_worker.py              # isolated libfst reader; request-scoped native state
  src/cycle_query.py
  src/waveform_batch.py           # FSDB/VCD/FST batch readers (time-window)
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
  src/fsdb_sweep_stream.py        # bounded event pages / edge blocks for capable FSDB sweeps
  src/fsdb_count_index.py         # parser-local cumulative counts for covered time windows
  src/tlul.py                     # explicit A/D mapping over handshake/transaction engines
  src/packed_layout.py            # packed-member export from trusted semantic artifacts
  src/txn_reconstruct.py          # reconstruct_transactions: id-correlated transaction layer

Native integration
  libfsdb_wrapper.so
  native/fsdb/fsdb_wrapper.cpp
  native/fsdb/fsdb_point_read.h
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

- Optional driver dependency inventories bind bounded typed references before
  applying the 32-selection limit. Compatible references to the same source
  and dump declaration combine exact ordered bit sets only when declaration
  shape, roles and active level agree. Distinct source aliases and nonidentity
  packed-member mappings remain separate; sparse bits never become a range.
  Feedback is limited to intersecting state bits. Expression-node, deadline,
  output and cancellation limits still apply, and removing a selection gap
  does not establish complete driver coverage.
- Project-owned C/C++ code lives in `native/<module>/`; external dependencies
  live in `third_party/<dependency>/`. FSDB runtime symlinks remain under
  `third_party/verdi_runtime/linux64/`. `scripts/build_wrapper.sh` builds the
  FSDB sources into the repository-root `libfsdb_wrapper.so`, preserving the
  Python loader path and `$ORIGIN`-relative runtime lookup. The portable PyPI
  distribution excludes native sources and shared libraries.
- VCD point and window reads bisect the existing ordered transition records;
  separately dumped bits retain their declared indices (`valid[0]`, `valid[1]`)
  and disjoint slices retain their ranges. A unique vector range remains
  accessible by its traditional base name as well as its exact declared range;
  multiple bit/slice declarations never silently overwrite that base name.
  Symbol aliases continue sharing the same transition records. Point/window reads
  do not rebuild a timestamp list or scan the preceding history. Equal
  timestamps keep file order, including rounded sub-ps events. Query work is
  O(log N + returned events) with no persistent duplicate time index. Initial
  parsing retains the decoded input and complete transition records, and splits
  whitespace-aligned token chunks of about one million characters. An individual
  wide token can exceed that size. Iterator dispatch avoids retaining a whole-file
  token list; no signal, scope or time-window filtering is applied during loading.
  Cancellation checkpoints between chunks discard partial parser state so a later
  request can retry without duplicate records.
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
  from this restriction. `native/fsdb/fsdb_point_read.h` saves the current bounds before
  changing them, releases the traversal and load, then restores those bounds
  on success, failure and exceptions. Missing bounds or unsupported windows
  use the previous loading path; an empty partial read retries the original
  view once. Cleanup failure closes the Python reader before any later query.
  Active transition groups keep their existing load/view; point reads reuse
  group members and reject nonmembers without disturbing the group. Range and
  around-time reads keep their existing predecessor/history behavior. Native
  calls still run under the global FSDB lock and cancellation is observed after
  the native call returns.
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
- Structural scan results separate lexical and semantic coverage.
  `lexical_coverage_status` preserves the lexical scan's coverage. The top-level
  `coverage_status` starts with lexical coverage and is also degraded when an
  explicitly requested `deep` scan has incomplete semantics. `semantic.status`,
  `semantic.gaps` and `semantic.propagation` describe semantic work. Complete
  lexical coverage with no findings is not a semantic pass.
  `zero_coverage` and `degraded` preserve source/parser uncertainty, and
  `semantic.status=not_run` means semantic checks did not run.
- Handshake sweeps are optional investigations. Missing or partial sweeps never
  set `required_next_call`, `workflow_incomplete`, or a missing workflow step.
  Recommendations use `suggested_next_actions`; diagnostic sections use
  `suggested_call`. Both retain `not_run` or the actual scope/window coverage,
  including when no finding exists. `not_run` is distinct from `zero_coverage`.
  Protocol coverage and retry actionability are separate. Actions carry
  `reason_code`, `addresses`, and `remaining_gaps`. Discovery limits prefer
  metadata discovery in observed child scopes; the scope is a choice, not a
  symptom or root-cause ranking. A larger interface cap is suggested only when
  it is the sole known gap. Transition-prefix limits need a shorter window:
  an observed stall anchor may supply its start; an unknown anchor remains a
  prerequisite rather than an invented time. Actions preserve wave identity
  and applicable sampling parameters. Server routing accepts smaller scopes
  and windows even for `truncated` results without upgrading global coverage.
  An unscoped zero-interface sweep
  and a degraded sweep with no such action retain their warning/coverage receipt
  but do not generate an identical suggested call. Recommendation output carries
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
  worker's Linux RSS receipt reads `VmRSS` and `VmHWM` together from
  `/proc/self/status`, so the peak belongs to its current executable address
  space and includes allocations already released. If `VmHWM` is unavailable,
  the resource high-water fallback is conservative and may include pre-exec
  history; unavailable observations remain unknown. Live polling and the
  reported-peak guard both remain active at the configured limits. A runtime
  frontier outside the context takes the historical one-shot route without
  discarding the still-useful parent session. An opt-in, default-disabled disk
  tier stores only canonical ConnectivityIR JSON plus a versioned manifest
  under `TRACEWEAVE_CACHE_DIR/source_graph/disk-v1`. It performs a direct
  exact-identity lookup only after a memory miss, never scans at startup, and
  never bypasses fresh adapter content validation. A verified hit constructs
  an independent query engine; corruption is a safe miss and failed/cancelled
  builds are never published.
  Adjacent-artifact reuse is classified as
  `not_needed_existing_artifact_scope`, separately from a semantic-session hit.
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
  indexes are built once per match without changing IR or public result schemas.
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
  `CompileInputCapture` is the independent preparation primitive for log and
  filelist evidence. An optional call-local reader binds VCS, Xcelium and
  Verilator parsing, preprocessor options and compile-environment inference to
  the exact captured raw bytes. Ordinary parsing keeps its streaming I/O.
  Capture has file/reference, byte and cooperative time budgets, rejects
  non-regular inputs, records original path aliases, and rehashes content before
  producing an immutable snapshot. Cancellation and capture aborts propagate
  past parser warning handlers. Closing the capture clears all source bodies;
  snapshot completeness describes captured inputs, not semantic include
  closure. This primitive does not build a hierarchy, run a frontend or load
  a KDB.
  The `prepare_design_context` core and MCP entry use this capture independently
  of `build_tb_hierarchy`. It preserves ordered primary/supplementary evidence,
  captures simulator-library units and recorded support files, and follows
  preprocessor dependencies without materializing expanded HDL or scanning
  instances. It records include search candidates and original path aliases;
  content, environment, selection or alias changes invalidate the context.
  Log-proven environment replay must validate the ordered project-unit sequence
  before its resolved filelists replace unresolved parser spellings. Proven
  bindings are retained explicitly; unrelated parse warnings and unverified
  replay continue to block complete input claims.
  Cross-unit macro lifetime, phase-local options, unexpanded dependency macros,
  opaque/protected inputs and missing inputs stay explicit gaps. Source-stage
  budget exhaustion may return a partial context; parse failure, cancellation,
  timeout or changing inputs cannot publish one. Compilation replay, semantic
  coverage and KDB association still require their own validation.
  `DesignContextStore` owns immutable metadata under process-specific `dc_`
  handles, with at most eight entries sharing a 64 MiB conservative metadata
  charge and leases protecting busy entries from eviction. This total budget
  was raised from 16 MiB for large SoC include-resolution inventories; it allows
  up to 48 MiB more accounted metadata, while retaining the same accounting,
  eviction and capacity rejection rules. It is not a process RSS limit.
  Registration and acquisition revalidate content; neither registry
  entries nor released captures retain source bodies. Preparation and content
  validation run in a cancellable non-wave thread. A validated registration
  ticket is published on the event loop only after a cancellation checkpoint;
  an abandoned worker owns no registry entry and cannot evict an existing one.
  Queue wait, capture and registration share the request deadline. Session
  reset replaces the registry generation, preventing an older pending request
  from publishing into the new session. Explicit contexts survive ordinary
  discovery changes; each acquisition revalidates its own inputs.
  The MCP receipt separates captured-input completeness, include resolution,
  and unexecuted semantic/KDB checks. It returns full identity and ordered
  recovery inputs without promising semantic capabilities. The optional
  `context_identity_sha256` preparation input makes those recovery inputs
  directly replayable: capture must reproduce the expected compile/source
  identity before registration validation or publication. A match issues a
  new process handle and reports `matched_recovery_identity`; it does not
  recover a native session, loaded KDB generation or old registry entry.
  Partial-input and include gaps remain part of the identity and receipt.
  The optional
  `kdb_path` is an identity-bound declaration, not eager KDB discovery or proof
  of source association. Driver/load/path and X snapshot/history accept `design_handle` and derive
  compile arguments from it; supplied conflicting arguments are rejected.
  `find_tb_instance` and `get_tb_subtree` also accept an independently prepared `design_handle`:
  exact path lookup, bounded/exact module search and subtree browsing use the existing NPI source
  query service without building the full lexical hierarchy. This route returns
  schema version 2; legacy `handle` queries retain their existing shape. The
  context stays pinned through source/KDB validation, native cleanup and final
  validation. Explicit null counts/locations survive public serialization.
  Instance and definition locations retain raw NPI paths. Each role gains a
  `npi_instance_location_unbound` / `npi_definition_location_unbound` row gap
  when its exact spelling or captured alias is absent from captured inputs.
  This check includes every returned subtree node without enumerating more nodes.
  Such paths are vendor observations, not verified source locators. No basename
  inference or live path probing repairs them. A bound path alone still does
  not prove that the KDB was built from the current source bytes.
  Its deadline includes context validation and KDB binding; late completion
  keeps positive facts partial and discards exact negative/count proofs.
  Local native queries remain synchronous; explicit LSF uses the existing
  worker. This does not yet reuse NPI backend instances across public queries.
  `lookup_tb_files` and `get_tb_file_detail` also accept `design_handle`.
  They plan captured ownership before backend binding. Metadata-only filters,
  missing compile members and empty metadata candidate sets validate the pinned
  source context before/after execution, without KDB probing, selection or
  loading. These results omit optional backend/KDB receipts and retain context
  identity, coverage and cumulative query costs. Semantic file queries use the
  same NPI binding/cleanup boundary; their canonical members, captured aliases
  and ownership identities are validated before publication. Native file
  queries retain their opportunity to provide facts even for unavailable source
  bodies. On the Slang file fallback only, an incomplete raw snapshot or an
  explicit source-capture byte/file/reference/dependency-work limit returns
  metadata, unknown semantic predicates and `source_capture_incomplete`, plus
  the captured context gaps. This does not enter manifest planning or the shared
  artifact runtime. A stable partial capture is not an input change: the pinned
  caller still performs its full initial/final digest, alias and unavailable-path
  validation. The legacy manifest adapter's complete-snapshot requirement stays
  unchanged. Include/options/opaque-language gaps alone do not trigger this gate.
  Native file
  mapping currently walks bounded global namespaces; local output does not
  imply local-only work. Partial declaration inventories and unknown filters
  cannot prove absence. `get_tb_class_hierarchy` and all seven `dump_tb_section`
  sections also accept prepared contexts. Class results retain static inheritance,
  qualified/base identities, reference-only external bases and separate rendered
  versus declaration counts. Source locations are checked across class roots and
  exported instance/class trees; unbound class paths use `npi_class_location_unbound`.
  Exports validate the requested section, selected component root, and captured
  file/interface ownership before publication. Compile metadata exports bypass
  backend binding. Semantic exports may do global work within the original
  budget; `max_items` only caps output, and an exact count does not prove a full
  export. Legacy handles retain their raw sections and static UVM inference.
  Failure-event analysis and next-step recommendations consume copied candidates
  from these same source queries: exact log instance plus a depth-one selected
  top subtree, with one cumulative 256-scope/8192-call/1-MiB budget. Partial or
  empty native results never trigger a full tree to improve heuristic ranking.
  The analyzer core no longer prepares hierarchy. Source unavailability returns
  partial/log-only evidence without a legacy full-tree build; source and native
  facts are not merged. Semantic preparation is outside waveform locks,
  wave access uses the normal request worker/lock, and recommendation cache
  publication follows final context/binding validation. Legacy compile-log
  calls independently prepare a context within the same request deadline.
  Slang hierarchy queries use a capability-isolated, topology-free artifact
  through the existing Source Graph runtime when NPI is unavailable. The
  `hierarchy_scopes_v2` envelope has its own strict codec, actual frontend top
  observations and exact search digest, including aliases, range, mode and all
  work limits. Runtime and disk validation reject mixed connectivity/catalog
  facts, missing top proof, duplicate paths, invalid parents and inconsistent
  child completeness or definition evidence. Only copied primitives leave the
  worker. Generate/array owners and bind-introduced instances come from native
  membership; dotted prefixes propose exact ancestors but never prove edges.
  Module/interface/program/generate/instance-array facts form the supported
  domain; checker/primitive/uninstantiated objects retain an explicit owner gap.
  Instance and definition locations use physical parsed-buffer offsets bound
  to captured aliases. Value parameter evidence identifies module definitions;
  it does not claim canonical nominal module type-parameter identity.
  Ordinary subtree projection covers requested depth plus a child frontier;
  exact/unlimited local subtrees complete the actual root, while module search
  and full component export complete the selected-top domain. Exact path input
  admission remains64 atoms; the semantic walk supports256 levels. Limits are
  min(10000,remaining scopes/2),5000 at the default scope budget, min(50000,remaining calls/2),256 depth and up to
  2MiB retained rows/parameters/coordinate indexes, within the original request
  deadline/byte budget. Early limits without real top proof publish a fixed
  unstarted blocker. Later limits retain positive facts and null exact totals;
  their critical coverage evidence precedes capped diagnostic details. No local
  child completion promotes a global negative, and blocking diagnostics retain
  partial coverage. Different hierarchy ranges have exact identities; global
  module search and full-tree export share the same top-complete request.
  Construction work limits remain in producer artifact identity and strict codec
  validation, but do not gate warm hierarchy or source-catalog reuse. Queries
  reuse compatible captured facts independently of either producer/consumer
  limit ordering. Validated immutable payloads and scope/file indexes are retained
  with the runtime cache byte/eviction budget. Readers convert only visited rows
  and pay current lookup, traversal and output costs; historical native visits,
  coordinates and whole-artifact size are not charged again. Wider hierarchy
  reuse needs actual parent/depth or complete-children evidence; partial positives
  retain unknowns and never establish an unvisited complete negative. Source
  catalogs retain explicit file/capability coverage and captured ownership.
  Exact disk lookup, producer raw identity, cold budgets, final-waiter cancellation
  and source/policy validation retain their existing contracts.
  Default prepared failure consumers use one artifact and never rebuild the
  lexical full tree on source unavailability. NPI and explicit legacy paths
  preserve their existing priority and capabilities. The internal
  `hierarchy_source_service` instance-query engine consumes immutable
  `HierarchyScope` facts and provider-owned child streams. Exact path lookup
  does not enumerate siblings; bounded subtree browsing stops at display
  frontiers and returns a lower bound instead of an invented exact total.
  Subtree `max_nodes` includes the root; descendant counts exclude it. At each
  visible depth frontier a single omitted child can establish truncation;
  `count_mode=exact` explicitly permits traversal beyond the displayed tree,
  still within the request's work/depth/time budgets. Deep trees use the
  iterative source codec and JSON payload, preserving nulls without Pydantic's
  recursive `Any` serialization limit.
  Explicit exact counting can traverse undisplayed scopes under separate
  scope/call/depth/byte/time budgets. Output truncation and incomplete analysis
  have separate receipts. Every opened stream is closed on normal completion,
  early stop, cancellation or failure. Definition and instantiation locations
  are distinct, and generate scopes need not invent a module definition.
  Legacy handles and their exact counts continue through the existing adapter.
  The prepared NPI backend's internal `query_hierarchy` implements subtree and
  instance lookup through `npi_hierarchy_source`. It uses low-level Language
  `InternalScope` iteration, preserving real generate scopes, array elements'
  actual parents, and separate declaration/instantiation locations. It validates
  the selected top and exact scope ancestry without a top/sibling enumeration.
  Iterator exhaustion and early release have different cleanup paths. One
  synchronous exclusive query owns all Language objects and releases them at
  its end; nothing stores these objects in the design registry. Cleanup failure
  rejects the result and invalidates that backend's loaded state. Native setup
  allocation/latency and opaque KDB payload identity retain their limitations.
  This does not introduce a persistent NPI worker or change Slang/disk defaults.
  Type-parameter inventory remains unverified; definition IDs conservatively
  include the instance path rather than merging unproved specializations.
  `hierarchy_source_metadata` builds a separate bounded compile-file ownership
  index from the immutable context, including captured aliases and selected
  include dependencies. It performs no filesystem reads or live symlink
  resolution, lexical scanning or semantic loading. Compiler file-type hints
  and reported include edges retain their compile-metadata provenance; they
  are not semantic declarations or a complete preprocessor include tree.
  Basename/path filtering and the three compile-record exports can use this
  layer without loading a frontend. Semantic file filters, file symbols,
  classes and the remaining exports still require the shared provider work.
  `hierarchy_source_files` supplies the internal file query contract: an exact
  compile-member miss returns basename suggestions before semantic work, and
  metadata-only lookup leaves semantic fields null. Declaration streams bind
  exact file identities/aliases; a same basename never establishes ownership.
  Module/type/UVM predicates preserve unknown separately from a proved false,
  and count modes distinguish a positive prefix from exact matching totals.
  UVM import and UVM usage are separate facts; no import does not prove no UVM.
  File, declaration and byte budgets cover semantic work independently of the
  displayed page. The internal NPI file adapter maps observed namespaces and
  typedef relations to exact captured files. It charges the bounded global
  namespace walk and retained copies explicitly as `global_namespaces_bounded`;
  requesting one file does not prove that native work is local to that file.
  Copied inventory is shared across files only within the same Language owner.
  No source parse, Text-token inference or eager SDK list supplies declarations.
  Uninstantiated declarations, variables, imports and UVM usage keep explicit
  inventory gaps. Synthetic namespace observations and instance-qualified
  specializations carry separate declaration statuses. Typedef identity uses
  the actual scope/name relation; file-inventory rows do not project packed
  layout. Public file routing has control and limited real-KDB positive
  validation; inventory gaps remain. The Slang route uses a separately versioned
  `source_catalog` projection in the existing compact-IR envelope, with purpose,
  file domain, captured aliases, capabilities and traversal limits in its build
  identity. Connection and hierarchy artifacts cannot satisfy this capability.
  Its envelope contains no definitions, instances, bindings or top-instance
  connectivity rows. A separate `semantic_tops` list copies the frontend's
  actual elaborated top paths. Runtime and exact disk loads validate this proof,
  the requested file domain, capability, limits and primitive codec before
  accepting a catalog. They reject mixed topology and catalog metadata under
  an ordinary query scope; ordinary artifacts retain their top-in-IR check.
  `slang_source_catalog` copies real declarations and resolved type/import/call
  references; `slang_source_codec` validates primitive ownership, coverage and
  limits before `slang_source_provider` constructs immutable public facts.
  Scope traversal is iterative; native expression visits and retained payloads
  are bounded. Inventory, import and usage coverage remain separate. Source
  buffers establish processed-file evidence, including empty files and includes;
  missing/unprocessed files, generic members, opaque contexts and unelaborated
  definition bodies cannot yield complete negatives. Locations bind the
  original parsed source buffer; a bounded byte-offset/newline index supplies
  physical coordinates even with line directives. Macro expansion usage leaves
  an explicit caller-side usage/import gap instead of a complete negative.
  Definition and package declarations precede global buffer bookkeeping;
  their requested native physical buffers receive lazy, shared coordinate
  indexes. All buffers, including macro expansions, still consume the same
  finite visit budget. A later buffer frontier preserves earlier positive
  declarations while marking inventory/import/usage coverage partial. Index
  creation and every indexed newline also consume that budget and retain
  cancellation and byte-limit checks. Projector identity `1.21` isolates the declaration ordering and native-anchor
  identity semantics from earlier artifacts.
  Coordinate-index, retained top proof and class/type parameter observation bytes share the artifact byte limit
  and request accounting; top proof also consumes the member-visit budget.
  Declaration IDs bind the
  captured content context plus native semantic scope/kind/name and a captured
  fully-original physical owner/byte-offset anchor. This anchor is independent
  of requested files and retained coordinate indexes; out-of-domain parent
  identities do not read foreign source text. Unbound native ownership remains
  unknown rather than receiving a name-only identity. Canonical class/type
  specialization identity retains native matching. Symbol IDs remain local
  to the exact artifact. Applicable file-detail/filter queries reuse one exact
  artifact; another file domain requires honest new work unless an explicit
  compatible catalog superset is available. Catalog subset reuse preserves all
  other scope, aliases, selected top, capabilities, version and work limits;
  actual artifact capabilities/domain are decoded before a provider is created.
  Exact cache entries retain priority. Preparation and pure
  metadata queries still do not load a frontend or native provider.
  `hierarchy_source_symbols` defines copied source declarations and class-base
  bindings by semantic ID. Its internal class renderer keeps same-name classes
  separate, exposes unresolved/external bases and inheritance cycles, and never
  presents static declarations as runtime UVM components. Derivative lookup
  can require the provider's full class inventory; symbol and byte limits cover
  that work independently of display depth. The legacy class `total` remains
  the rendered-node count, with separate inventory completeness/count fields.
  The inventory counts provider declarations, excluding reference-only bases;
  unresolved inheritance or a cycle can make the view partial without erasing
  a completed declaration count. Budget stops invalidate the count proof.
  The internal NPI provider also projects observed classes through bounded
  `Instance(None)` / `InternalScope` iterators. It binds `extends` through the
  actual class Typespec/definition handle and charges every visited namespace,
  including namespaces without class declarations. This explicit class query
  can visit global compilation scopes; instance browsing never triggers it.
  Uninstantiated declarations and class specializations remain unverified
  inventory gaps, not complete negative results. All iterators and handles
  share the same exclusive Language owner and cleanup deadline as instances.
  Public class routing has control and limited real-KDB positive validation;
  Slang source fallback requests a separate `class_inheritance` catalog
  capability, alongside file declarations, with no connectivity topology.
  `slang_class_catalog` copies finite declarations and actual referenced
  ClassTypes, following bound bases, type parameters and instantiated nested
  scopes. It never requests default specialization or enumerates possible
  parameter sets. Actual canonical types are interned by `Type.isMatching`;
  names, displayed parameters, bit widths and declaration locations are not
  type equality. Every actual class node has an artifact-local canonical type
  identity, including non-generic nested types under different outer
  specializations. Its stable `declaration_id` and physical location agree with
  file declarations; file `type_id` retains its declaration-domain meaning,
  while class nodes explicitly report `type_identity_domain="artifact_canonical"`.
  Type-parameter bindings use those canonical IDs, and specialized bases use
  `base_relation="specialization"`. Generic declarations without actual bindings
  keep explicit frontiers; an explicit extends clause with no resolved base
  cannot become a no-base fact. Outside-inventory positive bases remain
  reference-only. Class visits, recursion, copied parameter bytes and strict
  codec counts share the existing limits; a stopped binding retains an
  incomplete gap. Class source fallback also preserves the stable partial
  capture gate after healthy NPI's opportunity. The bounded canonical type
  domain is `declarations_and_referenced_specializations`, not all possible
  specializations or runtime UVM topology.
  Explicit class artifacts refresh the live compilation diagnostics after
  projection to reconcile the actually observed snapshot with the session's
  earlier observation. Unresolved inheritance can coexist with a complete
  finite declaration inventory and zero SDK diagnostics; these are independent
  facts. This refresh does not invent diagnostics or prove SDK lazy-binding
  behavior. The worker reconciles IR counts,
  bounded diagnostic gaps, class/file coverage and the final scope receipt
  before serialization, without reprojecting or building another frontend.
  New diagnostic details share the catalog byte envelope and existing gap cap;
  count/status metadata contains no duplicate messages. Warnings preserve
  established negative predicates; blocking errors turn incomplete class/file
  coverage and import/usage negatives into explicit unknowns. Ordinary
  connectivity/file-only artifacts retain their existing execution path.
  File catalog coordinate indexing is restricted to requested captured files;
  confirmed native owners outside that domain do not become unbound in-domain
  declaration gaps. Catalog planning reserves at most half the remaining call
  budget, capped at50000 visits, for buffer/declaration/namespace work, leaving
  provider/rendering budget. Its scope/byte/deadline caps and exact identity
  checks remain; ordinary connectivity/trace/packed4096 ceilings are unchanged.
  `hierarchy_source_export` composes the seven internal section exports.
  Compile-record exports never touch a provider and charge their copied payload
  against the byte budget. `record_export_complete` describes a successful
  captured-record copy; reported coverage gaps still make `export_complete`
  false. Semantic exports reuse the instance, class and file
  engines with one cumulative budget. `export_complete` also requires that
  output is not truncated; an exact count alone cannot make a partial export
  complete. Version 2 data keeps semantic identities and separate locations;
  legacy handles keep their raw scan dictionaries and static UVM inference
  view. Prepared public section exports use these same engines.
  `hierarchy_source_request` validates all six operations before backend
  selection. Its request-local plan uses only captured compile ownership to
  detect metadata-only work, nonmember files and empty file selections. Those
  requests need no NPI/Slang acquisition. Catalog preparation and execution
  share one budget; execution rejects a catalog from another context before
  touching a provider. Cancellation/typed failures propagate, and a preparation
  limit reports partial/not-run without selecting another backend. The plan
  retains no source bodies or frontend state and is not a registry cache.
  Prepared local NPI `query_source` executes all six plans through the same
  exclusive Language owner used by the internal instance API. Metadata-only
  work returns before KDB binding, SDK initialization or owner acquisition.
  Semantic work requires matching captured compile metadata and the selected
  top; its receipt separates KDB load evidence from the unverified association
  with current source bytes. The caller still owns context pinning and final
  validation. All six public prepared-context tools use this boundary.
  The version 2 source schemas and `hierarchy_source_codec` preserve explicit
  unknown values instead of passing semantic facts through legacy defaults.
  The boundary validates shallow rows and iteratively checks scope/class trees,
  file ownership, count proofs and export completeness. This avoids a recursive
  model depth cap below the service's 256-level limit. Validation checks shape
  and consistency, not the truth of an NPI/Slang fact. The source worker and
  public routes use this boundary.
  `design_context_codec` supplies a bounded private metadata transport for
  prepared workers. It preserves ordinary and explicit-recovery identities,
  snapshots and path observations without source bodies or native handles.
  Its payload hash proves message integrity only; decoding performs no I/O,
  and workers must still validate current inputs before/after execution.
  The 8 MiB context payload cap is explicit. Protocol 4.0 adds a private
  `hierarchy_source` worker request with strict query limits and a required KDB
  fingerprint. Its request envelope permits 16 MiB; other operations retain
  their 1 MiB cap, and responses remain capped at 8 MiB. File reads are bounded
  before parsing. The worker rebuilds only the metadata catalog, validates the
  captured source observation before/after querying, and enters the same
  Language owner before loading a KDB. It rejects foreign context/artifact
  identities and non-NPI semantic facts. Context, KDB and native timeout errors
  have distinct fixed protocol labels. Mandatory final validation can outlive
  analysis limits but cannot publish complete coverage after the deadline.
  Decode, source/map validation and result validation costs are exposed.
  Prepared LSF `query_source` keeps metadata-only work local, before any
  configuration, binding or transport access. Semantic requests transfer only
  the remaining work limits; the parent's monotonic deadline also bounds queue
  wait and triggers bounded job cancellation. No monotonic timestamp is sent
  to another node. Parent and worker catalog costs are both charged, so a tight
  byte budget can stop LSF work earlier than the equivalent local query. Source
  request encoding bytes/time, parent preparation, round-trip time (including
  bound-map checks), worker time and result validation are separate metrics;
  aggregate `query_ms` retains the original parent start. A scheduled worker
  that stops before loading cannot gain a KDB load receipt from file markers.
  Ordinary provider failures carry fixed placement status and never invoke
  licensed work locally. Typed identity/timeout/cancellation failures propagate.
  Common connectivity workers also preserve a native `TimeoutError` through the
  fixed protocol code. Independent codec/worker/parent round-trip controls cover
  all six tools, seven exports and packed requests. Real LSF acceptance remains
  pending; shared paths alone do not prove matching stat/environment
  observations on another node. No persistent worker or verified checkout reuse
  is implied.
  Expired explicit `compile_context`
  recovery can prepare a bound design on the KDB candidate route (see below).
  `PreparedTraceContext` supplies source identity and compile metadata to the
  existing dynamic route without a hierarchy or legacy-handle lookup. Snapshot
  attempts retain waveform file/parser generation across whole-chain restarts;
  history retains its observation-session, time window, selection, cumulative
  work budgets and whole-graph restart rules. Context validation and first
  binding share the history deadline; final identity checks still run after an
  exhausted analysis budget. Typed identity/capture failures and cancellation
  cannot become fallback attempts. A clean/missing waveform target performs no
  driver query and leaves KDB load status unverified. Dynamic NPI queries record
  actual load evidence even when their semantic model returns partial facts.
  Independent-context Source Graph traces use the captured compile context and
  bounded semantic preparation described in "Prepared traces and packed layouts"
  below. Missing source capabilities retain honest bound Static evidence without
  borrowing a legacy tree. This trace integration is a D7 subset.
  Dual-side `trace_divergence` accepts these handles independently, including
  mixed legacy/prepared sides. `divergence_design_context` pins all unique
  handles before any await, then prepares them under the existing graph
  deadline without nesting binding locks. Prepared compile arguments come from
  the handle; explicitly repeated arguments must match, and legacy identity
  tokens cannot be combined with it. Design keys use immutable context identity
  rather than random handles. Legacy H and independent C identity schemes do
  not imply equivalence, so mixed designs still need explicit dependency maps.
  Per-side dynamic routes retain actual backend/load receipts; shared candidate
  evidence and preparation metrics identify their single `metrics_owner_side`.
  Equal roots do not load NPI. Source/map hashes run outside wave locks once per
  unique handle at final validation; a final metadata pass checks both contexts
  again after hashing and parser-generation validation. This is bounded file
  observation, not an atomic filesystem snapshot. Post-budget validation is
  measured separately and included in elapsed time. Typed lifecycle failures
  clear the graph and root comparison and return `inconclusive`; cancellation
  propagates. Registry ownership and cancellation are checked on the event
  loop immediately before cursor publication. No abandoned worker publishes
  registry state, and all request pins release on every exit.
  `diff_first_divergence` accepts the same explicit identity choice in
  `context_a` / `context_b`. Its `divergence_diff_context` wrapper pins unique
  prepared handles before the first await and validates source plus any
  existing binding outside waveform locks. It never creates a binding or
  loads a semantic backend to generate driver actions. Final source/map,
  waveform identity and parser-generation checks precede cursor publication,
  including when the waveforms match. Typed failures return an error without
  cursor/actions; legacy compile tokens remain intact for exact recovery.
  Both optional Slang cache tiers remain default-off.
  `src/kdb_anchor.py` separately captures a bounded on-disk KDB generation:
  two observations of five fixed elaboration maps, including raw hashes and
  aliases, must agree. It distinguishes missing, empty, source-only and
  elaborated candidates from error-marked or unrecognized artifacts without
  loading NPI. Directory mtime/size changes from temporary locks are ignored;
  root replacement and map changes invalidate the anchor. Read budgets,
  cancellation, unreadable/non-regular maps and changing inputs remain explicit.
  A complete map observation is neither a successful NPI load nor proof that
  compiled sources match current files. Unmapped database payloads are outside
  this identity, and every receipt preserves that limitation. Local and LSF connectivity
  backends accept a validated anchor, use its fixed path, and reject changing
  generations before loading and after querying. Bound compile metadata is
  copied and shared with driver/load/path and source fallback; it is not
  reparsed from a different log. Local loaded-design identity includes the raw
  map fingerprint for these bound calls. The LSF protocol (4.0) carries the
  expected map fingerprint and budget; the worker validates before load and
  after query, and reports fixed identity-change/timeout failures to the parent.
  These failures and cancellation propagate without starting Static fallback.
  Unbound library callers retain automatic candidate selection. Candidate
  binding is an identity boundary, not NPI session pooling or source matching.
  `design_backend_binding` fixes one candidate, absence, or source policy on the
  handle's first connectivity query. A no-I/O pin protects registry capacity
  across the full asynchronous call; a per-entry lock coalesces first discovery.
  Preparation/validation workers never publish state. The event loop publishes
  after a cancellation checkpoint and checks its registry generation again
  before returning a result. Source content, KDB maps and relevant execution
  policy are checked before and after querying, outside waveform locks; TOP
  wave binding consumes captured compile metadata without a hierarchy lookup.
  Source-only policy performs no probe or NPI selection. Confirmed NPI load
  unavailability is remembered until a new context is prepared; ordinary query
  gaps remain retryable. Binding metadata is charged against registry capacity.
  Receipts distinguish a candidate, successful load, and unverified source
  association, with first-discovery and repeated-validation costs. New-handle
  driver/load/path fallback shares `prepared_source_connectivity` over its
  immutable captured manifest and content snapshot. Exact bounded candidate
  prefixes are resolved by Slang; only actual instances enter the IR, so packed
  fields and generate scopes are not guessed lexical instances. Endpoint parents
  select real assignment scopes together with their immediate real parents
  (for internal binding nets) and the real endpoint LCA. Other ancestors retain
  port/binding skeletons until a bounded expansion needs their assignments. A failed path
  search can request at most two semantic neighborhood expansions, rooted in
  the real endpoint LCA, and restarts from the original endpoints in one artifact.
  The artifact key includes the semantic search specification and remaining
  resource limits. Candidate/expansion scopes require exact projection/search
  semantics for reuse (capability subsets remain valid). The cumulative request
  budget charges repeated scope/definition-member work, including warm artifacts,
  conservatively; `actual_build_count` separately reports actual frontend work.
  Preparation, healthy NPI and legacy compatibility do not acquire this service.
  `timeout_sec` owns the original caller deadline through final validation; a
  stable worker cap preserves exact single-flight sharing and each caller's
  deadline independently detaches its waiter. Final-waiter cancellation reaps
  the worker. Cancellation, timeout, changed inputs and analysis budget failures
  never advance fallback. Ordinary compile-log/old-handle routing is retained.
  Frontend parsing/elaboration can still cover the full compilation, even though
  IR projection is bounded. Prepared X/divergence tracing and the six-tool Slang
  hierarchy/source service are described separately above and below; this
  planner does not change cache/session defaults.
  The optional `bounded_kdb_discovery` scope reuses that filesystem probe with
  one directory-entry budget (4,096 by default) and a caller-owned input
  capture. Setup files have a 64-KiB size precheck; compile option/filelist
  replay and managed-cache hashing share the capture's file/byte/time limits.
  It omits optional vendor error-log reads and propagates cancellation, budget
  and changed-input failures. The ordinary probe and explicit KDB builder keep
  their existing behavior outside this scope. The owner must close the capture
  and account for its reads; this does not eliminate first-query discovery cost.
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
  parsing, manifest translation, bounded projection, workers and KDB input
  extraction share `hdl_suffixes.py`: `.v`, `.vh`,
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
  buses are outside scalar discovery coverage; empty discovery cannot certify
  them. Each inspected row retains its actual check coverage.
  Paging and sampling run under the existing wave locks, including the one global
  FSDB lock. Cancellation is checked between pages and after native calls; native
  open and individual native calls are still not interruptible internally. Time
  conversion, four-state values, predecessor and transition truncation are unchanged.
- A full `sweep_handshakes` does not independently reread and re-extract the
  same clock for every interface. `handshake_sweep` groups discovered bundles
  by clock. The compatibility path creates one private `EdgeSamplingSession`
  per group;
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
- Full sweeps use a private column-oriented sampler: one edge-time vector and
  one value-reference column per signal. They do not materialize `time_ns`, a
  per-edge `signals` dictionary, or a normalized `{bin,hex,dec}` copy for every
  sampled value. Standalone sampling tools retain their existing row-oriented
  result schema. `inspect_handshake` consumes either representation with
  identical facts, and advances the AHB write-data hold state machine in the
  same pass as the main handshake state machine. Signal lookup uses a monotonic
  transition cursor (with a bisect fallback only for unexpected decreasing
  sample times), preserving duplicate-timestamp and pre-first-transition
  behavior.
- For FSDB clock groups, `FSDBParser.transition_group()` uses an optional native
  ABI to add the group's resolved signals once, call `ffrLoadSignals()` once,
  read each signal independently through the existing reusable 64 MiB per-call
  output buffer, and unload in a `finally` block. This removes repeated
  per-signal load/unload without adopting the multi-signal batch output format
  or changing truncation receipts. The default
  native group limit is 16 signals to bound resident FFR data on multi-GB waves;
  the sweep scheduler first-fit packs complete small clock units into that
  bound. On the compatibility path an oversized clock unit is split only at
  interface boundaries while retaining one `EdgeSamplingSession`: the first
  chunk reads/caches the clock,
  later chunks load only their signal subset, and a single interface that
  itself exceeds the bound falls back honestly. This removes whole-group
  oversized fallback without raising the resident-signal limit. Packs remain
  serial under the process-global FSDB lock and every native group unloads in
  its existing `finally` path.
  The sweep uses the private `_event_transition_group(paths, start, end)`
  entry point when available. With the lazy native ABI this reserves the same
  packed signals without `ffrLoadSignals()`; the existing event cursors load
  their histories on demand. It keeps the original native view and requested
  window, including the default full-duration request, so predecessor and
  exact-time sampling semantics remain intact. Wrappers without lazy support
  retain eager grouping. FFR session decompression can still retain substantial
  memory independently of the Python sampling strategy.
  With event-page and transition-group support, FSDB sweeps consume bounded
  pages and sample blocks through `fsdb_sweep_stream`; other backends and failed
  or unsupported groups retain the compatibility sampler. Each cursor keeps
  one page (at most 1,024 records / 256 KiB native output). Each block contains
  at most 2,048 edges and an 8 MiB conservative logical value budget; a single
  row exceeding that budget stops with partial coverage. Page objects, reader
  buffers, protocol latches and native residency are separate costs, not part
  of that 8 MiB estimate. Consumed columns and clock history are released.
  Shared-clock interfaces consume the same block columns within a pack.
  Chunked clock units reserve a clock slot in every pack and reread that clock
  once per pack rather than retaining full clock history across packs.
  `HandshakeConsumer` runs the same protocol loop used by standalone
  `inspect_handshake`. It preserves open stalls, latched payload/valid,
  X-episode debounce, AHB outstanding writes/data phases, counts, longest
  intervals and both independent finding quotas across blocks. Only final
  settlement closes a tail stall; finding display limits never stop counting.
  Physical femtoseconds and whole timestamp groups preserve strict-before
  sampling, including events spanning pages and sub-ps edges sharing a public
  ps label. Unknown/ambiguous clock history stops at the safe prefix; missing
  signal history yields unknown samples with explicit incomplete coverage.
  One request-wide budget caps streaming at 50 million page records, 32 GiB
  logical event work, or 300 seconds, independently of the block bounds. A
  record/byte budget may overshoot by one bounded page. Stops carry fixed
  `sweep_event_budget`, `sweep_byte_budget` or `sweep_time_budget` sampling gaps
  and transition truncation; they never assert complete coverage. Discovery,
  interface caps and skipped candidates remain separate coverage gaps.
  Cursors close before the native group unloads, including exceptions and
  cancellation; the global FFR lock and native-call cancellation boundary are
  unchanged. Numeric operation metrics expose block/page peaks and work stops,
  not paths, signals, sampled values or block identifiers. Logical Python
  working-set estimates do not imply a constant process RSS: FFR can decompress
  or retain entire flush sessions, especially with multiple live cursors.
  The private sweep cursor fast path consumes a single ordinary transition only
  when its successor is strictly later or absent. Pending initial observations,
  equal timestamps and uncertain ordering retain the generic group fold;
  cancellation checkpoints and truncation checks are preserved. Cached peek
  results avoid repeated lookups without changing page or work budgets.
  `TRACEWEAVE_FSDB_GROUP_MAX_SIGNALS` can set 1..256 after RSS review. Oversized
  groups, begin failures, and older wrappers automatically use the legacy
  per-signal path. Cancellation between interfaces unwinds the context before
  releasing the process-global FSDB lock.
- Capped public FSDB transition queries retain only the requested detail
  prefix while counting the rest through the event-count ABI. A parser-local
  exact-count LRU holds at most 1,024 signal/window keys or 256 KiB of accounted
  memory. Changing the detail cap can reuse that count; values and the true
  predecessor are always reread. Partial, cancelled and failed queries never
  publish an exact count. Internal full-list reads retain their existing
  contract.
  The optional `fsdb_event_count_index_v1` ABI also records a proved boundary
  between complete public-picosecond groups. `src/fsdb_count_index.py` stores
  cumulative counts strictly before these boundaries, so same-tick records
  and sub-ps ticks rounding to the same public ps are never split into a false
  complete boundary. A subsequent contained window can use the cumulative
  difference for its interior and count both edges exactly. An already-read
  detail prefix may supply the left edge. When the saved interior is at most
  65,536 records and an extra native edge read would be required, the query
  continues counting on its current cursor instead of paying for extra loads.
  Each index has at most 32,768 points, coarsening without changing cumulative
  counts; the parser's LRU admits at most 16 indexes or 4 MiB of accounted
  memory. Builders have a separate point bound and coarsening can temporarily
  retain old and new arrays; these budgets are not process RSS limits.
  Only a fully covering index is eligible; uncovered or expanded windows and
  old wrappers use the exact scan. The first scan still reads the requested
  history. Indexes publish only after cursor/group cleanup, cancellation and
  file-identity checks. Real path, device, inode, size, nanosecond mtime/ctime
  and parser epoch protect reuse; close/reopen clears both caches. No index
  survives a process restart or is persisted to disk. Public schemas, closed
  time windows, the global FFR lock and cancellation checkpoints are unchanged.
- For a unique, unaliased event cursor owning one signal, validated complete
  multi-session metadata can defer the existing record/byte-triggered reclaim
  across at most two adjacent metadata spans from the saved anchor. Crossing
  that bound or EOF still uses the existing unload/restore path, preserving
  raw-tick ordinals. Invalid metadata, aliases, multiple cursors or multiple
  signal IDs retain the original reclaim budget. This amortizes repeated
  decompression and cleanup, but FFR session sizes and demand-loading remain
  outside the progress budget's control; two spans are not a resident-byte
  guarantee. No native handle is retained by the count caches.
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
  its long Python loops.
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
  and 16 MiB of digest/include facts, never persistent source bodies. The identity
  reader additionally retains at most four complete immutable identity graphs /
  8 MiB of estimated metadata (bounded further by its configured capacity).
  An exact compile-context hit still replays preprocessor options (including
  environment-dependent paths and directory aliases), hashes current bytes and
  re-resolves literal includes, including earlier shadow candidates; it skips
  rebuilding the unchanged graph. Each validation shares at most 4,096 identical
  name/ordered-search-path lookups within that pass; later validations resolve
  them afresh, and capacity bypass still checks each choice. Incomplete/oversized
  identities bypass this tier. No source bodies or semantic coverage proof enter
  it. A warm result hit skips source preload and all rules. Exact concurrent
  callers share one build;
  one cancellation preserves it, final-waiter cancellation stops it. Results
  computed across a source change are degraded and never published. Set
  `TRACEWEAVE_STRUCTURAL_SCAN_CACHE=0` to bypass this optimization.
  An already joined source lease survives identity capture and transfers into
  the rule build on a miss, even if hierarchy finishes first. Cache hits and
  coalesced callers release unused leases.
- `structural_semantics.py` scans elaborated active instance bindings and
  specialization templates directly, without a ConnectivityIR build. It reports
  input/open-port facts, constant regions of input concatenations and continuous
  assignments, and named/literal constant comparisons. Statement source and
  enclosing consumer context remain evidence, not defect verdicts. Sequential
  reset/initial values are not treated as permanent ties. Definition buffer
  identity plus all value parameters form template identity; type parameters
  and external hierarchical dependencies conservatively retain separate
  instance templates. The isolated worker has time/RSS and instance/fact/AST/bit
  bounds. `fast` stays lexical. `auto` / `deep` first try existing KDBs through
  NPI; missing categories use this source service. `auto` permits completed
  memory-result or compatible query-IR reuse only; `deep` also permits construction.
  `analysis_mode` is a per-invocation MCP argument, defaulting to `auto`; no
  environment setting selects this mode. `deep` permits a cold frontend build
  but still reuses an exact completed result. `semantic_scope` limits basic
  fact extraction, not necessarily frontend parsing/elaboration. Reuse requires
  matching identity, selected top, scope and work/memory budgets; a completed
  category superset may supply just the missing categories. Unused time allowance
  does not affect a completed result's identity. The category list replaces
  the default selection rather than extending it. See [Structural Scan Invocation](#structural-scan-invocation)
  for JSON examples. The lexical scan still runs on a semantic miss.
  Lexical and semantic coverage are independent; output trimming preserves
  counts and marks `output_truncated`, while computation limits mark partial
  analysis and prevent publishing a completed cache entry.
  `auto` cache probing keeps metadata/policy/deadline guards without repeatedly
  validating source content for absent candidates. Positive source reuse still
  requires content validation, and every supplementation attempt retains final
  source-content validation, including a cache miss. `deep` keeps full source
  guards around preparation and construction.
  Semantic facts are structural observations, not confirmed defects.
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
  targets are boundaries, including `always_comb`.
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
  login-node parent performs the fallback. The public dispatcher also tries
  bounded Source Graph before final Static fallback; prepared driver/load/path
  share its compile-context planner. Static itself returns
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
  collared in two `native/fsdb/fsdb_wrapper.cpp` helpers (`_ToTag` floor / `_TagToPs`
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
  native ABI.
- `src/waveform_batch.py` provides `WaveformBatchReader` — a time-window
  multi-signal reader with FSDB and VCD implementations sharing the same
  shape. The FSDB path uses `ffrCreateTimeBasedVCTrvsHdl` for a single
  chronological walk; the VCD path is pure Python.

## FST Basic Reading

Private event pages distinguish `initial_state` from a genuine `predecessor`.
Typed recording-boundary events invalidate carried state, while separate
`RecordingGap` intervals retain the reason and endpoint semantics. `GroupCursor`
folds an initial observation at its physical time (or exposes a pre-window state
anchor), preserving complete same-time groups across pages. Selection projection
preserves those fields. Existing FSDB/VCD pages keep their previous defaults.

FST event sessions perform a masked native traversal per batch into private
per-storage spools, then reap the child before exposing logical readers. The
optional prefix probe described below may add one traversal. Aliases
reuse immutable storage with independent positions. Across a batch, at most 128
backing declarations and 64 MiB of temporary event records are admitted; a shared
30-second deadline includes preparation, admission and consumption. Requests
that exceed the spool budget fail explicitly. Only selected-window observations
and actual prefix evidence are retained, with bounded page decoding in Python.
This is eager window preparation, so early-exit comparisons still pay its scan
cost; narrow windows can still scan the selected signals' native prefix.
Spools, readers and positions expire on close, cancellation or file/epoch change.
They never enter a persistent cache. Generic page support is an internal
capability; public tools require explicit support for each reader capability.

FST supports cycle and expression queries. Sampling preserves integer fs
for both before and after phases, including several edges sharing a public ps
label; cycle rows additionally expose `time_fs` and `clock_period_fs`. Initial
observations seed state without inventing a transition; recording boundaries
invalidate it. An unknown, interrupted or ambiguous clock stops the trusted
prefix. Expression types, fixed selections and sparse/template array bindings
reuse the common engine. Exact declaration lookup remains independent of the
analysis gate, and local windows retain their separate conservative inference
policy. Column sampling shares request-local spools across backing dependencies;
no FST clock index is retained across requests.
Full sweeps also prepare one FST batch per existing bounded signal pack. Clock
and interface consumers reuse that pack's immutable spools; the batch closes
on success, error or cancellation before the next pack begins. FSDB continues
to use its existing native transition group and process-global lock.

The repository's optional `fst` extra pins `pylibfst==0.2.1` (BSD-3-Clause Python binding,
bundled MIT libfst; compression dependencies retain their upstream licenses).
TraceWeave supplies an adapter, without vendoring upstream source or shipping
another native bridge. Use the dependency's wheel for the server interpreter;
source builds follow the dependency's own build requirements. Existing
FSDB/VCD installations do not import or require pylibfst.

On Linux, `get_sim_paths` discovers `.fst` alongside existing formats, retaining
newest-first/path ordering and explicit `wave_file` selection. Its additive
`fst_runtime` receipt and `--doctor` report missing/wrong-version dependencies
without loading native code. `candidate_ready_unloaded` is only a dependency
probe. An actual `get_waveform_summary` reports the loaded extension path,
SHA-256 and version under `fst_backend`. Reconnect after code/dependency changes.

Basic reads include `get_waveform_summary`, `search_signals`,
`get_signal_at_time`, `get_signal_transitions`, and `get_signals_around_time`,
including fixed `{path,bits}` selections. Declaration aliases retain distinct
paths and coordinates; an around-time call reads each storage handle once.
Missing vector coordinates cannot support a structured selection. Unknown
direction/type/signedness or array layouts are never inferred. Scope paging
uses private identity-bound cursors; no public event cursor is introduced.

Raw times are integer fs; public `time_ps` labels use ceiling and `time_fs`
preserves exact event order. The first value at the file start is an
`initial_state`, separate from true changes and the strictly pre-window
`predecessor`. A late window can skip older blocks only after a separate bounded
probe witnesses a callback strictly after the first loaded block's begin tick
and strictly before the window for that storage handle. The block's initial
snapshot cannot satisfy this test. The final filtered traversal must therefore
replace that snapshot with a genuine predecessor before returning events.
Batch reads partition handles by this evidence: at most one masked full-prefix
traversal serves unwitnessed handles and one filtered traversal serves witnessed
handles, after the common probe. A constant or sparse field no longer forces
unrelated dense clocks to replay their full history. Without evidence, that
handle still scans from the recorded beginning; files with dump-inactive
intervals always retain the original full-prefix path. No block/event
index or prefix cache survives the request. The probe adds a traversal and may
cost extra on sparse or mixed selections that need the fallback; private native
callback/iteration totals include all passes. Libfst's block time filter is
not a strict event filter; the adapter
clips by physical time and consumes every admitted page before reporting a
complete result. Same-tick callback order does not prove delta/NBA order.
Actual X/Z, missing signals, times outside the recorded range, dump inactivity,
and a resume without a newly recorded value remain distinct. `value_status`
and `fst_reading.gaps` qualify evidence; two-state inputs establish only their
recorded observations. Summary metadata coverage does not certify signal data.

`src/fst_worker.py` owns native state in a request-scoped child process.
The parent never imports the native library, converts to VCD, or retains an
event index. Basic reads pause the callback between bounded IPC pages; batch
analysis writes only bounded selected-window spools before exposing readers.
Normal completion, errors, cancellation and deadlines terminate/reap the child
and remove its private temporary directory before releasing reader admission.
This is deliberate isolation for a callback API without cooperative native
cancellation. FST requests use the existing per-path wave lock plus a parser
lock; each native handle has a separate process. FSDB keeps its global lock.
File realpath/device/inode/size/mtime/ctime changes invalidate reads and cursors;
close/cache replacement rotates parser identity. There is no persistent FST
index or result cache.

Within one synchronous request, metadata pages, searches and exact declaration
queries reuse one thread-owned worker/index. Nested consumers share that lease;
AnyIO context propagation into another thread creates a separate owner, never
a shared IPC pipe. Switching parsers releases the old worker before acquiring
the next parser lock or admission slot. Stream/batch reads consume the lease;
a batch transfers only its private spool directory and reaps the worker before
exposing pages. Later reads may build a new index, so this is not a persistent
native reader or a promise of one build for an entire multi-batch analysis.
Parser close also reaps idle leases, invalidates epochs and rejects stale
owners. The synchronous request's finally block reaps remaining metadata work
on completion, error or cancellation. Lock/admission/IPC waits retain the
cumulative public deadline. Direct Python consumers can use
`fst_runtime.request_budget()` to obtain the same bounded request lifecycle;
individual calls outside that context continue to close their own workers.

| Resource | Bound |
|---|---|
| Native children | 4 process-wide; 512 MiB address space each; 30 s per child including admission/open/read |
| Public FST request | 30 s cumulative across metadata, readers and analysis, including nested comparison inputs; cancellation reaps active native work |
| Native work | Validated block envelopes; advertised value-block working set above 256 MiB rejected before decode; hard process limit also covers hierarchy/decompression |
| Private temporary files | 64 MiB per file; selected event spools share a 64 MiB batch/session cap; directory removed on cleanup |
| Metadata | 32,768 declarations and 32 MiB owned-object index accounting, whichever comes first; depth 256, name 4,096 bytes |
| Signal / activity | 65,536 bits per declaration; 4,096 dump activity changes |
| Event page | Default 1,024 records / 256 KiB; hard 4,096 / 1 MiB, including initial/predecessor records |
| Public read result | 65,536 changes / 8 MiB conservative value accounting; aliases also debit output memory |
| Local window | 128 signals, 128 history changes each, existing fixed fallback window limit; no clock/transient inference |

Native buffers can exceed a page's size inside the worker; page limits are not
represented as total-memory bounds. A late narrow window without confirmed
prefix evidence may still scan a large history and hit the deadline. Large metadata can require
a smaller dump rather than just a narrower time window. Nonzero `timezero`,
precision below 1 fs, times beyond signed-64-bit fs, external `.hier`, whole-file
gzip containers, and real/string/special value types are explicitly unsupported.
Truncated reads carry partial coverage; display caps alone preserve the read
coverage and total count. `full` and `compact` project the same validated facts.

Metadata declarations use compact immutable records and share parsed scope
paths/ancestor references. Public rows and range dictionaries are built only
for IPC output. Accounting includes object/string sizes and container backing
allocations; shared enum/small scalar objects are conservatively overcharged.
The accounting budget is independent of the native memory limit. A
bounded in-construction record, container resize, sorting and IPC projection
need additional transient space covered by the 512 MiB process address cap.
Four admitted workers can each consume that cap; it is not a combined RSS
guarantee. Index accounting, worker RSS and sampled parent/child RSS must be
reported separately when comparing capacity or performance.

Each worker sorts declaration paths once and charges the retained pointer
table to the same metadata budget. Scope pages binary-search both the inclusive
cursor and the lexical candidate interval. Parsed ancestry still determines
membership: dots inside escaped scope/signal names cannot establish a subtree.
Direct pages retain descendant visit counting and the original item/byte/scan
stop order, including empty or non-progressing pages. Substring search uses the
same sorted table but still checks every declaration; scope indexing does not
turn substring matching into a prefix query.

Log correlation and recommendation tools also accept FST; their read work runs
in a cancellable worker. Packed-field resolution validates the same semantic
source layout against exact FST declarations. Structural work holds no FST wave
lock. Snapshot X tracing reports `value_unavailable` for an unrecorded sample.

Protocol discovery, sweeps, handshake/TL-UL inspection, transaction reconstruction,
`period` and `verify_window` reuse the shared analysis engines. Transaction
input budgets charge initial states separately from predecessors; missing
recording intervals never carry an old value forward as acceptance evidence.
Discovery receipts accept `fst_scope_v1`. Period measurement preserves integer
fs and refuses unknown or incomplete clock coverage.

The schema adds `fst_runtime`, `fst_backend`, `fst_reading`, `initial_state`,
`value_status`, and `metadata_query_mode="fst_isolated_v1"`; existing enum values
remain. Strict enum clients must accept the new summary and discovery modes,
and comparison's `fst_spool_pages_v1`. FST/FST, FST/VCD and native FST/FSDB
comparisons share the integer-fs page consumer. A future recording gap cannot
invalidate a proven earlier difference; an earlier gap prevents an earliest
difference claim. Dynamic and X-history observations retain typed initial
states and recording gaps through bounded request-local cache slices. They
never substitute a current recovered input for its earlier sampled value.
Structural backend identity is independent of waveform format; wave readers
close before connectivity work. Sub-ps causal scheduling and asynchronous
assignment values retain their existing explicit proof gaps.

## Property Explanations

`explain_property` is a static SVA explanation service. It does not read a waveform,
launch simulation/proof, execute Tcl or call an LLM. It does not determine a runtime
failure's root cause. The optional, versioned `pyslang` frontend runs in the configured
Source Graph interpreter, behind the existing process-wide cold-build admission.
The MCP process imports no optional frontend and holds no waveform lock. Missing or
disabled frontend support produces a structured per-item gap; it never installs a
dependency or switches to an unvalidated parser.

Use one `text` / source target, or `items` with unique caller IDs. Single input uses
the same processing path, with default ID `property-1`. A shared `units` entry names
module-scope `declarations`, optional `default_clock` (event text without `@(...)`)
and `default_disable`. Each item explicitly selects its `unit`; items in different
units never share same-named definitions. Unit declarations may include types,
parameters, macros, sequences, properties and functions. Text includes require a
source/compile request so dependency identity can be checked.

```json
{
  "items": [
    {"id": "all_matches", "unit": "bus", "text": "(req ##[1:3] ready) |=> ack"},
    {"id": "first_only", "unit": "bus", "text": "first_match(req ##[1:3] ready) |=> ack"}
  ],
  "units": [{"id": "bus", "declarations": "logic clk, req, ready, ack;", "default_clock": "posedge clk"}]
}
```

For source input, provide `source_file` and/or an exact `target`, with `compile_log`
or a process-scoped `design_handle`. `target` can be a directive label or a fully
qualified definition/directive path. `instance_path` and `line` narrow selection;
an ambiguous target returns candidates. `arguments` supplies actual argument text
when selecting a parameterized definition. Standalone source files are also accepted,
but have no certified association with an earlier compilation or run. Existing
compile replay and content/include identity checks apply to compiled source; changed
or expired prepared observations require explicit preparation. Current source content
does not by itself establish the bytes used by an earlier simulation.

The frontend binds definitions using synthetic callers where needed, retaining their
definition identity and original source. These callers are analysis scaffolding, not
evidence that an assert was present or enabled. Parameter specialization and active
generate instances come from elaboration. Source/context errors remain explicit.
Slang assertion analysis validates clock flow and multiclock/empty-sequence legality
in addition to compilation binding. Parenthesized clocks stay scoped to their expression.
No result cache is used, so there is no cross-mode or cross-source semantic cache hit.

Each `results[]` entry preserves its ID and carries `identity`, `target`, `mode`,
`directive`, a typed `nodes` graph, `clocks`, local `variables`, `effects`, `references`
and source-linked `gaps`. Sequence endpoints and alternatives remain symbolic:
implications require a consequent for **every** antecedent match, while a sequence
consequent may choose a successful alternative. The projection distinguishes sequence
and property `and/or`, fixed/range delays, consecutive/nonconsecutive/goto repetition,
empty matches, `first_match` (including tied earliest ends), `throughout`, `within`,
`intersect`, conditional properties, strong/weak completion and abort/disable rules.
For `and/or`, the frontend syntax determines the domain even when a sequence is
used as a property consequent. `domain_basis` records this classification.
`and` and `intersect` expose `requires_same_start` and `requires_same_end` explicitly:
both share a start, but only `intersect` requires equal ends. Sequence `and` ends
at the later operand end; property `and` combines independently completing truths.
Multiple clocks retain destination-event alignment and the clock owning each delay.
Sampled functions retain arguments, types, history rules and vector/LSB distinctions.
Default disables retain asynchronous evaluation separately from sampled functions
inside their conditions. Physical waveform pre-edge values are not automatically
assertion scheduling-region samples.

Same-clock concatenation retains explicit empty-operand endpoint rules. An empty
match ends at `start-1`: for positive delay `N`, `empty ##N empty` spans `N-1`
ticks. In particular `empty ##2 empty` is a one-tick match without a required
Boolean sample; `##0` joined to an empty operand has no match. Frontend legality
checks still apply to the enclosing property and any empty antecedent.

`throughout` requires a right-hand sequence match; `within` requires both operand
sequences to match their sampled values as well as satisfying containment. Neither
operator supplies implication-style vacuity when an operand has no match.
For goto and nonconsecutive repetition, a finite occurrence count does not bound
the waiting time between occurrences. A finite run can end with the attempt still
pending; absence of a match is not automatically a reported assertion failure.
Repeated nodes use sequence domain and describe the entire repeated interval.
For a repeated Boolean operand, `base_match` retains the original one-tick truth,
unknown-value and endpoint rules with explicit single-occurrence scope. Node-level
`match` / `endpoints` instead refer to repetition and its zero-count rules. Goto
and nonconsecutive repetition count true occurrences in order from that repetition's
start; earlier true occurrences cannot be skipped to select a later set.

Source-linked summaries retain activation scope and clock ownership before the
operator rules. Repetitions and sampled functions remain semantic entries even
when their frontend node is a `sample`; negation, abort and property-combination
rules are also retained. Sampled history defaults refer to the input argument
expression's declaration initializers and recursive component defaults, separately
from a function's return type. Pure integral defaults up to 4,096 bits and 256
expression nodes are folded by the frontend. Calls, effects, mutable initializer
dependencies and unrepresented forms remain explicitly symbolic; no assumed
type-default literal replaces an unresolved declaration initializer.
Summaries contain at most 32 entries and explicitly mark further omitted rules;
`detail=full` retains the supporting graph. Compact display is not a runtime verdict.
`directive.activation.schedule` separates leading-clock declarations, procedural
encounters and caller-supplied fragment activation. An initial site without an
enclosing runtime repetition has a source-site encounter upper bound of one;
conditional reachability can still yield zero. For/while/repeat/do/foreach/forever
loops retain unknown encounter counts instead of that bound. Counts are per
elaborated site, not a whole-design total, and neither actual attempts nor start
times are inferred from source. Clock advancement does not invent a new visit.
`directive.completion_policy` states the calling-statement default for sequence
properties. At each sequence-as-property boundary, `facts.sequence_property`
records effective explicit/default strength, nonempty completion, finite-bad-prefix
failure and unfinished/window-end rules. Assert/assume property uses weak by
default; other property statements use strong. A definition or bare fragment keeps
that default caller-dependent. Sequence cover and immediate assertions have
separate source roles. These rules follow IEEE 1800-2017 section 16.12.2; an
uncompleted weak sequence without a finite bad prefix is not a failure solely
because simulation ends. A requested waveform cutoff does not prove the actual
simulation end, and enclosing negation/abort/composition can change the whole
property result. No per-run pending/pass/failure state or simulator reporting is
inferred by this static service.
Required sample mismatches eliminate candidates even under weak strength; once
all candidates are eliminated there is a finite bad prefix. A false/X/Z Boolean
result at a single required sample fails at its tick and cannot be repaired by later
values. Occurrence repetitions retain their distinct ability to wait through
false gaps. Summaries put these failure conditions before unfinished-at-end
rules. Whole-property truth maps to check satisfaction/failure only after all
enclosing operators, separately from disabled and pending attempts.
Single-sample implication antecedents are anchored to their supplied start;
false/X/Z completes that implication vacuously instead of waiting for a later
true tick. Same-clock single-sample `|->` operands finish at that start. Bounded
clock/reference unwrapping preserves this rule through named definitions, without
collapsing repetitions or temporal compositions. Sampled-function facts also
specialize the first event without prior history for uniform integral zero/one/X
default sampled values. These are symbolic current-value predicates; history does not
restart at each assertion attempt or arbitrary waveform window.

Local variables belong to attempts and matching branches. A separate definite-flow
pass checks incoming alternatives and zero repetitions; parallel writes that block
flow are retained. Local sequence input/inout ports copy in at entry; whole-variable
output/inout actuals receive values on successful matches. Indexed output aliases
and function output/ref alias effects remain explicit gaps. Recursive definitions become finite reference graphs with guarded
clock progression and argument bindings, without a termination or proof claim.
Straight automatic functions with a single return and argument-only reads can expose
their return expression without evaluation. Stateful calls, match items, actions and
assertion-control tasks retain statically known reads/writes, conditions and phases.
Loops, dynamic calls, unmodeled system calls, unsupported temporal operators and
frontend language limitations produce specific gaps rather than guessed effects.
Simulation-time functions retain their time domain; formal modeling requires separate
tool/configuration evidence.

`mode=auto` resolves an exact `run_anchor` (discovered log, wave or selected run
directory), or the associated compile log, against independent bounded discovery
receipts. It never chooses whichever discovery tool ran last. Unassociated or
ambiguous inputs remain `unknown`, and common temporal analysis continues. Explicit
`simulation`, `formal` and `unknown` modes retain caller provenance. Simulation
assert/assume have the same property truth rules; assume does not constrain ordinary
simulation stimulus. Formal roles are conditional proof goals, enabled constraints
or witness searches. Discovery never supplies proof outcomes, enabled assumptions or
reset semantics. Optional `role_context` identifies a caller statement or script
declaration and its source; neither is promoted to verified execution. `cover property`
and `cover sequence`, procedural `expect`, immediate and concurrent assertions retain
their separate directive/activation forms.

Read `syntax_status`, `semantic_status` and `context_status` independently. Syntax,
missing context, unsupported behavior, frontend failure, identity changes and limits
have separate gap categories and recovery instructions. One bad item does not discard
independent results. `analysis_limited` means work stopped; `output_truncated` means
display was reduced. `detail=summary` omits graph/effect rows, preserving status and
gaps. Summaries prioritize operator rules with node IDs and source excerpts over
repeated value-node boilerplate; alignment explanations name their actual operands.
A byte-limited response also retains `gap_codes` and marks `gaps_truncated` when
detailed diagnostics are shortened. Request an explicit item again for more detail;
there is no implicit current property. AI interpretation of remaining source gaps
must stay labelled as inference.
Summary display keeps all retained frontend errors and at most eight warning
details, preferring the selected target's source location. Full display retains
the frontend's original diagnostic allowance. This display reduction does not
change semantic completeness, preparation gaps or executable plans.
`diagnostics_truncated` marks the frontend or summary diagnostic display cap; errors precede
warnings, and warning-only truncation does not make complete semantic analysis partial.

`include_check_plan=true` optionally lowers bound integral values through the shared
typed Slang expression projector and prepares a static property plan. Sampled
functions retain separate history inputs and declaration defaults; local assertion
variables are not waveform dependencies. Each item returns `check_plan` with a
prepared handle or specific gap codes. Preparation is cancellable, stays off the
event loop and reads no waveform. The full graph is registered before display
trimming, so `detail=summary` can still return its plan handle. Unvalidated user JSON
is never accepted as a trusted plan.

Plans are process scoped: 10-minute idle expiry, 32-entry / 32 MiB store, at most
4 MiB per plan; session reset clears them. They retain the source observation and
validation hooks for later execution. Plan preparation does not evaluate a property
or establish its runtime activation; `runtime_analysis` remains `not_performed`.
Unsupported values, recursive references, unresolved semantics and clocks outside
the supported direct single-bit clock shape return explicit preparation gaps.

`src/property_engine.py` is the internal single-clock matching layer over typed
samples. Sequence matches retain endpoints and branch-private locals; implication
checks every antecedent match, while consequent alternatives are existential.
It handles concatenation, bounded and occurrence repetition, first-match ties,
throughout, intersect/and/within, sampled history and separate asynchronous abort
timelines. Missing evidence stays distinct from observed X/Z.
Property values can be determined without unavailable inputs when a fully
controlling bitwise AND/OR mask fixes every output bit, or a sampled edge
predicate's current LSB excludes that edge for every prior value. Typed widths
and signed extension still apply. These rules retain missing-input gap codes,
never create a historical waveform observation, and do not certify a first-clock
history origin. Partial bitwise determination is opt-in for property evaluation;
other dynamic-evidence callers retain their existing behavior. Unsupported
expressions and analysis limits cannot be suppressed by a controlling mask.
Decisive-prefix probes locate completion before later resets or impossible endpoint
joins can distort failure time; probes share the request's work/deadline/match budgets.
Display trimming does not stop analysis. Default bounds are 200,000 work steps,
16,384 cumulative matches and 256 evidence rows; cutoff and actual simulation end
remain separate. This layer performs no waveform I/O or source parsing.

Property target discovery walks declaration symbols and assertion-containing
procedure statements. It skips unrelated initializer/value trees and subroutine
statement bodies; selected property expressions are projected separately under
the same visit/depth/time bounds. Procedural assertion-control calls remain
explicit source effects. A large unrelated driver does not consume the selected
property's graph budget merely because it shares the source file.

`verify_window(mode="property")` executes a registered `property_plan` handle
against VCD/FSDB/FST. Supply `log_path` to select the first failure of the exact source
directive instance, a normalized `failure_event`, or a `failure_time_ps` TimeSpec.
No clock or window arithmetic is required from the caller. The tool uses logged
attempt start times when available, otherwise derives candidate starts from the
temporal horizon and actual clock events. Simple bounded responses use their
deadline to select the originating attempt. Unbounded history or a candidate cap
retains partial coverage. `property_scope` relocates the selected source scope;
`property_bindings` maps exact source names or unique leaves to dump paths or fixed
selections. Shape and declaration identity are validated, with no suffix guessing.

Source observations are checked before and after execution, outside the waveform
lock. Waveform work uses the existing cancellable worker and locks; wave/log
identities are rechecked before publication. Clock-window discovery is bounded,
adaptive and uses raw-fs events rather than a guessed period. Public-ps collisions
at the failure/start anchor are rejected as ambiguous. Data sampling is strictly
before the physical edge. Asynchronous controls read separate transition timelines,
including between-edge pulses. `$sampled` in an asynchronous condition uses the
state before the current physical timestamp group; in a sequence or consequent it
uses the assertion clock sample. Control timestamps are witnessed dump events,
not a reconstruction of simulator delta-region callbacks.
Unresolved same-time control ordering stays unknown. First-clock defaults require
history-origin evidence, never a new arbitrary window boundary. Multiple clocks,
recursive definitions and unmodeled effects remain
explicit gaps. Source identity does not certify the bytes used by an earlier run.

FST property reads share the existing isolated reader, per-path lock, request
ownership and cumulative 30-second FST deadline (in addition to the property
deadline). Initial observations strictly before a window can seed asynchronous
controls without becoming fabricated predecessor transitions. A logged attempt
with no sampled-history dependency first reads its own window, allowing complete
evidence after an earlier recording gap. Gaps in required evidence and incomplete
reads prevent a complete check; `gap_codes` retain reader reasons. Native FST failures retain the common
`fst_*` error codes, and request exit reaps workers and private event spools.

Property mode returns its own result contract: `failure_reproduced`,
`not_reproduced`, `insufficient_evidence` or `unsupported`, with execution/coverage,
candidate selection, per-attempt state, obligations, sampled values and source node
IDs. It does not redefine legacy `holds`/`vacuous`. Non-reproduction is local to the
supplied failure; a positive failure under partial coverage remains a local fact.
Pending attempts, missing evidence and limits never become a global pass.
Attempt endpoints retain `end_time_fs` alongside ceiling-rounded `end_time_ps`.
Per-attempt `sampled_values` retains actual bound input vectors separately from
evaluated expression results, with dump paths, physical sample times and ordered
bits for partial or reordered bindings. X/Z remains literal four-state data;
unavailable history or local values have specific gap codes rather than implying
an absent dump signal. These observations are bounded direct reads, not a complete
dependency inventory (cached history and asynchronous timelines are separate).
At most 256 observations / 64 KiB of estimated observation data are retained per
attempt, without numeric expansion of wide vectors. Prefix probes add no display
observations. Output trimming drops display rows before dropping whole attempts;
display caps do not alter matching or coverage. Pending attempts at the report
cutoff retain `insufficient_evidence` and can suggest a bounded
`get_signals_by_cycle` query for the same attempt beyond that cutoff. The suggestion
does not run automatically or certify a later failure at the supplied report time.
Property mode also returns `evidence_context` (version 1, a unique `check_id`).
Retained `sampled_values` and each attempt's actual pre-edge `clock_sample` have
typed `s_*` evidence IDs; expression results have `x_*` IDs. The clock sample is
read from the sampled column, never inferred as 1 from a posedge witness. Existing
raw fields remain available. IDs address retained display rows, not a complete
dependency inventory; trimming can remove rows and makes their IDs unavailable.

Clients can keep the original tool response and call
`src.property_evidence.resolve_property_evidence(result, references)` with
`references={"check_id": ..., "signal_evidence_ids": [...],
"expression_evidence_ids": [...]}` selected by the model. This pure helper
assembles separate `signal_facts` and `expression_facts` from the retained facts,
preserving binary X/Z, ordered selections, raw-fs time and sampling phase. It
performs no I/O, uses no live store, checks cancellation and rejects foreign,
changed, removed, duplicate or mistyped references (at most 256 references / 4,096
catalog records). Hashes detect receipt mutation, not authenticate user-supplied
replacement results: the client must supply its own original tool response.
Client integration is required to assemble final facts without model rewriting;
ordinary MCP clients continue to receive additive IDs alongside the existing
values. A resolved fact remains evidence from that query, not a new observation
of a potentially changed waveform file.
`failure_event.start_time_ps` retains the normalized logged start when supplied.
Clients can call `src.property_verdict.resolve_property_verdict` with their original
response and expected `wave_path`, `failure_event`, optional `plan_fingerprint` and
`check_id`. It checks exact wave, directive instance, report/start and requested
identities before mapping a known tool status to a local verdict. Legacy receipts
can recover a logged start only from their single logged-start attempt, never an
arbitrary read-window boundary. A witnessed positive may remain authoritative with
partial candidate coverage. Non-reproduction requires complete execution, coverage
and candidate checks; pending/unknown results (including unresolved history),
unsupported and failed execution remain unresolved. A complete disabled or vacuous
attempt is only evidence about the supplied failure event, never a whole-run pass
or root cause.

`assemble_property_answer` in the same module uses this decision for final verdict
and canonical anchors, optionally resolving evidence references as well. It retains
the original `model_proposal`, records changed fields in `proposal_conflicts`, and
separates the unverified `model_interpretation` from a program-generated decision
description. If the tool is unresolved, a retained proposal is explicitly labelled
`verdict_source="model_fallback"`; it is not promoted to a tool conclusion. Callers
must retain and supply original trusted tool receipts, check conflicting receipts
before selecting one, and integrate this pure helper in their client. The helper
performs no I/O or new simulation and does not classify free-form prose or certify
the explanation merely because a verdict and physical facts are correct.
Semantic failure and simulator reporting time are retained separately. Complete
disjoint endpoint inventories can establish an impossible intersection at its
start. If a log identifies the same attempt, a false prefix at or before its
reported failure reproduces that attempt's violation; `report_timing_exact` and
`report_alignment` record whether timestamps also coincide. An earlier failed
candidate without logged start correlation remains insufficient evidence.

Property defaults: 16,384 read edges (max 65,536), 1,024 candidate attempts
(max 4,096), 200,000 work steps (max 2,000,000), 20 seconds total (max 120),
128 KiB output (4..512 KiB). Existing selection/read limits also apply.
Asynchronous control inventory additionally caps 65,536 records / 16 MiB logical
bytes. Output trimming is independent of analysis, and cancellation does not
advance to another evaluator. Property-only options are rejected in template modes.

Limits: 16 items and 16 shared units; 64 Ki characters per text/declaration and 256 KiB
total text; 16 KiB argument text per item; 256 captured source inputs / 32 MiB; default
1,024 temporal nodes per item (maximum 4,096), depth 64 (maximum 128), and bounded AST
visits. The total request deadline defaults to 20 seconds (maximum 120), including
admission, preparation and validation. Workers have a monitored 512 MiB RSS limit
and an 8 MiB transport cap. Output defaults to 512 KiB, configurable from 4 KiB to
2 MiB; an envelope too small for every per-item receipt is explicitly rejected.
Cancellation terminates the worker process group and never advances fallback. A
timeout preserves already completed items and marks unfinished IDs; output trimming
does not convert partial semantic analysis into a complete result.

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

Run `.venv/bin/python scripts/check_waveform_readback.py --help` for a local
stdio readback check using a neutral VCD; it needs no EDA tool. Confirm your
client's tool access separately through its configured TraceWeave connection.

## Handle-based Hierarchy Access

`build_tb_hierarchy` generates a full hierarchy result server-side (project
metadata, grouped file list, complete `component_tree`, `class_hierarchy`,
raw `compile_result`, compact per-file scan results, and a private immutable
content snapshot) but returns only a **slim payload** to the LLM: project,
stats, depth-2 `tree_skeleton`, interfaces, `ambiguous_basenames`, numeric
`build_metrics`, and a content-addressed `hierarchy_handle`. Compile transcripts
are consumed as streams. Without a shared preload, a source body exists only
while its file is being scanned. Concurrent legacy hierarchy/scan consumers may instead retain bounded
source text in the transient compile-source index until their final lease ends.
Cross-file facts plus digest/stat/marker records survive; `source_text` is removed
before the result enters the handle store.
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
change NPI/Source Graph/Static routing.

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

Source Graph IR 1.5 / projector 1.22 records a template-local
`structural_driver_id` independently of every statement's `assignment_id`,
dependencies, guards, location and dynamic expression. The query binds writer
identity to the instance; distinct generate blocks, processes and continuous
assignments stay distinct. Missing procedural identity is an explicit gap and
cannot prove independent or exclusive writers. Older IR artifacts are rejected.
Driver collection admits up to 256 writer identities while retaining up to 4,096
statement matches. Independent bounds remain for 4,096 states, 16,384 inspected
edges, 16,384 assignment inspections and 4,096 frontiers. Evidence saturation
continues structural inspection; work saturation stops with an explicit gap.
Dynamic/dependency queries consume the statement view, never one representative
per process. `traversal.structural_driver_count` counts known identities (a lower
bound when identity is missing), `inspected_assignment_count` counts work, and
`retained_evidence_count` counts retained statement mappings. Their individual
limits and truncation flags must be read separately.

The driver query records the relation between each visited bit and the requested
bit. Provenance carries `resolution=found|proved_no_driver|unknown`, exact
`target_bits`, relevant `reason_codes`, and `driver_set_complete`. A positive
fact and a complete enumeration are different claims. Constants and local
assignments can be found without a `source_path`. Scoped exclusions remain
scoped; exclusions on shared definition statements are rebound to every instance.
Global context gaps remain global. Budget stops conservatively affect the
uncompleted query; display-only saturation adds `query_evidence_limit` while
structural inspection continues. Sparse unresolved bits are never widened to a
continuous range. If a driver query is inconclusive and Static also returns an
unsupported result without facts, the router returns that one Source Graph
artifact's partial result with every backend attempt. Supported Static facts
still replace the entire payload. Bootstrap and NPI acceptance are unchanged.
Positive facts from the final artifact also survive a stalled or capped frontier
expansion; its coverage gaps and specific expansion blocker remain visible.

Source Graph's canonical `driver_chain` and `bit_provenance` group only identical
metadata belonging to the same known writer. `statement_semantics` retains the
common guard, dependencies, selection, traversal and evidence strength. Every
distinct line/column remains in ordered `statement_evidence`; `evidence_index`
allows `src.driver_evidence.expand_driver_evidence` to reconstruct the original
row order. `statement_count=0` explicitly marks a structural row with no retained
statement location. The legacy `source_line`/`source_column` are the first
evidence location, not a representative replacing the rest. Different guards,
dependencies, confidence or bit mappings stay separate. This is the default
lossless representation, not a summary option; internal dynamic queries still
consume all retained statements. NPI rows are not grouped this way.
`traversal.returned_fact_count` counts returned chain groups;
`retained_evidence_count` counts query statement mappings before provenance
segments are split. `query_instance_path` identifies the requested instance;
`resolved_instance_path` keeps the historical first driver's instance.

Explicit driver `compile_context` recovery is orchestrated by the server; the
context resolver stays read-only. Only `hierarchy_unavailable` can trigger one
bounded recovery. A usable KDB candidate now selects independent preparation,
historical identity verification and atomic context/binding publication. No full
hierarchy, structural scan, Slang session or native load runs during that
preparation; the following query uses the normal bound local/explicit-LSF route.
Existing full handles stay compatible. A source-policy or unavailable candidate
still uses a lexical hierarchy rebuild until semantic planning is migrated.
Ordinary NPI query/load failure after selecting a candidate uses bound fallback,
never another full recovery build. Changed or ambiguous identities return a prerequisite
block with an executable build call. Primary log identity, supplementary log
order, simulator, compile-selected top, supplied snapshots and historical source
fingerprints are validated again on the private candidate before publication.
The old source fingerprint is the stat/raw-digest identity of the old
preprocessor's observed files, not the new capture of logs, filelists and sources.
The historical compile-log token contains stat identity only. Equal-size
rewrites with restored mtime can also retain ctime within a filesystem clock
tick; equal legacy tokens therefore do not establish historical log bytes.
New independent prepare recovery uses the captured raw-content identity.
`legacy_context_observations` reproduces that read set with dependency-only
preprocessing, preserving the old exact-log include policy without materializing
HDL or enumerating definitions. Additional inputs, aliases and positive/negative
lookup observations are retained in the new context for subsequent validation.
Matching an incomplete preprocessor observation does not upgrade its coverage;
gaps remain explicit. Its composite identity is recovered through the returned
legacy `compile_context`, not the plain `prepare_design_context` recovery token.
Recovery without a historical source token establishes current source context,
not the sources of a past simulation. A top that cannot be selected unambiguously
from the compile evidence is a blocker; recent session state never fills it in.

`HierarchyRecoveryRuntime` shares only exact live requests, with one admitted
build, at most four live flights and an independent per-flight deadline. The
default hard bounds are 30 seconds including admission, 4,096 input files and
128 MiB of unique raw input bytes per capture (also applied to the merged
historical/current observation set). Logical dependency expansion is separately
bounded, including repeated includes. Source compatibility retains its bounded
lexical reader. Identity rehashes add I/O; preparation metrics expose captured
bytes, actual read bytes, historical verification and discovery cost, and the
receipt's wall time includes all preparation phases. This is not an acceleration
claim; source compatibility currently pays both preparation and rebuilding.
Nonzero
`TRACEWEAVE_HIERARCHY_TIMEOUT` and `TRACEWEAVE_HIERARCHY_MAX_SOURCE_BYTES` can make
these stricter. Bounded reads prevent a growing include from bypassing preflight.
One waiter cancellation preserves other waiters; final-waiter cancellation arms
the existing cooperative worker cancellation. No result is published on failure
or cancellation before completion. Preparation workers hold no waveform lock and
invoke no NPI overlay, LSF job or Slang session. Only the event loop registers a
validated context plus binding; generation changes prevent late publication
after reset. Exact recovery aliases live in the bounded context registry and
expire with their entries. A later call validates the same binding without
repeating discovery; changed source, KDB or execution policy requires explicit
new preparation. Result caches and workflow/scan state remain intact.

`build_tb_hierarchy.handle_lifetime="process"` is explicit. Driver results and
prerequisite blockers carry an optional `hierarchy_recovery` receipt with status,
budgets, coalescing, handle and validated context. `structural_scan_performed`
remains false: recovering hierarchy does not satisfy the independent parallel
structural-scan workflow. Ordinary matching no-handle hierarchy reuse is retained.

Driver requests can bind a Verilator `TOP.<compiled_top>` dump root to the
current design root through `wave_design_binding`. The binding requires exactly
one compiled top, an exact dump declaration, an unchanged wave identity, and a
matching optional top hint. A real compiled `TOP` or a multi-top context is
never stripped. `wave_design_binding` retains both names and explicitly does
not prove historical source/wave identity. Root binding does not resolve
generate scopes, arrays or fields; semantic query gaps remain separate.

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

The standalone frontend enables IEEE 1800-2009 assertion extensions with
`-assert svaext`, because `-sv` alone selects the 2005 syntax and rejects
assertions such as `s_eventually`. The recorded source files and simulator
macros stay intact. Live commands and `build.sh` share these frontend flags;
the versioned cache key includes them, so an older recipe is rebuilt explicitly
instead of being reused as an equivalent KDB.

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
    ├── vericom -sv -assert svaext -kdb [-ntb_opts uvm] [+define+...] [+incdir+...]
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

Optional, local-only handler instrumentation records bounded numeric and
fixed-label aggregates for offline tool-use, cache and semantic-session
summaries. It does not retain argument values or source text.

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
- VCD cold loads and runtime-log parsing additionally record numeric
  `vcd_*` / `log_*` input costs: `parse_calls`, `parse_completed`,
  `parse_wall_ms`, `parse_cpu_ms`, `read_ms`, `input_bytes`, and
  `parse_rss_start_kib` / `parse_rss_end_kib`. Attempts include failed or
  cancelled loads; completion requires the parser's normal successful return
  (including VCD identity validation). `input_bytes` sums observed file sizes
  for completed reads, not physical disk I/O or decoded-object size. Repeated
  log reads count again. Reusing an already parsed VCD records no new parse.
  RSS values are process samples at entry/exit, not input-attributed peaks;
  CPU is process CPU. Input timing is nested within discovery/sweep timing,
  so these durations must not be added together. Both telemetry allowlists
  reject paths, arbitrary labels and nonnumeric input-cost values.
  VCD scope/window limits reduce work after the first full-file parse;
  `get_waveform_summary` also triggers that initial parse.
- `scripts/benchmark_input_costs.py` is a serial VCD/log backend microbenchmark.
  Supply immutable `--wave` and optional `--next-wave`, `--log`, `--next-log`,
  with a new `--output` directory and the same `--source-root`/input conditions
  for each comparison. It retains input/source hashes, actual facts, wall/CPU,
  RSS samples and process high-water RSS. Input hashing primes OS caches;
  cold means a new parser/process. Same-wave repeats reuse its parser; the
  new wave loads while the initial parser remains resident. Log repeats are
  repeated reads. Handler, scheduler, transport and log snapshot costs are
  excluded. This is deterministic tool measurement, not an Agent A/B run.
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

`src/divergence_context.py` freezes each side's exact hierarchy, ordered logs,
source snapshot and top. Ready driver relays carry those identities into the
normal public route instead of changing the mutable current session.

`src/dynamic_evidence.py` defines a bounded typed expression contract.
`src/npi_dynamic.py` obtains cell/port, mux polarity and edge facts from NPI and
expands generated nets to declared signals/constants. `src/slang_dynamic.py`
projects typed AST guards, priority and timing into Connectivity IR 1.5;
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
Slang evidence. The LSF worker protocol is 4.0; the dynamic step fact protocol
remains 2.0.

Slang preserves pure SV operators, casts, packed dimensions, fixed-array leaf
types, packed fields and net declaration initializers. It samples selected
array elements only after exact dump metadata binding; array writes never
reconstruct missing storage. `src/dynamic_selection.py` preserves the entire
arithmetic computation before projecting result bits, including carries.
Only an exact reference can establish passthrough; equal numeric values cannot.
Index differences and changing selected dependencies remain explicit.
Source Graph build/worker is 3.4, projector schema 1.22, query mapping 1.8 and
adapter 3.15, so prior expression/cache capabilities cannot silently carry over.

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

## Prepared traces and packed layouts

Prepared X snapshot/history and both `trace_divergence` sides use the captured
compile context directly through `prepared_source_connectivity` and
`prepared_source_trace`. They do not materialize a lexical hierarchy. Semantic
candidate unions are bounded to 64 targets/instances and 4,096 member visits;
frontier searches use actual semantic instance roots and at most two descendant
levels. Each artifact change discards the graph and restarts at its original
root. Divergence sides retain independent identities, artifacts and query
ledgers; an unchanged side can retain its valid artifact across the other side's
restart. Source preparation and queries stay outside waveform locks.

Snapshot uses the original prepared-request start and configured Source Graph
timeout across preparation, expansion, waveform work and final validation.
History retains its own graph budget: its deadline yields a partial graph and
frontier, while explicit backend timeouts, cancellation and identity changes
cannot publish an old graph. A declared top input reached through positive
bindings is an incomplete investigation boundary, not a complete negative or
an exclusive root cause. Dynamic receipts count the actual driver query statuses;
port-alias positive evidence is also retained in the structure, even when the
underlying bounded driver query is inconclusive.

Prepared `resolve_packed_fields` requests a separate `packed_layout` semantic
purpose, which participates in artifact identity and exact capability/scope
reuse. Only a real projected declaration and typed member layout can produce
field selections. Hierarchy and connectivity artifacts cannot stand in for
layout capability. Dump declaration ranges are checked under the waveform lock;
missing type/target evidence or a range mismatch retains `mapping_required`.
Legacy explicit hierarchy routes and trusted NPI priority remain available.

## Bounded X History

`trace_x_source` preserves the same-time `snapshot` mode and adds opt-in `history`.
History requires `history_start_ps` and either an independent `design_handle`
or the legacy compile/hierarchy context. A prepared handle needs no full
hierarchy; see [Prepared traces and packed layouts](#prepared-traces-and-packed-layouts).
For the legacy route, optional `compile_context` binds an exact hierarchy
handle/top as in divergence tracing, and its compile log must agree with the
outer request. The caller still
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

The default workflow includes structural scanning. Set
`TRACEWEAVE_STRUCTURAL_SCAN_IN_WORKFLOW=0` in the MCP server's startup environment
and restart or reconnect to omit automatic scan and continuation guidance.
Explicit tool calls remain available, and compatible cached evidence is retained.
Without a cached scan, the diagnostic section reports `available=false`,
`summary.status="skipped_by_policy"` and `summary.coverage_status="not_run"`;
skipping never establishes a pass. Other workflow requirements are unchanged.

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

All modes run or reuse the five lexical rules independently. Semantic routing is:

| Mode | Existing/cached KDB through NPI | Missing semantic categories |
| --- | --- | --- |
| `fast` | No discovery, load, license request or LSF submission | Not run; no Slang work |
| `auto` (default) | Discover, validate, load and inspect within budget | Compatible completed source-memory results or scoped query IR only; no cold Slang build |
| `deep` | Same native priority, including when source results are cached | Reuse first; permit bounded isolated Slang construction |

Scanning never invokes `build_kdb`, `vericom`, `elabcom` or a simulator. NPI may
request a commercial license, following `TRACEWEAVE_NPI_EXECUTION` and the
namespaced LSF settings below. Slang remains license-free. Missing Slang,
disabled Source Graph or incomplete source capture does not prevent an existing
bound KDB's input-port observations. Missing KDBs use the source route by mode.
If compile metadata has no unique top, supply an explicit `top_hint`; NPI verifies
it exactly rather than selecting the first candidate. The hint also selects the
source elaboration top. Other compile tops are outside that selected domain.

For example, request native input-port categories explicitly:

```json
{
  "compile_log": "/path/to/build.log",
  "top_hint": "tb",
  "semantic_scope": "tb.dut",
  "semantic_categories": ["constant_connection", "open_input"],
  "analysis_mode": "auto"
}
```

The category list replaces the default selection. Defaults remain
`constant_connection`, `open_input`, `constant_comparison`; NPI does not implement
comparison, propagated-constant or constant-control checks. Source supplementation
retains those Slang capabilities. An `auto` cache miss cannot cover a comparison.
Scope limits basic source fact extraction, not necessarily frontend elaboration;
these modes do not require a full-design ConnectivityIR build.

### Bit-level driver checks

Both NPI and Slang support the explicitly requested categories
`multiple_driver` and `undriven_signal`; defaults are unchanged:

```json
{
  "compile_log": "/path/to/build.log",
  "top_hint": "tb",
  "semantic_scope": "tb.dut.u_block",
  "semantic_categories": ["multiple_driver", "undriven_signal"],
  "analysis_mode": "deep"
}
```

Normal mode routing applies: NPI has priority, `auto` only reuses compatible
source results, and `deep` permits a bounded Slang build. No driver-tool subcall
or waveform read is required. Facts use `evidence_domain="elaborated_static_drivers"`,
an exact `signal_path` and `target_bits` in declared coordinates, preserving
ascending and descending ranges. `multiple_driver` requires at least two known
independent sources. Repeated writes to the same bit in one process count once;
different output bits wired onto one net retain distinct identities. The
`driver_count` is a lower bound when `driver_inventory_complete=false`.
`undriven_signal` requires a complete empty inventory for the reported consumed
or externally observable bits; unused wires and unknown driver sets are not
reported as undriven. Driver evidence is capped at 32 rows per fact, with
`driver_evidence_truncated` separate from the count. Both categories report
`runtime_conflict_checked=false`: resolved nets, conditional enables and runtime
force/DPI/class writes require further investigation before assigning a defect.

NPI enumerates integral declarations through Language and queries each bit's
direct Netlist drivers and loads in the existing native owner.
Verified vectors reuse one declaration handle and resolve individual bits through
the native index API. Names, bounds and widths must agree with Language metadata;
unsupported mappings use exact-name queries. The vector handle is cleared before
leaving the declaration, including cancellation and bounded work stops. Consumer aliases
are checked against the same net's original load inventory, never recursive
fan-in. Undriven proofs require a healthy, non-black-box module boundary and an
internal wire/tri/uwire with known bounds and observed loads. Input/inout/ref
boundaries, variable state, unknown resolution and unsupported declarations remain
excluded or gaps. Endpoint inventories are capped at 256 copied entries;
unverified driver/process mappings cannot establish absence. Native facts retain
the KDB's unverified source association. Signal/bit positions participate in
bounded continuation, including pages with no findings.

Slang inventories static assignments, procedural processes and port bindings
over all elaborated tops, even when output is scoped: siblings can write
hierarchical targets. Static net-port collapse shares per-bit writer sets.
Initial state, dynamic write targets, interface runtime state and unmodeled
subroutine side effects block affected absence proofs. Unresolved global writes,
blocking frontend diagnostics or an interrupted inventory prevent negative
proofs; known multiple-driver positives may remain. Unsupported shapes and
constructs stay explicit gaps. The inventory shares the instance/AST/fact and
isolated time/RSS limits, and uses `semantic_max_propagation_bits` and
`semantic_max_propagation_steps` for its signal-bit and work bounds. A scoped
request therefore does not guarantee a small frontend or a completed inventory.

### Native port facts and coverage

`npi_structural_scan.py` walks actual elaborated instances and active generate
scopes through bounded Language `iterate`/`scan`, inside the existing exclusive
native owner. It uses `Port`, exact direction, `HighConn` / `LowConn`, and native
binary values. Verified forms include literal and parameter constants, folding
of constant-only operations, X/Z, and constant regions of equal-width concatenations.
Interface/hierarchical reference wrappers resolve through their native `Actual`
relationship before type/width inspection. Single-dimension packed bit/part
selections preserve ascending/descending ranges, signed bounds and X/Z; mixed
concatenations retain only their constant regions. Packed dimensions come from
the native typespec. For SDK bit objects without an `Index` relationship, only
a canonical decimal suffix of the exact native parent identity is accepted;
the index must lie within the verified packed range. Symbolic package/genvar bit
selections can instead use an exact native bit-identity comparison over at most
256 declared bits. No match, ambiguous matches and missing SDK support remain
gaps. Packed structure and enum state remain dynamic; enum bit selections use
the underlying integral type and packed structures have a flat integral range.
Parameter-array elements require a proved constant, in-range index before using
native values: the SDK can materialize one element even for a dynamic selection.
Packed-array part selections account for element width rather
than assuming one bit per index, and verified bounds can recover missing Size
metadata. Sized expression casts and concatenations can
derive widths from verified operands when the SDK returns Size=-1. Truncation
and unambiguous signed/zero extension preserve constant regions; an unsigned
one-bit nonzero/X/Z literal whose unbased-unsized origin is unavailable retains
a contextual-conversion gap. Variable indexes, out-of-range selections and
unresolved or unsupported multidimensional types remain gaps. Procedural
`For`/`Begin`/`Fork` scopes are excluded from instance-port traversal; active
`GenScope` instances are still visited. The native capability receipt is
`npi_structural_v8`. Known index/range values with a leading zero have the same
nonnegative interpretation with either signedness; high-bit values still require
verified signedness. Four-state transfer functions model bitwise operations,
logical not/and/or, reductions, conditional merging and bounded replication.
They preserve proven constant regions even with dynamic operands; internal
dynamic unknowns are distinct from constant RTL X/Z. Common operand signedness
is applied before bitwise/conditional width alignment; equal-width operands do
not require irrelevant signedness metadata. A one-element packed array of packed
structures supports a one-operand assignment pattern only with verified formal
range and width; X/Z additionally requires identical native element types.
Unpacked or multi-element patterns without adequate metadata remain gaps.
Unmodeled operations
remain gaps; state initial values are never used as a substitute for modeling.
`constant_regions.lsb_offset` counts from the port's least significant position;
each region's `bits` is MSB first, matching Slang. Unverified width conversions,
unmodeled selections/expressions, unknown directions, unresolved array/scope forms
and missing metadata create explicit gaps. Initial/reset register values never
become permanent port ties. Native `constant_connection` covers input bindings;
source continuous assignments are a separate evidence domain.

An absent HighConn alone never proves an open input. Positive port-instance
location and the exact LowConn identify `connection_status="explicit_empty"`.
This is an empty parent-actual observation: the installed SDK does not reliably
recover default-port behavior, so `npi_empty_actual_default_semantics_unverified`
keeps coverage partial. Missing/omitted/default connections without that positive
syntax evidence remain unresolved. Top inputs are boundaries; output/inout/ref
ports are excluded. Native locations retain `source_association="unverified"`;
compile metadata does not establish that current source bytes produced the KDB.

`semantic.category_coverage` carries requested `scope`, actual `checked_scope`,
status, backend, fact count, gaps and result-group reference. `result_groups`
preserve the selected top, evidence domain and identity; native groups also retain
the bounded KDB/load binding, load quality and source-association limitation.
Complete means every requested category was checked in its declared domain, not
that the design is defect-free. An unstarted category has no checked scope.
Native result groups also expose `gap_summary`: occurrences, affected-port
counts (deduplicated per port/reason), and at most two examples per reason,
16 overall. Examples contain bounded instance/port and native type/operator/
width observations, never runtime values. Global stops do not count as affected
ports. Output trimming can omit examples or the summary and marks that omission;
it does not change category coverage. These diagnostics are not telemetry.

One category selects one inventory: complete NPI has priority; a complete source
pass can replace partial NPI for that category; two partial inventories are not
added together. Replaced groups keep their receipts and empty `selected_categories`.
Every returned fact has a unique `backend` and `result_group`. Combined results
report `backend="mixed"`; they never claim one shared native/source artifact.
Cache hits and zero facts do not establish completion. Lexical risk counts and
semantic fact counts stay separate, including in diagnostic/recommendation summaries.
Display trimming preserves counts, observed group identities and category coverage and marks
`output_truncated`; work stops remain partial. The combined displayed list is also
capped by `semantic_max_facts`, independently of each backend's extraction bound.
At the terminal display budget, optional limitations prose and an unreferenced,
unobserved `not_run` group can be omitted; `omitted_result_groups` identifies the
latter and backend-attempt reasons remain. Referenced/native artifact receipts
are retained.

### Native scan lifecycle and limits

`structural_native_runtime.py` uses one semantic deadline (default 15s, maximum
120s) for metadata/KDB discovery, validation, load, LSF queue/transport, traversal,
source supplementation and final validation. The lexical pass has its independent
lifecycle. Cancellation, total timeout, changed KDB/metadata/native generation or
execution policy terminate the semantic request without starting another backend.
`termination_reason` reports fixed terminal labels; late/cross-identity facts are
discarded. An ordinary unavailable/unsupported backend can fall back by mode.

Local NPI remains synchronous on the dispatch thread and serializes through the
existing Language owner. No new native thread or waveform read/lock is introduced.
The SDK cannot be interrupted inside a synchronous call; checkpoints reject late
completion and cleanup still runs. A blocked local call can delay other event-loop
work. Iterators are released and Language handles cleared on every owner exit.
At most one successfully validated local backend is retained for loaded-design
reuse; no native handles or full native scan-result/disk cache are retained.
A failed reused backend is discarded so later requests can try again.

`semantic_max_instances` bounds native visited scopes (including generate scopes);
`semantic_max_facts` bounds each native page (and each source extraction). Native work additionally caps explicit
calls at 100,000, combined ports/driver bits at 100,000, hierarchy depth at 128, expression depth at 64,
operands at 256, widths at 4,096, handles at 16,384 per batch, and estimated page
fact storage at 8 MiB. The scan copies paths/positions and expands siblings in
batches of at most 64, falling back to 32 once queued paths use a quarter of the
frontier byte budget, and flushing earlier at the byte/frame limits. This
reduces repeated parent-prefix replay on wide generate hierarchies without
retaining native objects across safe points or changing the per-call work caps.
After roughly 4,096 acquired handles, it releases all Language
handles at a safe point with no active iterator or borrowed scope frame, under
the same exclusive owner and loaded KDB. Other hierarchy queries retain their
existing per-query handle cap. Native handle metrics distinguish cumulative
acquisitions, peak per batch and batch count. One final handle may be observed
while detecting a batch cap; a bounded retry requires actual release/progress.
Expression depth/operand stops are port gaps and do not block unrelated ports.
Work metrics identify
native calls/ports/scopes separately from Slang AST visits; driver checks add
`native_driver_bit_count`. `semantic_max_rss_mib`
bounds the isolated Slang worker, not local NPI; process RSS can grow with a loaded KDB.
No hard native RSS isolation is claimed.


For opt-in diagnostics, `scripts/profile_structural_scan.py --help` describes
a separate stdio scan process. `--profile` adds fixed-name phase timings and
numeric/hash reports without argument or source text. Timings include nested
phases and must not be summed across parent/child rows. Normal MCP servers
retain their existing behavior.

### Continuing a bounded native scan

A work-limited scan that made progress returns `semantic.continuation` and a
ready-to-use `suggested_next_action`. Repeat `scan_structural_risks` with
`semantic_continuation` and the same compile log, selected top, semantic scope
and categories. Per-call work budgets may change. Repeating the original call
without the token starts a new inventory; the server does not silently select a
module from a test name. For a known target, pass its exact `semantic_scope`
directly, independently of unfinished global coverage.

Tokens are process-local, expire after ten idle minutes and are consumed only
after a successfully validated response. Restart, expired/consumed tokens or
changed metadata/KDB/policy/domain terminate continuation without source fallback.
Cancellation preserves the old token for an explicit retry. The event-loop-owned
store caps 16 entries / 16 MiB, with at most 1 MiB / 8,192 pending frames per
inventory. It retains only copied paths, port/signal/bit/child positions and prefix
fingerprints, cumulative counts
and bounded gap diagnostics, never native objects or source bodies. No-progress
or irreducible stops report `scan_progress.status="blocked"` without a token.

Reopening an iterator rechecks its skipped prefix fingerprint; reordered,
shortened or changed prefixes terminate the query instead of silently skipping
bindings. Native `result_groups[].scan_progress` distinguishes page work from the continued
inventory. `total_facts`, category counts and diagnostics are cumulative; native
`facts` contain only this page's rows. Earlier rows are not repeated or retained
as a full result cache, so keep earlier responses or query a narrower scope.
`output_truncated` remains true when the current response omits prior-page facts.
Visited-scope/call/port/byte metrics are per call; `completed_instances` and
`pending_scopes` describe the inventory frontier. Draining that frontier removes
transient work-limit gaps but preserves semantic gaps; `scan_progress.status`
does not replace per-category coverage. All categories must actually be covered
before a complete semantic result is possible.

Each continuation has a fresh bounded semantic deadline; no automatic unbounded
background sweep occurs. LSF workers accept the same validated copied frontier,
execute only native work, and return page deltas checked against cumulative counts.
Licensed loading remains remote in LSF mode; separate jobs may reload the KDB.

LSF uses the distinct version-5 `structural_scan` worker operation over the existing
private transport. Requests carry metadata, exact KDB/top/scope/categories and
remaining duration; the parent retains its original deadline. The worker executes
only the same NPI core, with no discovery, KDB build, Slang or fallback. Both boundaries
validate request fingerprint, binding, category/bit shapes, counts and work caps.
The parent requires matching load quality and completed scheduler/worker receipts;
identity/protocol mismatches are terminal. `semantic.native_execution` exposes fixed
`execution_mode`, `scheduler_status`, `worker_status` and failure labels only.
Source supplementation remains local. Queue cancellation and bounded kill retain
the existing transport behavior; no local licensed fallback occurs in LSF mode.

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

Environment inheritance depends on how the MCP client is launched and its
environment policy. For a deterministic Claude Code setup, list the tool roots,
license and runtime-library variables required by the server. Missing runtime
libraries can prevent NPI from loading; inspect `backend_status` when a query
falls back.

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
passes that shell environment to the TraceWeave server. For Codex, explicitly
forward required parent-shell variables with `env_vars`, or provide fixed
values under the server's `env` table.

The following Codex configuration is for an EDA environment already established
by the parent shell. It is an alternative to the fixed-value EDA block in the
Codex section above. Adapt this example LSF/EGO variable list to the site's
setup, and do not repeat any name under `env`:

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

For Claude Code, merge the following fixed values into the existing TraceWeave
server's `"env"` object (replace `digital` with the user's queue). Include the
site's required EDA variables when they are not inherited from the parent shell:

```json
{
  "TRACEWEAVE_NPI_EXECUTION": "lsf",
  "TRACEWEAVE_NPI_LSF_QUEUE": "digital"
}
```

JSON values are literal too; do not put `"$LSF_QUEUE"` in this static map.

With this mode enabled, explicit connectivity operations
(`explain_signal_driver`, `find_signal_loads`, `trace_signal_path`,
`trace_x_source`), native structural scans in `auto` / `deep`, and every
`build_kdb` cache miss or forced rebuild submit a bounded `bsub -K` worker.
Exact KDB cache hits, log parsing, waveform reads, lexical structural rules,
source supplementation, KDB detection and Static analysis remain local. Connectivity-worker failure or
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

`get_signals_by_cycle` retains global numbering by default. Explicit
`cycle_index_origin="window"` requires `start_time_ps` and numbers confirmed
local edges from zero. `src/local_cycles.py` discovers edges in bounded time
pages, sharing the existing raw-fs event cursors, polarity rules and column
sampler. It never reads a full clock history to establish a local index.
The cumulative budget is one million records / 64 MiB estimated decoded data
over clock and signal reads, 64 expanding time windows and a 30-second deadline;
native calls remain cancellable at the existing return/checkpoint boundaries.
The first time window is at most 1 microsecond. FST spool and native decompression
costs remain format dependent; the budget does not describe total process RSS.

An earlier X does not block a known local predecessor. The first unknown clock,
recording gap or same-time clock ambiguity ends the contiguous prefix. Data X
remains a sampled value. `local_coverage` reports the stop reason and whether the
entire explicit window was inspected; reaching an output cap does not prove its
total edge count. `clock_edges_complete=false` makes no global count claim.
`reading` reports admitted/read records and estimated decoded bytes. A missing
or budget-limited signal remains unknown/partial. `before` excludes all events
at the edge; `after` uses the stated ps offset, with `time_fs` and
`sample_time_fs` preserving exact physical times. It is not delta-cycle settling.
Integer ps labels can repeat; raw timestamps distinguish returned rows, but an
integer-ps continuation cannot address the middle of such a timestamp group.

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

Discovery suggests `scan_structural_risks`; subsequent design queries accept
compile metadata directly. Explicit source recovery still uses `prepare_design_context`.
The diagnostic snapshot has separate prepared-context and legacy-hierarchy
sections. It copies bounded registry metadata without source hashing, native
loading, pins or LRU updates; all context identities are explicitly unvalidated
observations until an actual query checks them. Multiple matching top/log/policy
contexts remain ambiguous. Recommendations retain their context handle/identity,
origin and partial coverage in cache provenance; expired or differently selected
contexts cannot be presented as current recommendations. Structural scans are
suggested independently of tree availability, and source-only policy suppresses
the optional Xcelium KDB-build hint. Legacy divergence actions keep historical
tokens and use exact recovery; changed/ambiguous identities require context
resolution instead of dropping tokens or demanding a full tree.

`resolve_packed_fields` accepts `design_handle`; a compile-log-only call prepares
an independent context within the same total deadline. Healthy NPI queries use
the shared exclusive Language owner, actual Typespec membership/ranges and
native bit-object identity. The initial native mapping supports small packed
structs (at most 256 flattened bits); it never infers offsets from member order
or sums of widths. Unsupported types use the separate Source Graph fallback;
native budget exhaustion does not advance fallback. Local/explicit LSF share
the copied layout contract and context/KDB validation. NPI source association
remains unverified, and signedness is not inferred by this mapping operation.

The existing Source Graph runtime retains named, including nested, member
support. Independent contexts use a bounded source seed without a full lexical
tree; legacy callers may reuse their full hierarchy. It requires a reusable content-anchored build
key, a current compile snapshot, active elaborated instance specialization,
packed-member type facts, and matching dump/source declaration coordinates.
Only explicitly projected instances can supply layouts; ancestor port-binding
fragments cannot. A scoped hierarchy gap alone is acceptable for an explicitly
projected instance with complete type facts; other semantic gaps and blocking
diagnostics require an explicit mapping. Compile identity is checked again
after worker preparation. Unavailable bounded context, an unresolved generate edge,
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

`explain_signal_driver` alone defaults to the independent
`traceweave.driver.compact.v1` typed-table envelope. Its complete retained
evidence is self-contained; `output_format="full"` preserves the previous shape
and indentation. `output_options_for_tool()` supplies both catalog and runtime
defaults. `ExplainDriverResult` remains the internal/direct-dispatch model.
Use `src.evidence_output.expand_compact_result(text_or_dict)` before reading
its driver arrays. This default wire change requires migration of old parsers.

### Driver response tables

`src/driver_evidence_codec.py` projects the already-validated model once at the
final output boundary. Routing, NPI priority, identity validation/recovery,
dynamic branches, dependency analysis and the canonical model are unchanged.
Successful NPI/Static results can have four empty tables; the encoder never
invents Source Graph evidence for them. Only successful `ExplainDriverResult`
models enter this envelope; errors and prerequisites keep their original shape.

| Field under a driver/provenance row | Table in the same response | Meaning |
|---|---|---|
| `statement_semantics_ref` | `statement_semantics` | Complete identical JSON object, including guards, ordered mappings and dependencies |
| `statement_evidence_ref` | `statement_evidence` | Pair of `locations_ref` and `evidence_indices_ref` |
| pair's `locations_ref` | `locations` | Exact `columns=["source_line","source_column"]`, ordered rows |
| pair's `evidence_indices_ref` | `evidence_indices` | Exact ordered integers belonging to this original array |

Inline and referenced forms are mutually exclusive per field. IDs are strict,
nonnegative, response-local integers, not writer/statement/artifact identities.
The fixed table graph cannot patch arbitrary result paths. Interning compares
complete canonical JSON values and preserves bool/int/float distinctions; it
does not merge facts belonging to different writers. No rows or duplicates are
removed. Resolution, reason codes, bit order, source/target mappings and all
receipts remain inline. Identical 64-row location sequences can share storage
while distinct index sequences still preserve each array's statement order.
Sparse, reversed and repeated indices are literal values, not ranges.

A null in the typed **location table's column cell only** means the public row
lacked `source_column`; column zero remains zero. Explicit null in arbitrary
semantics or an inline row remains present. Unknown/empty rows and
`statement_count=0` survive. Encoding either leaves unsupported location shapes
inline or rejects invalid schema inputs; it never drops their extra fields.
Small/nonprofitable candidates stay inline. The planner includes table entries,
pair/reference wrappers, comma/ID widths and first-table overhead in its cost.

```python
import json
from src.evidence_output import expand_compact_result
from src.driver_evidence import expand_driver_evidence

response = json.loads(text_content.text)
driver = (expand_compact_result(text_content.text)
          if str(response.get("format", "")).startswith("traceweave.")
          else response)  # explicit full, or an original error/prerequisite
if "error" not in driver and driver.get("ok") is not False:
    groups = driver.get("driver_chain") or []
    statements = (expand_driver_evidence(groups)
                  if all("evidence_index" in g and "statement_evidence" in g for g in groups)
                  else groups)  # NPI/Static can already be ordinary, ungrouped rows
```

The decoder dispatches on the independent format version; the original
`traceweave.compact.v1` rules remain unchanged. It rejects unknown versions/tools,
bad ID types/bounds, dangling or inline-plus-ref fields, extra table objects,
illegal columns/row widths, mismatched lengths and referenced statement counts.
Pass raw JSON text to also reject duplicate object keys during parsing; a dict
has already lost that information. Expanded groups own independent mutable
copies. No external table, handle, source file or previous response is consulted.
This validates a self-contained encoding, not authenticity: a structurally valid
manually substituted table is not detectable without a trusted signature.

Encoding defaults to a 16-MiB memo accounting budget (keys plus conservative
candidate/occurrence/created-row charges), 8,192 candidate entries and 8,192
emitted table entries. On exhaustion all remaining evidence stays inline. These
are optimization limits, not a constant total-process-memory promise: input,
projection and final text scale with evidence size. `DecodeLimits` defaults to
64 MiB of input/expanded JSON bytes, 2,000,000 value/container nodes, 200,000 total
statement rows and depth 128. Preflight checks precede output allocation and
allocation is counted again while copying. A limit raises
`DriverCodecLimitError`, never a successful empty/partial decode. Trusted offline
callers may supply explicit limits; they are not client token budgets.

The cancellable worker checks groups, key chunks and location/table rows and
holds no wave lock. It performs zero additional frontend/backend/wave reads.
One Pydantic export or Python JSON primitive/final serialization cannot be
preempted mid-call; cancellation is checked on return. There is no pagination,
summary mode, retained-result store or automatic evidence downgrade. Distinct
301-guard evidence remains large even though singleton locations stay inline.
Use the decoder before the existing per-statement expander; lossless delivery
of retained facts does not change analysis limits or prove exhaustive coverage.

The repository harness records client status `not_verified`. A real client can
be labelled `verified_inline` or `verified_spill` only after confirming the new
format and complete consumption in that client/version. TextContent size alone
cannot establish either state.

### Original compact.v1 tools

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
All other result families in the original v1 keep their entire JSON value within `result`.
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

For the original v1, projection runs after analysis in the existing cancellable worker without a
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

Compact projection can allocate temporary Python objects; fewer output bytes
do not imply fewer reads or lower analysis memory.

## Transaction sampling and bounded facts

`reconstruct_transactions` consumes the existing compact columns through
`cycle_query.iter_sample_rows`. Each iteration borrows one reusable signal view;
it does not allocate a signal dictionary or normalized value for every cycle.
`inspect_tlul` shares those same columns with both handshake inspections and the
transaction core. Two byte vectors carry reset/uncertain-history boundaries,
without copying samples or inventing a reset signal. Its private
`TransactionSampleInput` binds the original parser/epoch, file identity, actual
declarations and ordered selected bits, roles and ordered payload fields,
clock/edge/window, strict-before-edge sampling with zero offset, reset polarity
and semantic version.
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
not interchangeable process RSS measures.

There is no cross-call transaction result repository or persistent transaction
cache. The shared TL-UL input and bounded normalization table have request-local
lifetimes.

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

### Bounded elaboration candidates

When the compile hierarchy records a generate/instance-array gap, a deeply dotted
target may add its exact parent as an `elaboration_candidates` lookup request.
These paths are separately serialized and fingerprinted; they never enter the
lexically proved ancestor chains. At most 64 candidates of depth 64 are admitted.
The focused Slang projector admits only actual Instance objects, retains missing
candidate gaps, and never enumerates siblings. Trace scope checks require exact
candidate coverage before a fact enters the chain. Old artifact identities are
invalidated by the versioned build/worker contract.

The shared `wave_design_binding` maps only a verified unique Verilator top and
exact dump declaration. Driver/load/path, X-trace and dynamic observation routes
reuse that mapping without changing source coordinates in the semantic artifact.
Packed aggregate selections can bind to a typed field alias only when its exact
dump declaration has the expected range and storage identity. Missing/ambiguous
aliases remain observation gaps. Constant field selection is projected from the
same typed assignment RHS; it does not imply complete driver coverage.

### Optional driver dependency inventory

NPI candidate extraction is isolated by the internal
`include_dependency_candidates` flag, including its LSF worker transport.
Only an explicit driver dependency request enables it. Ordinary history and
divergence queries retain their original async observation boundary, opaque
operator gaps, and traversal cost; they never expand an async data pin solely
for the optional inventory.

`explain_signal_driver(include_dependencies=true)` attaches a one-hop inventory
from the selected backend and the same Source Graph artifact, where applicable.
It reuses typed dynamic expressions and NPI pins; unavailable semantics remain
gaps. Candidate roles and exact wave selections do not prove an active branch.
Source coordinates are included when different from the dump selection. The
default performs no extra query. Limits are 32 selections, 256 expression nodes,
32 KiB output and a 10-second cooperative deadline; native NPI cancellation still
waits for its bounded native call to return. Metadata binding runs under the
wave lock after semantic work finishes. A dependency failure cannot change the
original backend or erase its driver facts. Choose a time window and phase in
a separate cycle query; query a direct data input for another explicit hop.

## Runtime log formats

`get_sim_paths.suggested_calls` supplies a callable `parse_sim_log` action when
the log path and simulator are known. Discovery accepts `sim_log`; parsing uses
`log_path`. Snapshots and prerequisite recovery use the same call template.

### Custom checker messages

`parse_sim_log` already recognizes standard `UVM_ERROR` / `UVM_FATAL` messages and VCS / Xcelium assertion failures, with a generic `ERROR` fallback. For project-specific checker, scoreboard, or `$display` output, add Python regular expressions to [custom_patterns.yaml](https://github.com/gokeshenzhen/TraceWeave/blob/main/custom_patterns.yaml). Custom messages do not need to contain `UVM_ERROR` or even `ERROR`; no Python changes are required.

If your messages share a label but the text after it varies, matching that label is enough:

```text
MY_CHECK_FAIL @ 12.5 ns expected=0x12 actual=0x34
MY_CHECK_FAIL @ 20 ns timeout waiting for response
```

Replace the default `patterns: []` with the following, or append the rule to your existing `patterns` list:

```yaml
patterns:
  - name: my_checker
    severity: ERROR
    regex: '^MY_CHECK_FAIL'
```

`^` means the start of the line; everything after the label may vary, with no extra regex needed. If a timestamp or other text precedes the label, use `regex: 'MY_CHECK_FAIL'` to match it anywhere in the line.

- `name` identifies the failure group. `severity` defaults to `ERROR`; `FATAL` and `WARNING` are also supported. Matched custom warnings are included in runtime failure counts. `description` is for maintainers.
- `regex` matches one log line at a time. Use YAML single quotes to preserve backslashes. Built-in assertion and UVM parsing runs first, followed by custom rules in list order (first match wins), then the generic `ERROR` fallback. Recognized compile / elaboration diagnostics remain excluded.

The `expected=0x12 actual=0x34` format above is already recognized automatically, so the simple label rule is enough and no named captures are needed. Suppose your log uses its own field names instead:

```text
MY_CHECK_FAIL @ 12.5 ns want=0x12 have=0x34
```

`regex: '^MY_CHECK_FAIL'` still recognizes the error and preserves the full message. To also extract `want` and `have` as the expected and actual values, replace the `regex` in the rule above with:

```yaml
regex: '^MY_CHECK_FAIL.*want=(?P<expected>\S+)\s+have=(?P<actual>\S+)'
```

Repository installations use the root `custom_patterns.yaml` by default. To keep project rules elsewhere, or when using the PyPI installation, save the YAML above in your own file and pass its absolute path to the MCP server process:

```bash
export TRACEWEAVE_CUSTOM_PATTERNS_FILE="/absolute/path/to/custom_patterns.yaml"
```

This selects that file instead of the default custom rules; built-in formats remain active. The MCP client must inherit or explicitly forward the variable. Restart or reconnect the server after changing it, then parse the log again. Edits to the selected YAML are loaded on the next `parse_sim_log` call.

### Verilator runtime logs

`parse_sim_log(log_path=..., simulator="verilator")` recognizes native runtime
assertions / `$error` / `$fatal` / `$stop`, cocotb test failures with bounded Python
tracebacks, X-HEEP `example_dma` comparisons and software exit values, and
OpenTitan software comparisons / status failures. Compiler warnings and errors,
`ERROR:cocotb:` logger wrappers, source echoes, normal `$finish`, and software
exit zero are excluded. Other custom TB text needs explicit custom patterns.

Bare native timestamps stay `time_ps=None` unless `native_time_unit="ps"` (or
fs/ns/us/ms/s) is supplied with independently verified TB/context evidence.
This setting applies only to native/custom/UVM bare simulation timestamps.
Explicit units take precedence. Conversion uses exact decimal arithmetic and
rounds up sub-ps time to integer ps. Cycle counts, UART rows, and host wallclock
time do not acquire waveform timestamps. Optional TraceWeave acceptance
observers provide software-exit or termination anchors, not the first bad RTL
transaction. Detection source locations are checker locations, not root causes.
Use the same verified unit setting in analysis and snapshot/log diff calls;
snapshots with different settings cannot be compared. Unknown-time analysis
returns log context without querying 0ps.

Verilator discovery reads bounded head/tail log samples, including delayed cocotb
banners. Hierarchy and structural analysis require a real HDL invocation
(`verilator --cc/--xml-only ...`) with an explicit `--top-module`, the execution
cwd (for example Make's entering-directory line), and available ordered
`-f`/`-F` inputs. Include paths, macros, and top parameter overrides are replayed
into the existing Source Graph frontend; C++ files and `.vlt` controls are not
HDL. Waiver/trace/optimizer controls are fingerprinted support inputs; unmodeled
controls stay explicit coverage exclusions. Missing HDL commands, filelists,
or tops remain gaps. A C++ build log alone does not provide HDL context; there
is no automatic unrelated-sidecar or vendor-tree recovery.

Run `get_sim_paths`, then choose log/source and waveform investigations from the
failure phase and target; handshake sweeps are optional. Pass `compile_log` (plus ordered supplementary logs,
simulator and optional top) to design consumers. Native-first is independent of
legacy hierarchy caches. A pure KDB request uses explicit `kdb_path` + `top_hint`;
compile ownership and source fallback still require a log. Explicit design
handles, legacy handles and historical action tokens retain their identity and
recovery contracts. Structural scan budgets/coverage remain separate.

`native_query.py` captures only consumed logs/filelists and environment. Declared
source paths do not become current-source conditions. Discovery configuration
observations and bounded KDB anchors are checked before and after work. Native
candidates bypass cached KDB source hashing; the exact cache discovery fallback
still hashes sources and reports actual read bytes, including validation.
No new KDB is built. Local NPI retains the synchronous native owner; LSF retains
its cancellable worker and namespaced placement configuration.

`native_dispatch.py` adapts the existing connectivity, Language, file/class,
packed and failure engines. Native results omit the full-source design receipt;
`native_route` distinguishes compile metadata, KDB and separately captured source
identity, always retaining unverified KDB/source association. Metadata name/path
filters and three compile-record exports do not select a native provider.
`ownership_basis` separates compile metadata from captured source. Raw native
locations retain unbound/content-unverified evidence. Partial positives, exact
versus lower-bound counts, generate/array/bind parents and qualified classes keep
the shared engines' existing semantics.

An allowed fallback creates a real source context through
`source_fallback_runtime.py`: at most eight handle aliases, four live flights and
one capture admission. It retains references/tasks, never RTL bodies or another
source cache. Exact metadata identities may reuse a retained observation; every
use pins and strictly validates it before/after semantic work. Changed and expired
observations fail instead of silently recapturing. Incomplete identities share
only a live flight. Waiters cannot extend the producer deadline; one cancellation
preserves other waiters, while the final cancellation stops owned work. The event
loop publishes a validated registration ticket only for its current store and a
live waiter. Capacity failures remain atomic. Reset cancels the old coordinator.

A separate 32-entry LRU remembers only confirmed `npi_load_failed` and
`npi_lsf_npi_unavailable` for exact KDB/top/execution/route/runtime environment.
`retry_npi` clears one matching entry. Capability gaps, ordinary query failures,
truncation, timeout and native analysis limits never become sticky load failures.
All native-first phases share the original finite deadline, including fallback
and final validation. X history reuses its cumulative budget across the complete
restart; divergence binds sides separately and restarts discarded graphs from
the original targets. Source preparation/validation never holds a waveform lock.

Worker protocol 5.0 adds `native_source`, carrying bounded metadata, KDB identity,
top, business arguments and remaining limits, without DesignContext or source
bodies. Native workers never discover another KDB or run a fallback backend.
The strict 4.0 decoder remains valid for old operations; 4.0 rejects the new
operation. Responses echo the accepted request version, and parent boundaries
reject version, identity, ownership, query and work-count mismatches. Old full
context source requests retain their source validation.

MCP input validation reuses checked JSON Schema validators in a process-local
LRU (128 entries, 256 KiB of serialized schemas). The key includes the exact
schema content, and validators own private decoded schemas, so changed tool
definitions cannot reuse old rules. Every request still runs full instance
validation with the SDK's error selection. Only repeated schema self-validation
is avoided; request arguments/results, design identities, KDB/source guards and
native sessions are not cached here. SDK output handling remains unchanged.
