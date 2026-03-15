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
│  Returns: top_module, component_tree, class_hierarchy, interfaces
│
│  What the agent learns:
│  - Module instantiation hierarchy (DUT structure)
│  - UVM component tree (test → env → agent → driver/monitor)
│  - Class inheritance chains
│  - Interface definitions and bindings
│  - Source file locations for each component
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
│    (e.g., UVM_ERROR [SCOREBOARD_MISMATCH] → locate scoreboard in component_tree)
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
│  Returns: list of matching signal paths with bit widths
│  Note: `.fsdb` wave paths are usable only when fsdb_runtime.enabled is true
│
│  How the agent picks keywords:
│  - From step 2's component_tree: module instance names → signal names
│  - From step 3's error message: signal names mentioned in assertions or checkers
│  - From RTL source code: read the relevant module file (path from step 2)
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
| `signal_path` | `search_signals → results[].path` or waveform observation | `explain_signal_driver` |

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

## Server Instructions

The current `server.py` should keep these instructions in `Server(instructions=...)`:

```
Waveform debug workflow:

1. ALWAYS start with get_sim_paths to discover file paths and simulator type.
   - Inspect discovery_mode first: root_dir, case_dir, or unknown.
   - If discovery_mode is unknown, do not guess deeper paths; follow returned hints.
   - If case_name unknown in root_dir mode, omit it to get available_cases, then ask user.
   - Pick compile_log with phase="elaborate" for build_tb_hierarchy.
   - If fsdb_runtime.enabled is false, prefer `.vcd` wave files over `.fsdb`.

2. Call build_tb_hierarchy to understand project structure BEFORE analyzing errors.
   - Use the elaborate-phase compile_log and detected simulator from step 1.

3. Call parse_sim_log to get grouped error summary, failure_events, and rerun hints.
   - Use `sim_logs[0].path` and `simulator` from step 1 only when sim_logs is non-empty.
   - Prefer normalized `failure_events[].time_ps` over parsing raw message text again.
   - If `previous_log_detected` is true, consider diff_sim_failure_results before restarting analysis from scratch.

4. Pick a usable waveform file.
   - Prefer `.vcd` when fsdb_runtime.enabled is false.

5. Call recommend_failure_debug_next_steps for a default target and role-ranked signals.
   - Use `recommended_signals` as the first candidates for waveform inspection.

6. Call search_signals to confirm full signal paths for investigation when needed.
   - Derive keywords from: step 2's component_tree, step 3's error messages, or RTL source.

7. Call analyze_failures to get combined log context + waveform snapshot.
   - Pass confirmed signal_paths from step 6.
   - Follow the returned analysis_guide.

8. Use deep-dive tools as needed:
   - analyze_failure_event: failure-centric instance/source correlation
   - explain_signal_driver: map suspicious waveform signals back to likely RTL drivers
   - get_error_context: inspect other error groups
   - get_signal_transitions: full signal history when pre_window is insufficient
   - get_signals_around_time: batch query additional signals at a time point
   - get_signal_at_time: exact value at a precise time
   - get_waveform_summary: simulation metadata and sanity checks
```
