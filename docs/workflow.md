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
│           and rerun hints such as previous_log_detected / candidate_previous_logs
│
│  What the agent does:
│  - Identify the earliest and most frequent error groups
│  - Prefer `failure_events[0].time_ps` as the waveform time anchor when present
│  - Cross-reference error signatures with step 2's hierarchy
│    (e.g., UVM_ERROR [SCOREBOARD_MISMATCH] → find_tb_instance(module="scoreboard")
│     or get_tb_subtree drilling from tree_skeleton)
│  - Decide which group to investigate first (usually group_index=0)
│  - If previous_log_detected == true, consider diff_sim_failure_results early
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
