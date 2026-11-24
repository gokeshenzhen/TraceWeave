# Waveform MCP — Standard Debug Workflow

## Overview

One simulation investigation route is:
`get_diagnostic_snapshot → get_sim_paths → scan_structural_risks
→ parse_sim_log → targeted investigation or optional sweep_handshakes`.
Choose tool order and scope from the current run, failure phase and known target.
Recommendations can precede a sweep. Reuse current cached steps when the snapshot
confirms them; standalone waveform queries need neither discovery nor a hierarchy
build. Inspect the tool's current MCP schema for exact arguments. Build/elaboration
diagnostics and earlier-run waveforms retain separate evidence identities.

To omit structural scanning from this default flow, set
`TRACEWEAVE_STRUCTURAL_SCAN_IN_WORKFLOW=0` in the MCP server environment and
restart or reconnect. Explicit scan requests remain available; skipped checks
remain unchecked. Handshake investigation remains optional.

### Reading driver evidence

For Source Graph driver queries, a process may contain many assignment
statements but is one structural writer per target bit. Inspect `traversal` for
separate writer, assignment-work and retained-evidence counts and limits. A
positive bit fact does not establish an exhaustive driver set; overlapping
writers do not establish an observed value conflict. Dynamic queries retain
all bounded statement branches, including overrides in the same process.
Read each provenance segment's `resolution`, `reason_codes` and
`driver_set_complete`. `found` says a source was found, `proved_no_driver`
requires complete writer enumeration, and `unknown` retains a coverage or work
gap. Neither a null source path nor zero matches proves absence. A partial
Source Graph result can remain the final result after unsupported Static;
check `attempted_backends` and the scope blocker instead of repeating the same
frontier expansion.
Driver calls now default to `traceweave.driver.compact.v1`, including suggested
calls that omit `output_format`. First resolve each response-local
`statement_semantics_ref` and `statement_evidence_ref` using the accompanying
typed tables (or `src.evidence_output.expand_compact_result`). All retained
evidence is in the same response. Legacy text consumers can request
`output_format="full"`; other tools retain their defaults. Table IDs and location
occurrences do not count independent writers, and different writers sharing a
table value remain distinct. A statement can appear in multiple per-bit
provenance segments. Those occurrences do not add writers. A complete artifact's negative result must not
be transferred to a scoped query with exclusions. Sample the relevant waveform
before making claims about values over time.

After decoding, expand each group's `statement_evidence` when inspecting branches; the top
`source_line` is only the first location. `statement_semantics` is shared only
when all semantic fields agree. Use `query_instance_path` for the requested
signal and `resolved_instance_path` for the first driver. VCD has no port
direction metadata, so a null direction must not be inferred from naming.
Analysis truncation, complete delivery of the retained evidence, and reading
only part of a client's spilled file are separate conditions. Neither compact
bytes nor table counts can establish complete analysis. Different evidence can
still exceed a client's delivery limit; narrowing analysis or adding pagination
is not an implicit consequence of using the lossless codec.

If an explicit driver action's process-scoped hierarchy handle expired, the
server first looks for the exact current hierarchy and may perform one bounded
recovery. Read `hierarchy_recovery`: historical source tokens are checked again,
and absence of such tokens means current-source context only. Changed or
ambiguous identities, timeouts and source budgets return a prerequisite call.
Structural scanning follows its own workflow setting. Keep the original case
and identity tokens when recovering a stale action. The full view and its
handle tools are described in the [architecture guide](architecture.md#handle-based-hierarchy-access).

This document defines the recommended tool invocation order for the Waveform MCP server. It is intended to be used as the basis for `server.py` instructions, guiding the AI agent through a structured debug flow.

## Continuing with source files

An unsupported, shallow or incomplete connectivity result leaves an automated
evidence gap. The client agent should continue investigating with its own file
reading tools when those are available. `static_backend_no_path_api`, empty
partial results and unresolved boundaries do not prove that no connection exists.
Preserve any complete negative or objective exclusion within its stated scope.

1. Use the compile log for the design being traced. Confirm a file named in the
   log with `lookup_tb_files(compile_log=..., basename=...)` and read the matching
   compiled path. If several paths share that basename, use the reported path
   to disambiguate. When continuing a prepared source query, reuse its
   `design_handle` (including one returned as `native_route.source_design_handle`).
   A changed source identity requires explicit preparation.
2. Read the verified declarations and trace assignments, port connections,
   always blocks, parameters, generate conditions, packed fields and bit ranges.
   Record a file:line for each inferred hop. Keep unknown macro/parameter or
   elaboration bindings explicit instead of guessing a concrete instance path.
3. Report these hops as **source inference**, separately from tool/backend facts.
   Source inspection can establish textual relationships and hypotheses; use
   waveform/log evidence to check active branches, values, clock phases and a
   claimed runtime root cause. Continue checking competing explanations.
4. Use another tool call only when it adds evidence or changes the query scope.
   Do not loop on an unchanged unsupported result or require a full hierarchy
   build merely to read a verified file. Respect cancellation, time budgets and
   user stop instructions. If source or runtime evidence is missing, report
   that specific gap and the next useful observation.

These instructions enable investigation beyond the connectivity backend's
coverage. They do not change an unsupported result into a tool-proved path or
guarantee that every client exposes local file-reading tools.

## Workflow

```
User: "Help me debug /path/to/verif, case0"
│
▼
Step 0: get_diagnostic_snapshot()
│  Reuse current session evidence and follow missing-step suggestions.
│  This reads cached state; it does not revalidate source/KDB identity.
│
▼
Step 1: get_sim_paths(verif_root, case_name?, sim_log?, wave_file?, compile_log?)
│  Discover all relevant file paths automatically.
│  Returns: discovery_mode, case_dir, compile_logs (with phase tag),
│           sim_logs, wave_files, simulator (auto-detected),
│           fsdb_runtime, hints, available_cases
│
│  Key decisions:
│  - For a non-standard layout, pass explicit sim_log/wave_file/compile_log:
│    any supplied field is used as-is, omitted fields are still auto-discovered
│    (a sim_log path anchors the case dir → finds its wave + compile/elab logs)
│  - If discovery_mode == "unknown" → stop guessing and follow hints
│  - If case_name omitted in root_dir mode → check available_cases, ask user to pick one
│  - If hints contain warnings (empty log, missing wave) → inform user early
│  - For a complete single-log flow, pick compile_log with phase="elaborate"
│  - For split VCS source-compile/elaboration logs, use the source-compile log
│    as primary and pass the other logs to design queries in build order via
│    supplementary_compile_logs. scan_structural_risks uses that same primary.
│  - Store simulator for all subsequent tool calls
│  - If fsdb_runtime.enabled is false → ignore `.fsdb` when `.vcd` is available
│  - Only proceed to step 3 when `sim_logs` is non-empty
│
▼
Step 2: scan_structural_risks(compile_log, simulator)
│  Omit this step when TRACEWEAVE_STRUCTURAL_SCAN_IN_WORKFLOW=0, unless the
│  user explicitly requests structural scanning.
│  Keep the same scan scope and coverage requirements. Pass the selected
│  compile_log, ordered supplementary_compile_logs and top_hint to subsequent
│  design queries. Native-first uses an existing KDB before full source work.
│  Ordinary unavailability prepares source within the request; native_route
│  returns its source_design_handle. KDB/source association stays unverified.
│
│  Explicit prepare_design_context remains available for strict source identity
│  and recovery. Run it alongside the scan when explicitly preparing source.
│  A changed or expired source observation requires explicit preparation.
│  For a particular instance, pass compile_log directly to find_tb_instance
│  or get_tb_subtree. Query dumped signals directly with the waveform path.
│
│  Call build_tb_hierarchy only for an explicitly requested full RTL/TB view
│  or legacy hierarchy handle. If also scanning the same log, run both in
│  parallel. The returned hierarchy_handle is for browsing that full view.
│
└─ scan_structural_risks independently scans the compiled RTL/TB source set.
   Its lexical CPU work runs in a lock-free cancellable worker. Native semantic
   queries preserve the synchronous local NPI owner; an in-progress native call
   can delay the event loop. Configured LSF queries use the existing worker path.
   Returns source-anchored structural risks (for example overlap, multi-drive,
   incomplete control, or narrow-condition findings). Risks overlapping the
   eventual failing signal/source path are high-priority root-cause candidates,
   but remain facts to correlate rather than verdicts. Always inspect
   lexical_coverage_status separately from semantic.status, semantic.gaps and
   semantic.propagation. The top-level coverage_status also degrades when a
   requested deep scan has incomplete semantics. Complete lexical coverage
   with zero risks only means the covered lexical rules found no risks.
   Default analysis_mode=auto runs/reuses lexical rules and reuses compatible
   semantic facts without a cold Slang build after first attempting existing KDBs
   through NPI. NPI may request a local/LSF license; scanning never builds a KDB.
   Use top_hint when compile metadata has no unique top. Read category_coverage,
   result_groups and native_execution: input-port observations and source checks
   retain separate domains/identities, and default comparisons remain missing until
   source evidence covers them. Empty actuals do not establish default behavior.
   semantic.status=not_run is neither a skipped lexical scan nor a semantic pass.
   deep permits bounded Slang supplementation; fast never probes or loads NPI.
│
▼
Step 3: parse_sim_log(log_path, simulator)
│  Get grouped error summary from simulation log.
│  Returns: groups list, normalized failure_events, time normalization fields,
│           rerun hints such as previous_log_detected / candidate_previous_logs,
│           and log_snapshot_id for same-path rerun diffing
│  candidate_previous_logs are conservative runtime-log candidates from bounded
│  sibling head/tail samples; compile/elaboration/build logs are never baselines.
│
│  What the agent does:
│  - Identify the earliest and most frequent error groups
│  - Prefer `failure_events[0].time_ps` as the waveform time anchor when present
│  - If the error cites a source file, verify its compiled path with
│    lookup_tb_files(compile_log=..., basename=...) before opening it.
│    If it names an RTL instance, use find_tb_instance(compile_log=..., path=...).
│  - Decide which group to investigate first (usually group_index=0)
│  - If previous_log_detected == true, consider diff_sim_failure_results early
│  - If the simulator overwrites the same log path on rerun, call
│    diff_sim_failure_results with snapshot IDs or only new_log_path; the
│    previous parsed snapshot becomes the baseline
│
▼
Step 4 (optional): sweep_handshakes(wave_path, scope?, start_time_ps?, end_time_ps?)
│  Batch protocol investigation for opaque timeout/hang or compare symptoms.
│  With a known module, narrow scope/window; with a known interface or signals,
│  use targeted inspection directly. An unperformed sweep remains unchecked.
│  Returns: discovered_count, interface_count, flagged_count,
│           coverage_status, coverage_warnings, suggested_next_actions,
│           interfaces[], skipped[]
│
│  What the agent does:
│  - Prefer the first sweep unscoped unless the interface scope is already known
│  - Always examine the finding_summary before diving into individual interfaces
│  - Read discovery separately: null scope_total means enumeration did not prove
│    an exact scope count; scope_total_lower_bound is only a lower bound. Narrow
│    scope when a discovery budget is exhausted. Raising max_interfaces only
│    checks more already-discovered candidates.
│  - Suggested actions identify the gap addressed and the gaps that remain.
│    With several limits, prefer scoped metadata discovery and a justified
│    smaller window. A larger interface cap is suggested only for a cap-only
│    gap. Unknown time anchors are not replaced with guessed windows.
│  - Idle ready remains a count, with no anomaly flag or ranking weight. Req/ack
│    names do not establish valid-hold semantics and automatic sweeps skip them.
│    Supply confirmed semantics in an explicit inspect_handshake call. Ambiguous
│    clocks and payload ownership need explicit mapping; a packed-only interface
│    is outside scalar discovery coverage.
│  - Interpret flagged_count only with coverage_status:
│    * coverage_status="zero_coverage" means no protocol interfaces were checked;
│      it is not a protocol pass. A scoped result may retry without scope or at
│      a parent scope. An unscoped zero-interface result with no changing action
│      is terminal for routing: retain the warning, do not replay the same call.
│    * coverage_status="truncated" or "degraded" means partial coverage; do not
│      call the protocol clean from flagged_count=0. This includes native FSDB
│      transition-prefix truncation (`transition_truncated_count > 0`); narrow
│      the time window for a complete targeted check. Follow any available
│      suggested_next_actions only when they change scope/window/edge/cap. With
│      no changing action, surface the dump/clock/window prerequisite instead.
│    * coverage_status="complete" and flagged_count=0 means the discovered,
│      supported interfaces had none of the checks' reported findings. It is not
│      proof that every protocol behavior in the design is correct.
│  - Global findings (flagged interfaces) are facts to correlate against the symptom,
│    not root-cause verdicts. Findings on one channel/interface may be unrelated to
│    a mismatch on another channel.
│  - If the symptom is on interface A but global sweep findings are on interface B,
│    that is a correlation fact, not a clearance. Run targeted checks on A to
│    establish whether A's protocol is clean; global findings on B remain true.
│
▼
Step 5: recommend_failure_debug_next_steps(log_path, wave_path, simulator, ...)
│  Get a strong default failure target and role-ranked signal suggestions.
│  Returns: primary_failure_target, recommended_signals, recommended_instances,
│           suspected_failure_class, recommendation_strategy, failure_window_center_ps,
│           correlated_structural_risks, runtime_protocol_findings,
│           runtime_protocol_coverage
│  - runtime_protocol_findings: flagged interfaces carried over from a compatible
│    sweep_handshakes cache. Facts to correlate, not a verdict.
│  - runtime_protocol_coverage reports not_run or the actual wave/scope/window,
│    discovery gaps and zero/degraded/truncated coverage, including empty findings.
│  - suggested_next_actions may offer an optional sweep or a changing retry.
│    Sweep coverage never creates a required_next_call or workflow_incomplete.
│    get_diagnostic_snapshot retains it in protocol_health, outside missing_steps.
│
│  What the agent does:
│  - Use the top recommended signals first instead of blind substring search
│  - Prefer signals with useful role/reason_codes (state/counter/handshake/etc.)
│  - If the recommendation is weak, fall back to explicit search_signals
│
▼
Step 6: search_signals(wave_path, keyword)
│  Confirm full hierarchical paths for signals relevant to the error.
│  `keyword` also accepts a LIST of keywords — batch every stem you plan to
│  look up into ONE call (one result entry per keyword, in order) instead of
│  issuing consecutive single-keyword searches.
│  Returns: matching signals with bit width, `direction`, and `var_type`.
│    - `direction`: input/output/inout/implicit (FSDB only). VCD always null.
│    - `var_type` : wire/reg/integer/real/parameter/memory/...
│    Clients filter ports/nets/variables in a scope by combining a hierarchical
│    keyword (the scope prefix) with these fields, instead of a dedicated tool.
│  Note: `.fsdb` wave paths are usable only when fsdb_runtime.enabled is true.
│        Port-direction filtering requires FSDB — VCD cannot encode direction.
│
│  How the agent picks keywords:
│  - Use names from the error message, recommended signals or known waveform scopes.
│  - Inspect a known parent's children with get_tb_subtree(compile_log=..., root=...).
│  - When taking names from RTL source, verify the file against the compile log first.
│
│  May need multiple calls with different keywords.
│
▼
Step 7: analyze_failures(log_path, wave_path, signal_paths, simulator)
│  Core analysis: combines log context + waveform snapshot for one error group.
│  Returns: summary, focused_group, log_context, wave_context, analysis_guide
│  Note: `.fsdb` wave paths are usable only when fsdb_runtime.enabled is true
│
│  The agent should:
│  - Follow analysis_guide steps (check timing, signal values, pre-window history)
│  - Compare expected vs actual signal behavior
│  - Identify root cause or narrow down the investigation
│
▼
Step 8: Deep dive (on demand, based on step 7 findings)
   │
   ├─ analyze_failure_event(log_path, wave_path, simulator, failure_event, ...)
   │    When: Want failure-centric instance/source correlation
   │    Output: time_anchor, likely_instances, recommended_signals, related_source_files
   │
   ├─ get_error_context(log_path, line)
   │    When: Need to inspect other error groups beyond the one selected in
   │          step 3 / analyzed in step 7
   │    Input: first_line from a different group in step 3's results
   │
   ├─ explain_signal_driver(signal_path, wave_path, compile_log, top_hint?)
   │    When: Waveform shows a suspicious signal and the agent needs the likely RTL driver
   │    Output: driver_status, driver_kind, source_file, source_line, expression_summary,
   │            traversal.{returned_fact_count, output_limit, output_truncated,
   │            visited_state_count, state_limit, state_truncated,
   │            search_exhaustive, incomplete_reasons, continuation_supported}
   │    Notes: For deeper / cross-hierarchy traces a Verdi KDB enables the NPI
   │           backend, which can cross instance port boundaries. If the
   │           simulator is Xcelium and no KDB exists yet, get_diagnostic_snapshot
   │           lists `build_kdb` in `missing_steps` — call that first.
   │           NPI execution defaults to local. With
   │           TRACEWEAVE_NPI_EXECUTION=lsf, the explicit driver/load/path tools
   │           submit an exact-only worker using TRACEWEAVE_NPI_LSF_QUEUE
   │           (set that namespaced queue directly; only map an already-existing
   │           generic LSF_QUEUE if desired; TraceWeave does not read it).
   │           Read
   │           backend_status.execution_mode / scheduler_status / worker_status /
   │           fallback_reason: ordinary worker/backend unavailability may use
   │           local Source Graph or Static fallback; inspect actual_backend.
   │           Identity failures, cancellation and timeouts do not advance fallback.
   │           Recursive NPI fan-in admits at most 4,096 native states and
   │           returns at most 32 driver facts. A positive bounded prefix has
   │           driver_status="partial" and remains useful, but it is not an
   │           exclusive or complete driver-set claim. Read traversal instead
   │           of inferring completeness from a non-empty driver_chain.
   │           trace_x_source preserves this receipt and stops with
   │           driver_traversal_incomplete instead of naming one bounded
   │           candidate as an exclusive root cause.
   │           driver_status="testbench_driven" (cross_check.conflict) means NPI
   │           found NO RTL driver — its only candidate is also a LOAD of the net
   │           (interface-slice alias / a register reading the net), so the real
   │           driver is the TB (a UVM driver via virtual interface + clocking
   │           block, invisible to RTL fan-in). Start in the TB driver/BFM, not a
   │           DUT register. For an AHB master's HTRANS/HADDR this is the correct
   │           answer; do NOT read a landed DUT/matrix register as a mis-wire.
   │
   ├─ find_signal_loads(signal_path, compile_log, kind_filter?, include_expr?)
   │    When: Symmetric to explain_signal_driver — list the consumers (fanout)
   │          of a signal: child instance input ports, RHS in assigns /
   │          procedural assignments, always-block sensitivity lists.
   │    Output: loads[].{load_path, kind, source_file, source_line, expr},
   │            enumeration.{returned_count, output_limit, output_truncated,
   │            search_exhaustive, incomplete_reasons, continuation_supported}
   │    Notes: Static backend is shallow_only and cannot follow interface
   │           positional bindings or cross-hierarchy fanout — those are
   │           surfaced via stopped_at. NPI backend (when a Verdi KDB is
   │           present) resolves both with direct-load indexes and bounded
   │           output-port boundary steps; it does not expand a whole fan-out
   │           cone for a direct-load query. All backends cap output at 256.
   │           A positive truncated prefix is usable evidence, but only
   │           search_exhaustive=true means it is the complete list. There is
   │           currently no resumable pagination token.
   │           Each load carries source_info_origin = "compile_log" or "npi".
   │
   ├─ trace_x_source(signal_path, wave_path, compile_log, time_ps, max_depth?)
   │    When: A signal is X/Z at the failure time and the agent wants to
   │          trace propagation back to the root cause net.
   │    Output: propagation_chain[], root_cause, trace_status, analysis_guide
   │    Notes: Combines waveform reads with trusted local/LSF NPI, bounded Source
   │           Graph and Static routing. Both modes accept design_handle; healthy
   │           NPI needs no full hierarchy. Prepared-context Source Graph trace
   │           planning uses the captured compile context with bounded semantic
   │           candidates and artifact expansion. An artifact change restarts
   │           the trace from its original root; unavailable capabilities may
   │           fall back to bound Static metadata with explicit gaps. At an
   │           unresolved boundary, inspect a driver query and continue reading verified source, checking propagation
   │           and clock phase against the waveform. See the source workflow above.
   │
   ├─ trace_signal_path(from_signal, to_signal, compile_log, expand_assigns?)
   │    When: Need a connected chain of nets between two signals (e.g. "how does
   │          an input port reach the failing assertion?"). Uses trusted NPI,
   │          then bounded Source Graph; prepared queries need no full hierarchy.
   │          Final Static has no path API. An unsupported/inconclusive result is
   │          not proof of disconnection. Use driver/load facts where useful and
   │          continue with client source-file reads when automation reaches its limit.
   │    Output: path[], hops, found, direction_note
   │    Notes: This is connectivity (any direction), NOT temporal driver direction.
   │           Use explain_signal_driver for "what drives X temporally". Set
   │           expand_assigns=true when you want explicit assign hops surfaced
   │           instead of collapsed.
   │
   ├─ build_kdb(compile_log, top_hint?, force_rebuild?)
   │    When: Xcelium (xrun) flow and `backend_status.kdb_path` is null,
   │          or you want to refresh a cached KDB after source changes
   │    Output: status (rebuilt / cached / failed), kdb_path, cache_dir,
   │            build_script_path (runnable build.sh), vericom_log, elabcom_log
   │    Notes: Cache lives under $TRACEWEAVE_CACHE_DIR/kdb/<hash>/. New automatic
   │           backend discovery can find the cached KDB. A design_handle with
   │           an existing binding retains that binding; prepare a new context
   │           to select the new KDB. VCS users get a cheaper
   │           path: recompile with `-kdb=only` (suggested by kdb_hint).
   │
   ├─ get_signal_transitions(wave_path, signal_path, start_time_ps, end_time_ps)
   │    When: analyze_failures' pre_window_transitions is not enough,
   │          need full transition history of a signal
   │    Note: `.fsdb` wave paths require fsdb_runtime.enabled == true
   │
   ├─ get_signals_around_time(wave_path, signal_paths, center_time_ps)
   │    When: Need to inspect additional signals not included in step 6,
   │          or examine a different time point
   │    Note: `.fsdb` wave paths require fsdb_runtime.enabled == true
   │
   ├─ get_signal_at_time(wave_path, signal_path, time_ps)
   │    When: Need exact value of one signal at a precise time
   │    Note: `.fsdb` wave paths require fsdb_runtime.enabled == true
   │
   └─ get_waveform_summary(wave_path)
        When: Need basic waveform metadata (simulation duration, signal count)
        Note: `.fsdb` wave paths require fsdb_runtime.enabled == true
        Useful for sanity checks before deep analysis
```

## Root-Cause Discipline For Protocol / Scoreboard Mismatches

When a failure is a protocol or scoreboard mismatch (handshake stall/deadlock,
data mismatch, or timing violation), do not conclude a root cause from one-sided
evidence:

- Carry at least two competing hypotheses about where the fault is — for example
  the initiator/stimulus side versus the responder/DUT side — and keep both
  alive until waveform evidence rejects one.
- Before attributing the root cause to one side, check the opposite side with
  waveform evidence.
- A clean result on one side is not a whole-protocol verdict: "no violation found
  on side X" does not establish "the protocol is correct." State which sides you
  checked and which you did not.
- A confirmed violation/anomaly is a perception fact, not a consequence verdict.
  Before stating what it *did* (its effect on the failing observable), confirm
  that effect against the actual values — do not infer the consequence from the
  anomaly alone, since the same anomaly can have different downstream effects.
  (E.g. a stall-time hold violation may have skipped a beat or corrupted one; the
  scoreboard `got=` value disambiguates — a never-written `00` vs a wrong
  non-zero byte.)

This discipline pairs with tool coverage facts: inspection tools should report
which checks they actually performed, and the agent uses that coverage to avoid
treating a one-sided result as a full protocol conclusion.

**Global vs. Targeted Protocol Checks:** `sweep_handshakes` discovers protocol
interfaces across the requested scope and checks the selected AHB / valid-ready
bundles within discovery, interface and resource limits. Capable FSDB readers
stream bounded sample blocks with continuous protocol state; reaching the end
of selected histories does not remove discovery, interface-cap or skipped-check
gaps. A work-budget stop remains partial, and compatibility readers can still
stop at a materialized-transition prefix. Always inspect coverage and warnings.
`inspect_handshake`,
`reconstruct_transactions`, and `verify_window` are targeted checks on one interface
or signal. A targeted clean result ("no violation on Master0's R channel") does NOT
erase earlier global findings ("W channel has payload-hold violations"). State both
facts: the global findings exist, and the targeted check on the symptom-correlated
interface is clean. Continue into the symptom's producer, consumer or reference
model as the remaining evidence warrants; the combination alone does not select HVL/BFM or
exclude an internal RTL fault.

On a scoreboard/compare-style failure, `parse_sim_log` sets a generic
`protocol_symptom_hint` (mirrored into `get_diagnostic_snapshot`'s top-level
output) reminding you that such a mismatch is frequently the *symptom* of a
lower-level bus-protocol problem. Consider `sweep_handshakes` when the interface
is unknown, choosing scope/window from evidence; use `inspect_handshake` directly
for a known interface. The hint is a boundary-safe pointer only: it
never asserts a protocol type or names a specific signal, and the
two-hypothesis discipline above still applies.

## Tool Dependency Graph

```text
get_diagnostic_snapshot (reuse current cached steps)
  └─ get_sim_paths

get_sim_paths
  ├─ provides compile_log / simulator / log_path / wave_path to downstream tools
  ├─ scan_structural_risks (default structural policy)
  ├─ parse_sim_log
  ├─ sweep_handshakes (optional)
  ├─ recommend_failure_debug_next_steps
  └─ targeted investigation:
       search_signals / analyze_failures / deep dive:
         analyze_failure_event
         explain_signal_driver / find_signal_loads
         get_error_context / get_signal_*
         trace_x_source / trace_signal_path
```

## Parameter Flow

| Parameter | Source | Consumed by |
|-----------|--------|-------------|
| `compile_log` | Complete single-log build: elaborate log; split VCS build: primary source-compile log | Structural scans, file/instance queries, connectivity and X traces, explicit full-tree or source preparation, `build_kdb` |
| `design_handle` | `prepare_design_context → design_handle` or `native_route.source_design_handle` after source fallback | Hierarchy/source queries, tracing, failure correlation and packed-field resolution |
| `simulator` | `get_sim_paths → simulator` | `prepare_design_context`, `build_tb_hierarchy`, `parse_sim_log`, `analyze_failures` |
| `log_path` (sim) | `get_sim_paths → sim_logs[0].path` | `parse_sim_log`, `get_error_context`, `analyze_failures` |
| `wave_path` | `get_sim_paths → chosen wave file (.vcd preferred when fsdb_runtime.enabled=false)` | `sweep_handshakes`, `search_signals`, `get_signal_*`, `analyze_failures` |
| `failure_event` | `parse_sim_log → failure_events[]` | `analyze_failure_event` |
| `signal_paths` | `search_signals → results[].path` | `analyze_failures`, `get_signals_around_time` |
| `group_index` | Agent decision from `parse_sim_log → groups` | `analyze_failures` |
| `line` | `parse_sim_log → groups[].first_line` | `get_error_context` |
| `center_time_ps` | `parse_sim_log → failure_events[].time_ps` or `groups[].first_time_ps` | `get_signals_around_time` |
| `signal_path` | `search_signals → results[].path` or waveform observation | `explain_signal_driver`, `find_signal_loads`, `trace_x_source` |
| `from_signal` / `to_signal` | `search_signals → results[].path` (endpoints chosen by agent) | `trace_signal_path` |

> Time parameters (`time_ps`, `center_time_ps`, `start_time_ps`, `end_time_ps`) accept a **TimeSpec**: a raw integer (ps), a cursor reference `@<name>`, or a unit literal like `12.34ns`. So a time anchor located by `diff_first_divergence` / `period` (auto-registered as a cursor) can feed downstream `get_signal_*` / `trace_x_source` calls as `@<name>` instead of a copied timestamp.

## Optional Analysis Primitives

Choose these tools according to the symptom and known target. `sweep_handshakes`
is an optional batch investigation; known interfaces or signals can use targeted
tools directly. Missing or partial sweep evidence remains explicit and never
requires automatic retries.

- `period(wave_path, signal, edge?)` — when a signal should be periodic (clock, strobe, fixed-rate valid) and the symptom is a cadence/throughput irregularity with no value in the log. Returns the dominant period and the first off-beat (auto-cursor).
- `diff_first_divergence(wave_path_a, signal_a, wave_path_b, signal_b)` — when two waveform signals should match: cross-run (passing vs failing) or within-run (lockstep / shadow). Returns the first confirmed known-value difference in the covered window (auto-cursor); X/Z and incomplete observations remain explicit. Needs both sides dumped as waveform signals; does not compare against a software reference model.
- `cursor_set` / `cursor_list` / `cursor_delete` — manage the named time anchors referenced above.
- `inspect_handshake(wave_path, clock, valid, ready, payload?)` — cycle-by-cycle classification of a clocked valid/ready handshake: stalls, long-stall windows, backpressure imbalance, payload-hold violations during a stall, and **premature valid deassertion** (`check_valid_hold`, default on): a stalled beat whose valid/htrans drops the next edge before ready/HREADY arrives — the master dropping the transfer instead of waiting (the AHB master-not-waiting-for-HREADY bug). The deassertion check needs no `payload` and catches what payload-hold cannot: a 1-cycle stall (`max_stall_cycles==1`) leaves no room for payload to change, and htrans (the derived valid) is not a payload signal. For protocol-timing bugs that leave no value pattern in scoreboard logs. AHB has no literal valid — pass `valid_htrans` instead. Its `coverage` object reports only dimensions it actually checked (`stall_checked`, `backpressure_checked`, `payload_hold_checked`/partial, `valid_hold_checked`); side labels must come from discovery/caller context, not this inspection tool. On a finding it sets `violating_signal` (the valid/htrans for a premature deassertion) + a `next_actions` link to `explain_signal_driver`. For the **one-sided** violations (payload-hold, premature deassertion) it also returns a structured `attribution` block (`violating_side=valid_driver`, `exonerated_side=ready_driver`): both are breaches of the valid-driver's obligation, so follow the channel producer's driver rather than the consumer/ready driver. This is protocol role, not trace-ownership: the valid-driver is the channel producer (master on AXI AW/AR/W, slave on R/B; AHB htrans is always master), and `explain_signal_driver` on `valid` lands on the actual instance — but when the producer is a UVM/TB driver (procedural drive via virtual interface + clocking block), NPI cannot see it and returns `driver_status="testbench_driven"` rather than naming a DUT register that merely reads the bus (do not read that as a mis-wire). A plain stall is genuinely two-sided, so `attribution` stays empty and the link targets `ready` — the trace holds values, not ownership; attribution = bus-fact + drive-direction, composed by you. With no finding in a complete targeted check, investigate internal producer/consumer behavior and the expected model; interface checks alone do not select a root cause.
- `suggest_handshakes(wave_path, scope?)` — scan the waveform and propose ready-to-use `inspect_handshake` bundles (pairs `*valid`/`*ready`, finds the clock, groups payload). Run before `inspect_handshake` so you don't hand-assemble signal paths.
- `suggest_protocol_bundles(wave_path, protocol=ahb|apb, scope?)` — scan AHB/APB-style protocol bundles where there is no literal valid. AHB candidates return `valid_htrans`-based `inspect_handshake` args; APB candidates return `psel`/`penable`/`pready` facts and mark the missing derived-valid step. For AHB candidates the result also carries a `next_step` field — a copy-paste-ready `inspect_handshake(...)` call per interface — because discovery only LOCATES the bundle; running that call is the analysis step. Do not stop at discovery. Treat `direction_tag=unknown` as a real coverage limitation, not as permission to infer a side.
- `sweep_handshakes(wave_path, scope?, start_time_ps?, end_time_ps?)` — optional batch investigation for timeout/hang or compare symptoms when the responsible interface is unknown. Discover valid/ready and AHB interfaces within the selected scope, then inspect the selected time window in one call. The comparative table tags each row `kind=valid_ready/ahb`; APB requires a derived valid and is excluded. `get_diagnostic_snapshot.protocol_health` retains coverage and optional suggestions, outside `missing_steps`. Read `coverage_status`: `zero_coverage` checked no interfaces; `truncated`/`degraded` means partial coverage. Zero findings never establish a clean result outside the checked scope/window. No-progress terminal coverage remains visible without blind retries.
- `verify_window(wave_path, clock, mode, predicate | antecedent+consequent | delta)` — evaluate a temporal predicate (always/never/eventually/implication/sequence) over a clock window and return a `holds` verdict plus a concrete witness/counterexample. To prove or disprove an RTL inference in one call. For `implication`, `overlap=false` (`|=>`) starts the response window the NEXT cycle — the shape for a **hold/stability** property ("B must STILL hold next cycle", e.g. `(htrans==2 && hready==0) |=> htrans==2` proves premature-deassertion). With the default `overlap=true` such a property is a **vacuous pass** (B already true on A's own cycle); the result is flagged `vacuous=true` + a `VACUOUS PASS` warning so the `holds=true` is not misread as exclusion evidence — re-run with `overlap=false`. `sequence` checks the per-accepted-beat increment of one signal (address-stride): `predicate` is the accepted-beat gate, `delta`=`{signal,value,op?,modulo?,restart_when?}` — `modulo` absorbs a legal WRAP wrap-around, `restart_when` (e.g. htrans==NONSEQ) re-seeds at burst starts, both supplied by you so the tool stays burst-decode-free. On a violation it sets `violating_signal` + a `next_actions` link to `explain_signal_driver` (master-driven signal → points at the master by elimination). Multi-slave/master: scope a property to one subset with a predicate term (per-slave `HSEL`, per-master `HMASTER`) — `inspect_handshake`'s htrans-only valid cannot qualify by select, so use the verify_window gate for per-slave/per-master checks.
- `reconstruct_transactions(wave_path, clock, req_valid, req_ready, cmp_valid, cmp_ready, ...)` — id-correlated request/response transaction layer: per-transaction latency plus outstanding/ordering/unmatched facts. AXI read AR→R, write AW→B (+ optional W-data channel); `req_id`/`cmp_id` optional (omit both for in-order AXI-Lite/APB streams).

For a failed concurrent assertion, use `explain_property(include_check_plan=true)`
on the exact directive instance, then call `verify_window(mode="property",
property_plan=<handle>, wave_path=..., log_path=...)` with VCD, FSDB or FST. Log selection uses the first
failure of that instance, independently of other assertion failures. Alternatively
pass the normalized `failure_event` returned by log parsing, or an explicit
`failure_time_ps` TimeSpec. The tool derives the relevant attempts and history;
the failure timestamp is not automatically the attempt start. Logged start times
are preferred. Text plans can infer their waveform scope from the supplied failure
instance, or use `property_scope` / exact `property_bindings`.

Read `failure_reproduced`, `not_reproduced`, `insufficient_evidence` or `unsupported`
alongside coverage, candidate selection and per-attempt evidence. A reproduced
failure is a waveform fact, not a DUT/TB root-cause assignment. A cutoff, pending
obligation, missing history or unobserved signal is not a pass. This targeted check
retains its own coverage independently of structural scans and optional sweeps.

## Iterative Debug Pattern

After step 7, the agent may loop:

```
analyze_failures(group_index=0) → findings → need more signals?
    │                                              │ yes
    │                                              ▼
    │                                    search_signals(new keyword)
    │                                              │
    │                                              ▼
    │                                    get_signals_around_time(new signals, same time)
    │                                              │
    │                                              ▼
    │                                    updated understanding
    │
    ├─ Root cause found → report to user
    │
    └─ Not enough info → analyze_failures(group_index=1) → next error group
```

## FSDB report export

When preparing the final report, the local agent exports one nWave signal RC for
each independent issue with confirmed signal and time evidence in a single FSDB.
Group repeated reports of the same issue into one view. Choose signals that show
the observed failure and the upstream or opposite-side behavior actually checked.
Use function names meaningful to the investigation. The exporter adds a subgroup
for each full instance path, including distinct instances of the same module.

Export is enabled by default. To disable it, set `TRACEWEAVE_VERDI_RC_EXPORT=0`
in the MCP server environment and reconnect. The local command also honors this
setting in the agent's shell and returns `{"status":"disabled","reason":"TRACEWEAVE_VERDI_RC_EXPORT=0"}`
before reading a manifest or creating `debug/`. Unset the variable or set it to
`1` to enable export again.

Run the command supplied in the MCP initialization instructions from the agent's
current working directory. For example, with a repository checkout:

```bash
/path/to/TraceWeave/.venv/bin/python /path/to/TraceWeave/scripts/export_verdi_rc.py <<'JSON'
{
  "issue": "read_data_mismatch",
  "fsdb": "/path/to/run/wave.fsdb",
  "groups": {
    "Read data": [
      {"path": "tb.actual[7:0]", "role": "evidence"},
      {"path": "tb.expected[7:0]", "role": "evidence"}
    ],
    "Acceptance": ["tb.u1.valid", "tb.u1.ready"]
  },
  "changes": {
    "mux.sv:21 data[7:4] -> data[3:0]": [
      {"path": "tb.u0.data[7:0]", "slice": [7, 4]},
      {"path": "tb.u0.data[7:0]", "slice": [3, 0]},
      "tb.u0.u_mux.dout[3:0]",
      {"path": "tb.u0.u_mux.sel", "role": "evidence"}
    ]
  },
  "error_time": "1250ns",
  "first_deviation": "1210ns"
}
JSON
```

Mark signals and slices according to how the final report uses them, even when
the investigation ends without a code edit.

| Signal role | nWave color | When to use it |
|---|---|---|
| `evidence` | Cyan | A sampled value, transition or comparison supports the conclusion or rules out a competing explanation. Mark both sides of a comparison when both contributed. |
| `modified` | Yellow | The edit changes an assignment or a connection involving this signal or slice. |
| `context` | Viewer default | The row helps DV navigate the waveform. |

A plain signal or slice defaults to `context` in a function group and `modified`
in a `changes` group. Set `role` on a row to override that default. All occurrences
of the same instance path and slice share a color. Yellow takes priority when
an edited signal also supplies evidence. Include the color key in the report.

For each edit, add a `changes` entry with its filename, line and a short description.
These groups open at the top of the view. For a changed connection, include the
port and the old and new selections. For a changed case value, label the index
and value change, mark the observed selector as `evidence` and the assigned signal
as `modified`. Give an edit with no mapped signal an empty list and explain the
gap in the report. Omit `changes` when the investigation has no code edits.

Use exact FSDB declarations, including bus ranges. To display a packed slice,
supply its exact parent `path` and the two inclusive `slice` indices in HDL
declaration order. For example, `{"path":"tb.u.S[1:32]","slice":[6,9]}` displays
`S[6:9]`. A repeated index selects one bit. All rows show the selected FSDB's
values, including both ranges of a changed connection. For escaped HDL names,
supply the literal hierarchy components as a JSON array, such as
`["tb", "\\lane[0]", "\\net.with.dot"]`. Integer times are picoseconds.
Unit strings accept fs, ps, ns, us, ms and s down to one femtosecond. Prefer
the exact `first_divergence_time_fs` with an `fs` suffix when the comparison
proves the first difference. Omit an unknown time. At least one anchor is
required. The cursor lands on the reported error when available, and the
initial view includes all supplied anchors.

The command prints JSON containing the absolute `rc_path` and `fsdb`. Include
both in the report. Files are saved under `./debug/<issue>_YYYYMMDD_HHMMSS.rc`
using local generation time. Concurrent exports with the same name receive a
numeric suffix. If the export fails, report the reason. DV opens the indicated
FSDB in nWave, then uses **Restore Signal** to load the RC into that window.

## Notes

### From a verified difference to its upstream evidence

Use `diff_first_divergence` for a quick event comparison. Require `comparison_status`
plus `coverage_status` when interpreting equality, and `earliest_difference_proven`
when calling an observation the first difference. Supply `context_a` / `context_b`
to receive exact driver relays; follow their `status` and `prerequisite_calls`.

Use `trace_divergence` when the comparison needs repeated driver and temporal
queries. For each of `side_a` and `side_b`, supply its `wave_path`, `signal_path`
and `compile_log`, or use `kdb_path` with `top_hint` for a KDB-only design input.
Each side also accepts its own prepared `design_handle` or exact legacy hierarchy
identity. The sides may name the same run. Map distinct designs/scopes using
`signal_pairs` / `scope_pairs`.
`reference_side` labels the caller's intended reference, not which side is correct.
Default NPI priority and LSF placement match normal trace tools.

Inspect both sides' backend receipts and observations. Control differences are
investigation candidates; a hold depends on previous Q. A strict predecessor with
unresolved same-timestamp scheduling cannot establish the actual sampled value.
Unknowns, unavailable mappings, incomplete driver sets and budget limits remain
frontiers. Retain upstream input, local logic/control, and reference/mapping as
competing explanations. A local candidate is never an exclusive root-cause verdict.
Only `no_difference` denotes complete agreement in the declared comparison mode;
clock-mode agreement does not exclude between-edge transients.

Native-first inputs also support `kdb_path` + `top_hint` without a log. File
ownership and source fallback then report the missing log explicitly. Name/path
file filters and `compile_result`/`include_tree`/`filelist_tree` exports read only
reported metadata. `not_captured` is an evidence state, never proof of a failed
native query. The seven section exports retain their output and analysis caps.

Native and source stages share one finite `timeout_sec` (maximum 300 seconds).
Only confirmed load failures are remembered per KDB/top/execution/environment;
`retry_npi=true` retries one matching generation. Capabilities, truncation,
query failures and budget stops do not become persistent load failures.
Cancellation, identity/policy changes and exhausted native budgets do not start
a new backend. A trace changing artifact restarts from its original target and
resolved time/window, with cumulative history budgets. Divergence binds each
side independently; explicit historical tokens retain their strict recovery.
