# Waveform MCP — Standard Debug Workflow

## Overview

This document defines the recommended tool invocation order for the Waveform MCP server. It is intended to be used as the basis for `server.py` instructions, guiding the AI agent through a structured debug flow.

## Workflow

```
User: "Help me debug /path/to/verif, case0"
│
▼
Step 1: get_sim_paths(verif_root, case_name?)
│  Discover all relevant file paths automatically.
│  Returns: discovery_mode, case_dir, compile_logs (with phase tag),
│           sim_logs, wave_files, simulator (auto-detected),
│           fsdb_runtime, hints, available_cases
│
│  Key decisions:
│  - If discovery_mode == "unknown" → stop guessing and follow hints
│  - If case_name omitted in root_dir mode → check available_cases, ask user to pick one
│  - If hints contain warnings (empty log, missing wave) → inform user early
│  - Pick compile_log with phase="elaborate" for step 2
│  - Store simulator for all subsequent tool calls
│  - If fsdb_runtime.enabled is false → ignore `.fsdb` when `.vcd` is available
│  - Only proceed to step 3 when `sim_logs` is non-empty
│
▼
Step 2: build_tb_hierarchy(compile_log, simulator)
│  Build project-level understanding BEFORE analyzing errors.
│  Returns a SLIM payload (full data is server-cached behind `hierarchy_handle`):
│    - project: top_module, source_root, simulator
│    - stats: file_count, module_count, instance_count, tree_depth,
│             class_count, interface_count, uvm_file_count
│    - tree_skeleton: component_tree truncated to depth 2 with `child_count`
│      and `truncated` flags on each node
│    - interfaces: full list (small)
│    - ambiguous_basenames: collisions like xxx_v1.v vs xxx_v2.v — when
│      non-empty, MUST disambiguate with `lookup_tb_files(basename=...)`
│    - hierarchy_handle: pass to every handle tool
│    - handle_tools: name map of the six handle tools
│
│  What the agent does next, on demand (via handle tools):
│  - Drill into a branch     → get_tb_subtree(handle, root="top.x.y", depth=N)
│  - Find a specific file    → lookup_tb_files(handle, basename=...) or
│                              path_contains=... / has_module=... / contains_uvm=...
│  - Locate an instance      → find_tb_instance(handle, path=...) or module=...
│  - Read a file's symbols   → get_tb_file_detail(handle, path=...)
│  - UVM class tree          → get_tb_class_hierarchy(handle, root_class=...)
│  - Raw section (heavy)     → dump_tb_section(handle, section=...)
│
│  Before reading any RTL source file, call get_tb_file_detail(path=...) or
│  lookup_tb_files(...) first. The compile_log is the only source of truth
│  for which file version was compiled in this session.
│
▼
Step 3: parse_sim_log(log_path, simulator)
│  Get grouped error summary from simulation log.
│  Returns: groups list, normalized failure_events, time normalization fields,
│           rerun hints such as previous_log_detected / candidate_previous_logs,
│           and log_snapshot_id for same-path rerun diffing
│
│  What the agent does:
│  - Identify the earliest and most frequent error groups
│  - Prefer `failure_events[0].time_ps` as the waveform time anchor when present
│  - Cross-reference error signatures with step 2's hierarchy
│    (e.g., UVM_ERROR [SCOREBOARD_MISMATCH] → find_tb_instance(module="scoreboard")
│     or get_tb_subtree drilling from tree_skeleton)
│  - Decide which group to investigate first (usually group_index=0)
│  - If previous_log_detected == true, consider diff_sim_failure_results early
│  - If the simulator overwrites the same log path on rerun, call
│    diff_sim_failure_results with snapshot IDs or only new_log_path; the
│    previous parsed snapshot becomes the baseline
│
▼
Step 4: recommend_failure_debug_next_steps(log_path, wave_path, simulator, ...)
│  Get a strong default failure target and role-ranked signal suggestions.
│  Returns: primary_failure_target, recommended_signals, recommended_instances,
│           suspected_failure_class, recommendation_strategy, failure_window_center_ps
│
│  What the agent does:
│  - Use the top recommended signals first instead of blind substring search
│  - Prefer signals with useful role/reason_codes (state/counter/handshake/etc.)
│  - If the recommendation is weak, fall back to explicit search_signals
│
▼
Step 5: search_signals(wave_path, keyword)
│  Confirm full hierarchical paths for signals relevant to the error.
│  Returns: matching signals with bit width, `direction`, and `var_type`.
│    - `direction`: input/output/inout/implicit (FSDB only). VCD always null.
│    - `var_type` : wire/reg/integer/real/parameter/memory/...
│    Clients filter ports/nets/variables in a scope by combining a hierarchical
│    keyword (the scope prefix) with these fields, instead of a dedicated tool.
│  Note: `.fsdb` wave paths are usable only when fsdb_runtime.enabled is true.
│        Port-direction filtering requires FSDB — VCD cannot encode direction.
│
│  How the agent picks keywords:
│  - From step 2's tree_skeleton or get_tb_subtree: module instance names → signal names
│  - From step 3's error message: signal names mentioned in assertions or checkers
│  - From RTL source code: verify path via get_tb_file_detail first, then read
│
│  May need multiple calls with different keywords.
│
▼
Step 6: analyze_failures(log_path, wave_path, signal_paths, simulator)
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
Step 7: Deep dive (on demand, based on step 6 findings)
   │
   ├─ analyze_failure_event(log_path, wave_path, simulator, failure_event, ...)
   │    When: Want failure-centric instance/source correlation
   │    Output: time_anchor, likely_instances, recommended_signals, related_source_files
   │
   ├─ get_error_context(log_path, line)
   │    When: Need to inspect other error groups beyond the one in step 5
   │    Input: first_line from a different group in step 3's results
   │
   ├─ explain_signal_driver(signal_path, wave_path, compile_log, top_hint?)
   │    When: Waveform shows a suspicious signal and the agent needs the likely RTL driver
   │    Output: driver_status, driver_kind, source_file, source_line, expression_summary
   │    Notes: For deeper / cross-hierarchy traces a Verdi KDB enables the NPI
   │           backend, which can cross instance port boundaries. If the
   │           simulator is Xcelium and no KDB exists yet, get_diagnostic_snapshot
   │           lists `build_kdb` in `missing_steps` — call that first.
   │
   ├─ find_signal_loads(signal_path, compile_log, kind_filter?, include_expr?)
   │    When: Symmetric to explain_signal_driver — list the consumers (fanout)
   │          of a signal: child instance input ports, RHS in assigns /
   │          procedural assignments, always-block sensitivity lists.
   │    Output: loads[].{load_path, kind, source_file, source_line, expr}
   │    Notes: Static backend is shallow_only and cannot follow interface
   │           positional bindings or cross-hierarchy fanout — those are
   │           surfaced via stopped_at. NPI backend (when a Verdi KDB is
   │           present) walks the elaborated netlist and resolves both.
   │           Each load carries source_info_origin = "compile_log" or "npi".
   │
   ├─ trace_x_source(signal_path, wave_path, compile_log, time_ps, max_depth?)
   │    When: A signal is X/Z at the failure time and the agent wants to
   │          trace propagation back to the root cause net.
   │    Output: propagation_chain[], root_cause, trace_status, analysis_guide
   │    Notes: Combines waveform reads (per-hop value at time_ps) with
   │           source-level driver analysis. Uses fan-in via NPI when KDB
   │           is available; otherwise source-regex.
   │
   ├─ trace_signal_path(from_signal, to_signal, compile_log, expand_assigns?)
   │    When: Need a connected chain of nets between two signals (e.g. "how does
   │          an input port reach the failing assertion?"). NPI-only — without
   │          a KDB this returns unsupported_reason="static_backend_no_path_api"
   │          and you should fall back to explain_signal_driver + find_signal_loads.
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
   │    Notes: Cache lives under $TRACEWEAVE_CACHE_DIR/kdb/<hash>/. After a
   │           successful build, subsequent driver/load queries automatically
   │           route through NPI via the cached KDB. VCS users get a cheaper
   │           path: recompile with `-kdb=only` (suggested by kdb_hint).
   │
   ├─ get_signal_transitions(wave_path, signal_path, start_ps, end_ps)
   │    When: analyze_failures' pre_window_transitions is not enough,
   │          need full transition history of a signal
   │    Note: `.fsdb` wave paths require fsdb_runtime.enabled == true
   │
   ├─ get_signals_around_time(wave_path, signal_paths, center_time_ps)
   │    When: Need to inspect additional signals not included in step 5,
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

On a scoreboard/compare-style failure, `parse_sim_log` sets a generic
`protocol_symptom_hint` (mirrored into `get_diagnostic_snapshot`'s top-level
output) reminding you that such a mismatch is frequently the *symptom* of a
lower-level bus-protocol problem — run `sweep_handshakes` once to check the
protocol health of every bus interface (AHB + AXI/valid-ready) in a single call
before reading RTL line-by-line or scrubbing the waveform by hand; drill into a
single interface with `suggest_protocol_bundles`/`suggest_handshakes` +
`inspect_handshake` only if needed. The hint is a boundary-safe pointer only: it
never asserts a protocol type or names a specific signal, and the
two-hypothesis discipline above still applies.

## Tool Dependency Graph

```
get_sim_paths ──→ build_tb_hierarchy ──→ parse_sim_log ──→ recommend_failure_debug_next_steps ──→ search_signals ──→ analyze_failures
     │                                                                              │
     │  provides:                                                                   │
     │  - compile_log path + phase                                                  ▼
     │  - discovery_mode / case_dir                                        ┌─── deep dive ───┐
     │  - sim_logs[0].path                                                 │                  │
     │  - wave_files[0].path                                               │                  │
     │  - simulator type                                                   │ analyze_failure_event
     │                                                                     │ explain_signal_driver
     │                                                                     │ get_error_context│
     └─────────────────────────────────────────────────────────────────→   │ get_signal_*     │
           all downstream tools use paths and simulator from step 1        └──────────────────┘
```

## Parameter Flow

| Parameter | Source | Consumed by |
|-----------|--------|-------------|
| `compile_log` | `get_sim_paths → compile_logs[phase="elaborate"].path` | `build_tb_hierarchy` |
| `simulator` | `get_sim_paths → simulator` | `build_tb_hierarchy`, `parse_sim_log`, `analyze_failures` |
| `log_path` (sim) | `get_sim_paths → sim_logs[0].path` | `parse_sim_log`, `get_error_context`, `analyze_failures` |
| `wave_path` | `get_sim_paths → chosen wave file (.vcd preferred when fsdb_runtime.enabled=false)` | `search_signals`, `get_signal_*`, `analyze_failures` |
| `failure_event` | `parse_sim_log → failure_events[]` | `analyze_failure_event` |
| `signal_paths` | `search_signals → results[].path` | `analyze_failures`, `get_signals_around_time` |
| `group_index` | Agent decision from `parse_sim_log → groups` | `analyze_failures` |
| `line` | `parse_sim_log → groups[].first_line` | `get_error_context` |
| `center_time_ps` | `parse_sim_log → failure_events[].time_ps` or `groups[].first_time_ps` | `get_signals_around_time` |
| `signal_path` | `search_signals → results[].path` or waveform observation | `explain_signal_driver`, `find_signal_loads`, `trace_x_source` |
| `from_signal` / `to_signal` | `search_signals → results[].path` (endpoints chosen by agent) | `trace_signal_path` |

> Time parameters (`time_ps`, `center_time_ps`, `start_time_ps`, `end_time_ps`) accept a **TimeSpec**: a raw integer (ps), a cursor reference `@<name>`, or a unit literal like `12.34ns`. So a time anchor located by `diff_first_divergence` / `period` (auto-registered as a cursor) can feed downstream `get_signal_*` / `trace_x_source` calls as `@<name>` instead of a copied timestamp.

## Optional Analysis Primitives

Most of these are not part of the default flow above; reach for them when the symptom is timing- or divergence-shaped rather than a logged value mismatch. The exception is `sweep_handshakes`, which is now a **default-flow protocol-health step** — run it after `parse_sim_log` on a failing run that has a waveform, like `scan_structural_risks` at the runtime layer (see Repository Guidance / `AGENTS.md`).

- `period(wave_path, signal, edge?)` — when a signal should be periodic (clock, strobe, fixed-rate valid) and the symptom is a cadence/throughput irregularity with no value in the log. Returns the dominant period and the first off-beat (auto-cursor).
- `diff_first_divergence(wave_path_a, signal_a, wave_path_b, signal_b)` — when two waveform signals should match: cross-run (passing vs failing) or within-run (lockstep / shadow). Returns the first unequal instant (auto-cursor). Needs both sides dumped as waveform signals; does not compare against a software reference model.
- `cursor_set` / `cursor_list` / `cursor_delete` — manage the named time anchors referenced above.
- `inspect_handshake(wave_path, clock, valid, ready, payload?)` — cycle-by-cycle classification of a clocked valid/ready handshake: stalls, long-stall windows, backpressure imbalance, payload-hold violations during a stall, and **premature valid deassertion** (`check_valid_hold`, default on): a stalled beat whose valid/htrans drops the next edge before ready/HREADY arrives — the master dropping the transfer instead of waiting (the AHB master-not-waiting-for-HREADY bug). The deassertion check needs no `payload` and catches what payload-hold cannot: a 1-cycle stall (`max_stall_cycles==1`) leaves no room for payload to change, and htrans (the derived valid) is not a payload signal. For protocol-timing bugs that leave no value pattern in scoreboard logs. AHB has no literal valid — pass `valid_htrans` instead. Its `coverage` object reports only dimensions it actually checked (`stall_checked`, `backpressure_checked`, `payload_hold_checked`/partial, `valid_hold_checked`); side labels must come from discovery/caller context, not this inspection tool. On a finding it sets `violating_signal` (the valid/htrans for a premature deassertion) + a `next_actions` link to `explain_signal_driver`: bus facts never self-attribute master vs slave (the trace holds values, not ownership) — attribution = bus-fact + drive-direction, composed by you. A clean bus with a wrong result means look INSIDE the consumer (slave mis-sampling), which no interface tool can see.
- `suggest_handshakes(wave_path, scope?)` — scan the waveform and propose ready-to-use `inspect_handshake` bundles (pairs `*valid`/`*ready`, finds the clock, groups payload). Run before `inspect_handshake` so you don't hand-assemble signal paths.
- `suggest_protocol_bundles(wave_path, protocol=ahb|apb, scope?)` — scan AHB/APB-style protocol bundles where there is no literal valid. AHB candidates return `valid_htrans`-based `inspect_handshake` args; APB candidates return `psel`/`penable`/`pready` facts and mark the missing derived-valid step. For AHB candidates the result also carries a `next_step` field — a copy-paste-ready `inspect_handshake(...)` call per interface — because discovery only LOCATES the bundle; running that call is the analysis step. Do not stop at discovery. Treat `direction_tag=unknown` as a real coverage limitation, not as permission to infer a side.
- `sweep_handshakes(wave_path, scope?)` — **default-flow protocol-health step** (runtime-layer counterpart of `scan_structural_risks`; `get_diagnostic_snapshot` lists it in `missing_steps` on a failing run with a waveform). Whole-design handshake anomaly sweep: discover every valid/ready interface **and every AHB interface** (htrans-derived valid) and inspect each in one call, returning a comparative fact table (each row tagged `kind`=`valid_ready`/`ahb`). The one-call protocol-health check the scoreboard-failure hint steers toward; for opaque global symptoms (timeout/hang) or any scoreboard mismatch when you don't yet know which interface misbehaves. APB excluded (needs a derived valid).
- `verify_window(wave_path, clock, mode, predicate | antecedent+consequent | delta)` — evaluate a temporal predicate (always/never/eventually/implication/sequence) over a clock window and return a `holds` verdict plus a concrete witness/counterexample. To prove or disprove an RTL inference in one call. `sequence` checks the per-accepted-beat increment of one signal (address-stride): `predicate` is the accepted-beat gate, `delta`=`{signal,value,op?,modulo?,restart_when?}` — `modulo` absorbs a legal WRAP wrap-around, `restart_when` (e.g. htrans==NONSEQ) re-seeds at burst starts, both supplied by you so the tool stays burst-decode-free. On a violation it sets `violating_signal` + a `next_actions` link to `explain_signal_driver` (master-driven signal → points at the master by elimination). Multi-slave/master: scope a property to one subset with a predicate term (per-slave `HSEL`, per-master `HMASTER`) — `inspect_handshake`'s htrans-only valid cannot qualify by select, so use the verify_window gate for per-slave/per-master checks.
- `reconstruct_transactions(wave_path, clock, req_valid, req_ready, cmp_valid, cmp_ready, ...)` — id-correlated request/response transaction layer: per-transaction latency plus outstanding/ordering/unmatched facts. AXI read AR→R, write AW→B (+ optional W-data channel); `req_id`/`cmp_id` optional (omit both for in-order AXI-Lite/APB streams).

## Iterative Debug Pattern

After step 6, the agent may loop:

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

## Notes

This document explains the recommended debug flow and the reasoning behind it.
It is intentionally not a second copy of the runtime `Server(instructions=...)`
text in `server.py`.
