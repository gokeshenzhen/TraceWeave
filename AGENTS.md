# Repository Guidance

## Domain Context and Authorized Scope

TraceWeave is a local EDA/RTL simulation-debugging project for semiconductor
designs and simulation artifacts that the user owns or is authorized to analyze.

`VCD` and `FSDB` are local digital simulation waveform databases.

In this repository, terms such as `scan`, `sweep`, `handshake`, `protocol`,
`interface`, `payload`, `trace`, `driver`, `load`, and `connectivity` refer to
digital-logic signals, bus behavior, waveform transitions, and elaborated-netlist
relationships.

`protected IP` means semiconductor intellectual property.
`narrow_condition_injection` is an RTL structural-analysis category for
control-logic conditions.

Repository tasks are limited to local source code, local waveform and log files,
test fixtures, and locally configured EDA tools. Do not access unrelated external
systems or resources.

For performance optimizations:

1. Establish a measurable baseline before making substantial implementation
   changes when practical.

2. Prefer preserving public MCP tool interfaces and output schemas. Intentional
   interface or schema changes are allowed when explicitly requested or when
   they provide a justified architectural benefit; update callers, tests,
   documentation, and compatibility handling accordingly.

3. Preserve FSDB thread-safety and observable cancellation and timeout
   behavior. Changes to locking, scheduling, worker, or execution models are
   allowed when justified by evidence and covered by focused regression tests.

4. Add regression tests appropriate to the behavioral risk of the change, and
   use reproducible before/after benchmarks for performance-sensitive changes.

5. Report the tested workload, benchmark conditions, measured results, memory
   impact when relevant, and any behavioral or compatibility trade-offs before
   claiming a performance improvement.

## TraceWeave Usage

When the task involves simulation logs or waveforms (VCS/Xcelium logs, FSDB/VCD), the default toolchain is:

`get_sim_paths -> build_tb_hierarchy + scan_structural_risks -> parse_sim_log -> sweep_handshakes -> recommend_failure_debug_next_steps`

Rules:

- `build_tb_hierarchy` and `scan_structural_risks` must run in parallel on the same `compile_log`
- `scan_structural_risks` should not be skipped by default
- It may only be skipped if the user explicitly asks to skip it
- `sweep_handshakes` is the runtime-layer counterpart of `scan_structural_risks`:
  a default-flow protocol-health scan. Run it after `parse_sim_log` whenever a
  waveform exists and the run failed; like the structural scan it should not be
  skipped by default (skip only if there is no waveform or the user asks). It
  returns a per-interface stall/deadlock/payload-hold fact table over every AHB
  and valid/ready interface — facts the LLM judges, not a verdict. A
  scoreboard/data-compare failure is frequently a protocol symptom, so this is
  the cheap one-call check before reading RTL line-by-line. Always read
  `coverage_status`: `zero_coverage` means no protocol interfaces were checked
  and is not a pass; `truncated`/`degraded` means partial coverage, so
  `flagged_count=0` is not a clean-protocol conclusion. When `coverage_status`
  is `truncated`, the result includes `suggested_next_actions` with a one-click
  retry; follow it to complete coverage. The compact `finding_summary` (by_flag,
  by_channel_hint, top_scopes) surfaces which channels have findings before
  opening the full interface list. **Critical**: do not collapse global findings
  from the sweep + targeted clean checks on one interface into a false "protocol
  is clean" verdict. State both facts: "global findings exist on W-channel,
  targeted Master0 R-channel checks are clean" → points to next layer (HVL/BFM).
  FSDB transition reads are bounded by the native output buffer; if any clock or
  sampled signal returns only a prefix, the row sets
  `transition_data_truncated=true`, the sweep increments
  `transition_truncated_count`, and coverage cannot be `complete`. Narrow the
  time window for a complete targeted check; never treat zero counts from a
  truncated prefix as clean.
- Do not analyze or recommend fixes before MCP output is available
- On protocol or scoreboard mismatches, carry at least two competing hypotheses
  and verify the opposite side with waveform evidence before assigning root
  cause; state which sides were checked. See `docs/workflow.md`.

## Debug Discipline

`docs/debug-discipline.md` is the module-type-agnostic debug discipline — a
reusable, copy-paste prompt for any failure (protocol/bus, datapath/algorithm,
or control/FSM), not only protocol mismatches. Follow it before assigning a root
cause. Its through-line: good discipline, not more tool output, is what turns
correct perception into a correct root cause — so ground every claim in a
trace/log fact, backtrace the symptom to its originating driver, respect a
tool's objective exclusions, keep two hypotheses alive and check the opposite
side, and label uncertainty honestly. The doc holds the canonical five rules,
maps each to the relevant TraceWeave tools, and specializes "the opposite side"
per module type; the protocol-specific form of the same discipline lives in
`docs/workflow.md`.

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
30. `src/cancellation.py`
31. `src/operation_metrics.py`

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
- `tests/test_server_concurrency.py`
- `tests/test_diagnostic_snapshot.py`
- `tests/test_operation_metrics.py`

## Repository Focus

- `server.py` is the composition root and MCP entry point.
- `src/cancellation.py` + `server._run_in_wave_thread`: wave-touching tool bodies (signal queries, search, summary, x-trace, period/diff, suggest/sweep/inspect, verify_window, reconstruct_transactions) are synchronous CPU-bound scans, so `_dispatch` offloads them to a worker thread — the event loop stays free (no head-of-line blocking of light calls behind a heavy sweep) and client cancellation can actually be delivered. Parser access is serialized by wave locks acquired INSIDE the worker: one global lock for ALL FSDB work (the Verdi ffr API makes no thread-safety promise even across handles), a per-path lock for VCD; `diff_first_divergence` takes both paths' locks in a stable global order (no deadlock). Cancellation is cooperative: cancelling the request task (client `notifications/cancelled` or disconnect) arms a per-call `threading.Event`, and the scan loops in `cycle_query`/`handshake_sweep`/`verify_condition`/`window_verify`/`txn_reconstruct` hit `check_cancelled()` stride checkpoints (`CANCEL_CHECK_STRIDE`). Handshake discovery checks before/after every `search_signals` call and between valid/ready and AHB phases; it explicitly re-raises `OperationCancelled` instead of swallowing it in best-effort search handling. A synchronous native search is not interruptible inside that call yet, so cancellation is observed immediately after it returns. A call cancelled while still queued on a wave lock gives up without ever touching the parser. Because a client-side read timeout may not emit `notifications/cancelled`, an interactive FSDB call queued behind background `sweep_handshakes` also arms the sweep's cancel event; the sweep releases the global lock at its next checkpoint, so the light query proceeds without ever overlapping FFR access. `src/operation_metrics.py` records only whitelisted numeric timings/counts and a fixed phase label (lock wait, discovery/search costs, preemption-to-cancel latency); it never records paths, scopes, keywords, signal names, or values. Loop-side dispatch state (`_result_cache`/`_session_state`/provenance) is still written only on the event-loop thread — the worker computes, the loop remains the single writer. Regression coverage: `tests/test_server_concurrency.py`.
- Full-sweep operation metrics add only privacy-safe aggregates: total sweep
  time, planned/attempted/completed interface counts, unique clock/signal
  counts, aggregate/max inspect time, clock-vs-signal transition read
  count/total/max, edge-extraction/value-sampling time, and
  transition-truncated interface count. FSDB sweeps also expose aggregate
  native phase timings, group/fallback counts, transition/output volume,
  sampling shape, cache peaks, result build/serialization cost, and process
  RSS start/peak/end. They remain numeric/fixed-label only.
  They never record paths, scopes, signal names, search keywords, or values.
- `src/path_discovery.py` owns compile/sim/wave path discovery.
- `src/compile_log_parser.py` and `src/tb_hierarchy_builder.py` drive compile-log-based hierarchy extraction.
- `src/analyzer.py` and `src/log_parser.py` contain the core failure analysis logic.
- `src/signal_driver.py` backtracks RTL drivers from waveform signal paths.
- `src/signal_load.py` resolves load/fanout for a signal — the symmetric counterpart to `signal_driver`.
- `src/connectivity_backend.py` defines the `ConnectivityBackend` protocol; `select_backend()` returns the Verdi NPI backend when a KDB is found, otherwise Static. NPI failures degrade transparently; the dispatch layer never sees verdi-specific exceptions.
- `src/verdi_backend.py` probes for Verdi KDB / license environment; emits per-simulator `kdb_hint` when KDB is missing.
- `src/verdi_npi_backend.py` is the NPI-backed implementation of `find_driver` / `find_loads` / `find_path`, plus `collect_instance_src_map` used by `build_tb_hierarchy` to overlay elaborated-netlist `file:line` onto compile-log-derived hierarchy nodes. Lazily loads `pynpi` from `$VERDI_HOME` and caches loaded designs across calls. Uses `NetHdl.fan_in_reg_list` to walk the elaborated netlist across instance boundaries — this is why NPI can resolve drivers that Static source-regex cannot reach. `find_path` wraps `netlist.sig_to_sig_conn_list`; Static has no equivalent and returns `unsupported_reason="static_backend_no_path_api"` (honest no-op rather than a regex approximation). **Driver-vs-loads cross-check (TB-driver misattribution guard):** when NPI's `find_driver` would report a "driver" whose raw NPI identity (modulo bit-indexing, via `_norm_raw`/`driver_is_load_alias`) is byte-identical to a LOAD of the *same* net — an interface-slice alias of the net's own consumer, or a register that reads the net — that "driver" cannot be the source (a net cannot be both driven by and read into the same pin). It then prefers a genuine RTL driver among the remaining candidates (`_loadcheck_head`), and if none exists returns an honest no-op `driver_status="testbench_driven"` + `cross_check.conflict` receipt + `unsupported_reason="driver_is_load_real_driver_is_testbench"` instead of naming the load as an `exact` driver. This kills the failure where NPI's register fan-in, unable to see a procedural UVM driver (virtual interface + clocking block), walked the net to a nearby LOAD register inside the DUT (e.g. an AHB matrix `lock_owner`) and confidently mislabeled it the driver of a master's HTRANS — pushing the model toward the interconnect instead of the TB master driver. It is FP-safe by construction: the discriminator is *byte-identical* driver==load (an aliasing artifact), so a legitimate self-referential counter (`q <= q + 1`, whose net loads into a distinct `Add`/`Assignment` cell, not the `Reg`) never matches; an initial-value block is not treated as a genuine runtime driver (`_is_genuine_runtime_driver`). **The decision is keyed on NPI's *original* `driver_list` (what it claims drives the net) and short-circuits BEFORE fan-in — this is what covers `recursive=True`. Keying it on the fan-in result instead would miss the misattribution: under `recursive=True` fan-in walks across the boundary to a downstream LOAD register (the AHB matrix `lock_owner` that merely READS the net), which lives in the net's fan-OUT, not its `load_list`, so the comparison would not match. And widening the load set to include fan-OUT is wrong — a self-counter's own `Reg` IS in its fan-out (the feedback), so that would false-positive.** Reads the net's own `load_list()` (no second dispatch). The contradiction logic is pure and unit-tested without a live KDB (incl. the `recursive=True` shape).
- `src/kdb_builder.py` provides the `build_kdb` MCP tool: when a Verdi KDB is missing (typical for Xcelium / `xrun` flows), it runs `vericom -kdb` + `elabcom -elab kdb` against the file list parsed from the compile log, caches the result under `$TRACEWEAVE_CACHE_DIR/kdb/<hash>/`, and writes a runnable `build.sh` reproducer. The probe in `verdi_backend.py` picks up the cache transparently as `kdb_flow: "traceweave_cached"`. Default-on; opt out with `TRACEWEAVE_AUTO_KDB=0`.
- `src/waveform_batch.py` exposes `WaveformBatchReader` for time-window multi-signal reads, with FSDB and VCD implementations sharing one shape.
- `src/structural_scanner.py` and `src/x_trace.py` are first-class analysis capabilities.
- `src/cycle_query.py` provides cycle-aligned signal sampling. `get_signals_by_cycle` slices by cycle index (capped); `sample_signals_on_edges` samples every clock edge inside a *time window* (the shared substrate for window-scoped relational analysis like `inspect_handshake`). Both reuse one private edge-sampling core. A full sweep may pass a private `EdgeSamplingSession`: all interfaces in one clock group reuse the clock transition list, extracted edges, and sample-time vector; only signals with multiple consumers are cached, and a remaining-consumer count evicts each cached signal immediately after its last use. Sessions are consumed one clock group at a time so large transition lists do not accumulate across clocks. This internal path preserves standalone sampler behavior, cancellation checkpoints, transition-truncation propagation, and public schemas. It also provides `annotate_center_transients`: a pure post-process over a `get_signals_around_time` result that flags a `value_at_center` which is a **sub-cycle transient** (the unmistakable dip-and-return `X→glitch→X` signature of a combinational mux re-settling to idle for ~1ns at the clock edge), setting `transient_note` + per-signal `center_transient`/`center_settles_to`/`center_settle_ps`. `server.call_tool` runs it on every `get_signals_around_time` result so a point sample at the edge is not misread as the settled protocol value (the failure mode that led a model to blame an interconnect mux for a 1ns glitch). Zero-FP: only the dip-and-return pattern is flagged.
- `src/schemas.py` is the single source of truth for tool output contracts.
- `src/problem_hints.py` provides lightweight failure symptom annotations.
- `src/hierarchy_handles.py` owns the in-process `HandleStore` and content-addressed handle derivation. `build_tb_hierarchy` returns a slim payload + `hierarchy_handle`; the full hierarchy is registered here and resolved by the handle tools. Handles are not persisted — server restart drops them.
- `src/handle_tools.py` implements `get_tb_subtree`, `lookup_tb_files`, `find_tb_instance`, `get_tb_file_detail`, `get_tb_class_hierarchy`, `dump_tb_section` as pure functions over a resolved full hierarchy dict. `lookup_tb_files` requires at least one filter; `get_tb_file_detail` returns `did_you_mean` basename suggestions when the path is not in the compile set (multi-version safety net).
- `src/fsdb_parser.py` and `fsdb_wrapper.cpp` define the Python/native FSDB boundary. FSDB tags are **tick counts, not picoseconds** (real time = tick x header scale, read at `fsdb_open` via `ffrGetScaleUnit()`, e.g. `100fs`); every tick<->ps conversion goes through exactly two helpers in `fsdb_wrapper.cpp` (`_ToTag` floor on input / `_TagToPs` ceil on output, integer-fs internal base so sub-ps scales lose no precision) — never hand-roll a `<<32|` time conversion. An unreadable scale refuses time-based queries (`FSDB_ERR_SCALE_UNKNOWN`) instead of assuming 1ps; `get_waveform_summary` exposes `scale_unit`/`scale_fs_per_tick` as the self-check. For full sweeps, the optional transition-group ABI adds resolved signals and calls `ffrLoadSignals()` once per bounded clock group, reads every signal independently through the existing reusable 64 MiB per-call buffer, and unloads in `finally`; the default group cap is 16 (`TRACEWEAVE_FSDB_GROUP_MAX_SIGNALS`, clamped 1..256). Oversized groups, begin errors, and old wrappers fall back to legacy per-signal loading. Cancellation and exceptions must still unload, and no native group may outlive the process-global FSDB lock. Cross-scale regression lives in `tests/test_fsdb_timescale.py` (fixtures `scale_100fs.fsdb`/`scale_1ns.fsdb`).
- `src/cursor_store.py` owns the in-process `CursorStore` — named, process-scoped time anchors (`cursor_set`/`cursor_list`/`cursor_delete`). Same lifetime semantics as `HandleStore`: not persisted, dropped on restart, no "active cursor" (references are always explicit `@name`).
- `src/timespec.py` resolves a TimeSpec (raw ps int, `@cursor` ref, or unit literal like `12.34ns`) to picoseconds. `server._resolve_time` wires it into every time-taking tool input (`get_signal_at_time`, `get_signal_transitions`, `get_signals_around_time`, `trace_x_source`, `diff_first_divergence`, `period`). Arithmetic (`@c ± cycle(clk)`) is intentionally NOT implemented yet (reserved for a future Lark grammar).
- `src/verify_condition.py` holds the auto-debug verification primitives: `diff_first_divergence` (first time two waveform signals differ; cross-run or within-run), `period` (median edge period + first off-beat), and `inspect_handshake` (cycle-by-cycle valid/ready classification: stalls, long-stall windows, backpressure imbalance, payload-hold violations during a stall, and **premature valid deassertion** — a stalled beat whose valid/htrans goes inactive the next edge before ready/HREADY arrives, i.e. the master dropping the transfer instead of waiting (the AHB master-not-waiting-for-HREADY bug); payload-hold structurally cannot see it because a 1-cycle stall (`max_stall_cycles==1`) leaves no room for payload to change and htrans/the derived valid is not a payload signal, and the `check_valid_hold` check (default on) needs no payload — the protocol-timing class of bug that leaves no value pattern in scoreboard logs; protocol-agnostic over AXI/valid-ready/credit; for AHB pass `valid_htrans` + `htrans_rule` to derive valid from htrans since there is no literal valid signal, echoed as `valid_source`). For an AHB interface (whose payload is address-phase control only) a third check, **`x_while_valid`**, flags a control field that is x/z at a known-asserted-valid edge — an active transfer carrying an unknown address/control field; it stays OFF for a literal-`valid` interface whose payload may be legally-x data lanes (a false positive). A separate **write data-phase hold** check (pass `hwrite` + `write_data`=HWDATA) verifies HWDATA stays stable through a write data-phase wait state (HREADY low) — a `write_data_hold_violation`; this is the data-phase window, one cycle behind the address-phase valid, that the htrans-keyed payload-hold structurally cannot see (and exactly why HWDATA is excluded from `payload`). It is sound ONLY on the producer (initiator/master) interface, where HWDATA is a single-source driven output; `suggest_protocol_bundles` therefore attaches `hwrite`/`write_data` only to initiator-side bundles (a responder interface's HWDATA is an interconnect-mux output that glitches at the clock edge → FP). On AHB the result also carries a `protocol_semantics` receipt naming which of its own metrics are faithful vs suppressed (valid-hold faithful; `ready_without_valid` is idle-bus, not a violation; payload-hold address-phase only), and the premature-deassertion finding carries `accepted_before_deassert=False` — the witness that the dropped beat was never accepted — so a true positive cannot be waved away as AHB pipeline overlap. `inspect_handshake.coverage` reports only checks the tool actually ran (`stall_checked`, `backpressure_checked`, `payload_hold_checked`/partial, `valid_hold_checked`, `x_while_valid_checked`), never protocol side. All three auto-register a cursor (`inspect_handshake` anchors x-while-valid > hold-violation > premature-deassertion > long-stall > longest-stall) and read existing waveforms only. On a finding `inspect_handshake` sets `violating_signal` (the held signal for a payload-hold, the x'd control field for an x-while-valid, the valid/htrans for a premature deassertion; `null` for a plain stall) + a `next_actions` link to `explain_signal_driver`. For the **one-sided** violations (x-while-valid, payload-hold, premature deassertion) it ALSO returns a structured `attribution` block (`violating_side=valid_driver`, `exonerated_side=ready_driver`): both breach the valid-driver's obligation (payload travels with valid; only the producer can mutate payload mid-stall or drop valid before acceptance), so the responder/ready side cannot cause them — do NOT start in the slave driver/monitor. This is protocol role, NOT trace-ownership (the trace still holds values, not ownership): the valid-driver is the channel producer — master on AXI AW/AR/W, slave on R/B; AHB htrans is always master — so `explain_signal_driver` on `valid` resolves the actual instance, or returns `driver_status="testbench_driven"` when the producer is a UVM/TB driver NPI cannot see (do NOT read a landed DUT register as a mis-wire). A plain stall is genuinely two-sided → `attribution` empty, link targets `ready` (slave back-pressure vs the master's valid); attribution = bus-fact + drive-direction, composed by the LLM. It also contains `diff_value_distribution` (multi-sample fail/pass differential), which is implemented and tested but **deliberately not registered as an MCP tool** — evaluation showed no clear benefit over baseline on the common scoreboard-data-mismatch flow, so it is kept off the tool surface; re-register in `server.py` if a real use-case appears.
- `src/window_verify.py` provides `verify_window` (P3, the "small templated version" of the propose-and-verify engine): the LLM states a temporal predicate and the tool returns a precise `holds` verdict + a concrete witness/counterexample (cycle + sampled `signal_values`) over a clock window. Deliberately **templates, not a DSL** — a *term* is `{signal, op, value}` (op: eq/ne/gt/ge/lt/le/is_x/is_known), a *predicate* is a term list = implicit AND (no OR/nesting; run two calls for OR), and five *modes*: `always`/`never`/`eventually`/`implication` (A |-> B within N cycles), and `sequence` (the per-accepted-beat increment of one signal — address-stride checks like AHB haddr +stride). **`implication` has an `overlap` flag** (default `True` = overlapping `|->`, response window `[i, i+N]` includes A's own cycle; `False` = non-overlapping `|=>`, window `[i+1, i+N]` starts the NEXT cycle). `overlap=false` is the shape for a **stability/hold** property — "B must STILL hold the next cycle", e.g. AHB `HTRANS`/valid held through a wait state — where A already implies B on its own cycle (`(htrans==2 && hready==0) |=> htrans==2` proves premature-deassertion). With `overlap=true` such a property is a **vacuous pass**: every antecedent satisfies the consequent on its own cycle, the window never matters, and a `holds=true` proves nothing about a later cycle — the tool flags this as `result.vacuous=true` + a loud `VACUOUS PASS` warning steering to `overlap=false` (keyed on every antecedent satisfied same-cycle, so an inconclusive-at-end pass is never mislabeled). This closes a real trap: a weak model could read the vacuous `holds=true` as exclusion evidence. `overlap=false` requires `within_cycles>=1`. In `sequence`, `predicate` is the accepted-beat gate and `delta`=`{signal,value,op?,modulo?,restart_when?}`: `modulo` (= WRAP region bytes = size×len) absorbs the wrap-around so a legal WRAP beat is not a violation, and `restart_when` (e.g. htrans==NONSEQ) re-seeds at burst starts so cross-burst jumps are not flagged — both supplied by the LLM so the tool stays burst-decode-free (anti-thick; WRAP/boundary math is the caller's, not a per-protocol decoder). Faithful for `sequence`: first/restart beat seeds only (never a violation), a wait-state (gate-false) keeps the predecessor, any x/z on gate or tracked signal breaks continuity (counted unknown, never a delta across an unknown). inspect_handshake/period/diff are conceptually special cases of this; the point is future analysis composes a predicate rather than spawning another bespoke tool. Faithful: x/z cycles → `unknown_cycles` (never silently passed), an implication whose response window runs past end-of-trace → `inconclusive_count` (never silently failed), unresolved signals are loud. Reuses `sample_signals_on_edges` + `_resolve_signal_path`; registers one cursor at the evidence. On a `sequence` violation it sets `violating_signal` + a `next_actions` link to `explain_signal_driver` (bus facts do not self-attribute master/slave — attribution = bus-fact + drive-direction, composed by the LLM). Decoupled from auto-hypothesis on purpose (hypothesis generation stays the LLM's job — do NOT bolt it into recommend_next_steps).
- `src/handshake_suggest.py` provides `suggest_handshakes` (T2 of the protocol-debug plan, the "self-serve multiplier"): scans the waveform's signal universe via `search_signals` and proposes ready-to-use `inspect_handshake` bundles — pairs `*valid`/`*ready` by scope + stem, locates the clock (same scope or nearest ancestor), and groups channel payload buses (width>1, non-bookkeeping var_types, preferring the channel stem prefix). It also provides `suggest_protocol_bundles` for AHB/APB discovery: AHB returns `valid_htrans`-based `inspect_handshake` args (payload = address-phase control only — HADDR/HWRITE/HSIZE/HBURST/HPROT; HWDATA/HRDATA are excluded as data-phase, so payload-hold cannot false-positive on the address/data phase offset — HWDATA is instead surfaced as `hwrite`+`write_data` for the write data-phase hold check, but ONLY on a mechanically-confirmed initiator-side interface: on a responder/consumer interface HWDATA is a combinational interconnect-mux output that glitches to its idle value for ~1 cycle at each clock edge, which the edge sampler reads as a spurious change, so hwrite/write_data are withheld on responder/unknown interfaces to keep the check zero-FP); APB returns `psel`/`penable`/`pready` facts and marks the missing derived-valid step. AHB results also carry a `next_step` field with a copy-paste-ready `inspect_handshake(...)` call per candidate (via `_inspect_handshake_relay`) — discovery only LOCATES the interface, `inspect_handshake` is the analysis; weak models stop at discovery unless the next call is spelled out at the one point its args first exist (here, not at parse time — parse has no signal paths). Direction tags are emitted only from discovery-layer mechanical evidence and degrade to `unknown` rather than guessing. Core proposal functions are pure over `{path,name,width,var_type}` descriptors (fully unit-tested).
- `src/handshake_sweep.py` provides `sweep_handshakes` (P1 of the enhancement roadmap): a whole-design handshake **anomaly sweep**. Discovers every valid/ready interface (via `suggest_handshakes`) **AND every AHB interface** (via `suggest_protocol_bundles`, htrans-derived valid), runs `inspect_handshake` over each, and returns a **comparative fact table** (`SweptInterface` rows: per-interface stall/deadlock/payload-hold/premature-valid-deassertion/backpressure facts + factual `flags`, plus a `kind` of `valid_ready` or `ahb` — for an `ahb` row `valid` is the HTRANS path) ordered by a transparent mechanical key (ended_in_stall > x_while_valid_violations > payload_hold_violations > write_data_hold_violations > valid_deassert_violations > max_stall_cycles > ready_without_valid; for an `ahb` row `ready_without_valid` is suppressed from `flags` and carries no sort weight — it is idle-bus, HREADY high while HTRANS idle, not backpressure). Running both discovery families is safe (each is empty on a design that lacks it); APB is deliberately excluded (no inspect_handshake args — needs a derived `psel&&penable` valid). Clocking-block scopes (`*_cb`: mdrv_cb/smon_cb…) are dropped from discovery — they are TB sampling mirrors of a parent interface with no own clock, so they would only inflate `discovered_count` or land in `skipped` and pull coverage to `degraded`. For execution efficiency, normalized bundles are grouped by their discovered clock path; each session binds that path to one consistent parser-resolved spelling. One bounded `EdgeSamplingSession` per group reuses clock transitions/edges/sample times and refcount-caches only cross-interface shared signals; the FSDB parser may keep that group's bounded native load session open across its interfaces. Public inputs/results and full-design coverage are unchanged; FSDB work remains under the existing process-global lock and cancellation is checked before cache reuse and throughout sampling. This is the one-call entry the `protocol_symptom_hint` now steers toward, collapsing the multi-hop suggest→inspect chain into a single fan-out. It is a context/round-trip reducer for the M-way fan-out case, NOT a root-cause verdict — every raw fact is exposed so the LLM re-ranks (perception-vs-judgment boundary). On a backpressured pipeline the sort surfaces the propagation *front*, not the root; the root is the stall→starvation boundary, which the LLM derives from the facts. Registers exactly ONE cursor at the top interface's stall begin; no-clock bundles → `skipped`; discovery beyond `max_interfaces` (default 64) sets `coverage_status=truncated` with a loud note. Empty discovery sets `coverage_status=zero_coverage` and explicitly says no protocol interfaces were checked; skipped rows set `coverage_status=degraded`. `recommend_failure_debug_next_steps` and `get_diagnostic_snapshot` treat non-`complete` coverage as an incomplete default-flow protocol scan. Relies on `inspect_handshake`'s `ended_in_stall`/`final_stall_cycles`. Hidden under the same A/B toggle as inspect/suggest.
- A bounded FSDB transition prefix sets per-row
  `transition_data_truncated=true`, increments top-level
  `transition_truncated_count`, and forces non-`complete` sweep coverage.
- `src/txn_reconstruct.py` provides `reconstruct_transactions` (the id-correlated transaction layer): walks a request handshake channel + a completion channel over the whole window, matches accepted beats by an `id` field, and returns per-transaction latency + aggregate facts. **One generic core, not a tool per protocol**: AXI read = AR→R (`cmp_last`=rlast, id=arid/rid); AXI write = AW→B (id=awid/bid) PLUS an optional unindexed W-data channel (`data_valid`/`data_ready`/`data_last` + `data_fields`; W carries no id so beats attach in order to the oldest data-incomplete request, matching real interconnect); any id'd req/resp; CHI-like. `req_id`/`cmp_id` are optional — omit both for an unindexed in-order stream (AXI-Lite, APB), which pairs requests and completions in FIFO order and reports txn id as null. AHB/APB phase tracking is otherwise out of scope. An optional `reset` (`reset_active_low`) clears in-flight state so a txn straddling reset is not a phantom hang (correctness, emits `reset_clears`); `capture_beats` (off by default → only `beat_count`) returns per-beat `data_beats[]` for data-integrity debug. An optional `req_len` (AxLEN = arlen/awlen) checks each txn's observed `beat_count` against `req_len+1`: a mismatch (early/late LAST, dropped/extra beat) is a real burst-length violation surfaced per-txn (`expected_beats`, `beat_count_mismatch`) and as `beat_count_mismatch_count` (x/z len → not checked, never a FP; with no `req_len` the count is 0 = "not checked", not a clean verdict). Facts not verdict: `latency` distribution (min/median/max/mean) not an "outlier" label; `outstanding_at_end`/`max_outstanding`/`max_outstanding_per_id`; `reorder_count` (informational, legal in AXI); `timeout_cycles`→`slow_count`; unmatched req/cmp surfaced loudly (the hang signature); one cursor (first never-completed request > peak outstanding). Out-of-order completion across ids via per-id FIFO. Returns objective facts only and leaves protocol-semantic interpretation (response-code decode, burst-type decode, outlier judgement) to the caller. Reuses `sample_signals_on_edges` + `_resolve_signal_path` + `_hs_truth`/`_hs_repr`.
- `src/usage_telemetry.py` provides passive, local-only usage telemetry: when explicitly enabled, `server.call_tool` (the single dispatch choke point) appends one JSONL line per call to `$TRACEWEAVE_CACHE_DIR/telemetry/usage.jsonl` (tool name, arg keys + whitelisted scalar flags only — never values/paths, `result_bytes` token proxy, latency, ok/blocked, and on failed calls a classification `error_code` — a code or exception class name, never the message). Long wave calls may include the privacy-safe `operation_metrics` diagnostics listed above. A session is anchored to each `get_sim_paths` case identity via `note_session`. Recording is best-effort (never raises into the call path). `aggregate()` is a pure function backing the offline `scripts/telemetry_report.py` CLI; it is NOT an MCP tool. Default-off; opt in with `TRACEWEAVE_TELEMETRY=1`. Exists to quantify how often the auto-debug v2 primitives (cursor/period/diff_first_divergence) actually get used before building more.
- `config.py` centralizes environment-sensitive paths and behavior constants.

## Working Rule

Before making non-trivial changes, build a quick mental model from the files above instead of editing from local assumptions.

## Documentation Rule

When a behavior change requires doc updates, **only touch documents tracked in
git**. Run `git ls-files | grep -E '\.md$'` to see the canonical doc set
(currently `README.md`, `README.zh.md`, `AGENTS.md`, `CLAUDE.md`,
`docs/architecture.md`, `docs/workflow.md`). Untracked files under `docs/` are
local drafts, RFCs, and session notes — do not edit them as part of code changes
and do not create new ones unless the user explicitly asks. This applies to
every agent working in this repository (Claude, Codex, others).
