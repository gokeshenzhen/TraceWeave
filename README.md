# TraceWeave

<p align="center">
  <img src="assets/logo.png" alt="TraceWeave" width="160">
</p>

<p align="center">
  <strong>MCP server for simulation-failure debug through log parsing and waveform analysis</strong>
</p>

TraceWeave is a workflow-oriented debug server rather than a loose collection of parsers. It combines:

- An MCP server with session state, workflow gates, and recommended tool ordering
- Path discovery for compile logs, simulation logs, and waveform artifacts
- Compile-log-driven hierarchy building and source-aware driver correlation
- VCD and FSDB waveform backends with signal search
- Failure-centric recommendations, structural risk scanning, and X/Z propagation tracing
- Structured output schemas designed for MCP clients

[Architecture](docs/architecture.md) · [Installation](#installation) · [Client Setup](#client-setup) · [Standard MCP Workflow](#standard-mcp-workflow) · [Tool Quick Reference](#tool-quick-reference) · [Testing](#testing)

## Architecture

- Architecture map: `docs/architecture.md`
- New-session bootstrap: read `AGENTS.md` first, then follow its first-read file list
- Fast path for code understanding:
  - `server.py`
  - `config.py`
  - `src/analyzer.py`
  - `src/log_parser.py`
  - `src/fsdb_parser.py`

## Repository Layout

```text
TraceWeave/
├── config.py                 # Environment-sensitive constants and discovery rules
├── server.py                 # MCP entry point, session state, and workflow gating
├── custom_patterns.yaml      # User-extensible log patterns
├── fsdb_wrapper.cpp          # Native FSDB wrapper source
├── build_wrapper.sh          # Builds libfsdb_wrapper.so
├── scripts/                  # Utility scripts such as link_verdi_runtime.sh
├── tests/                    # Unit and integration tests
└── src/
    ├── path_discovery.py
    ├── compile_log_parser.py
    ├── tb_hierarchy_builder.py
    ├── vcd_parser.py
    ├── fsdb_parser.py
    ├── fsdb_signal_index.py
    ├── log_parser.py
    ├── analyzer.py
    ├── signal_driver.py
    ├── structural_scanner.py
    ├── x_trace.py
    ├── cycle_query.py
    ├── schemas.py
    └── problem_hints.py
```

## Installation

TraceWeave requires Python `3.11+`.

```bash
pip install mcp pyyaml --user
```

For FSDB support, one of these runtime sources must be available:

- Repo-local runtime: `third_party/verdi_runtime/linux64/libnsys.so` and `libnffr.so`
- External Verdi installation exposed via `VERDI_HOME/share/FsdbReader/linux64`

If neither is available, TraceWeave still works, but FSDB parsing is disabled and the workflow should prefer `.vcd` waveforms.

Prepare the repo-local runtime:

```bash
export VERDI_HOME=/tools/synopsys/verdi/O-2018.09-SP2-11
bash scripts/link_verdi_runtime.sh
```

Verify the runtime can be loaded:

```bash
python3 -c "
import ctypes
d = 'third_party/verdi_runtime/linux64'
ctypes.CDLL(d + '/libnsys.so', ctypes.RTLD_GLOBAL)
ctypes.CDLL(d + '/libnffr.so')
print('FSDB runtime load OK')
"
```

## Client Setup

### Generic MCP Client

Any MCP client that supports stdio transport can connect to this server. The minimum configuration is:

- command: `python3.11`
- args: `["/home/robin/Projects/mcp/TraceWeave/server.py"]`
- env: provide either repo-local `third_party/verdi_runtime/linux64` or `VERDI_HOME` if FSDB support is required

If the client supports server instructions, it can follow the built-in workflow directly. Otherwise, use the workflow below.

### Claude Code

Add this to `~/.claude.json`:

```json
{
  "mcpServers": {
    "traceweave": {
      "command": "python3.11",
      "args": ["/home/robin/Projects/mcp/TraceWeave/server.py"],
      "env": {
        "VERDI_HOME": "/tools/synopsys/verdi/O-2018.09-SP2-11",
        "VCS_HOME": "/tools/synopsys/vcs/O-2018.09-SP2-11",
        "XLM_ROOT": "/tools/cadence/XCELIUM1803",
        "PATH": "/tools/synopsys/verdi/O-2018.09-SP2-11/bin:/tools/synopsys/vcs/O-2018.09-SP2-11/bin:/tools/cadence/XCELIUM1803/tools/bin:/usr/local/bin:/usr/bin:/bin"
      }
    }
  }
}
```

Environment variables must be set explicitly in the config. Claude Code does not automatically source your shell profile.

### Codex

Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.traceweave]
command = "python3.11"
args = ["/home/robin/Projects/mcp/TraceWeave/server.py"]
cwd = "/home/robin/Projects/mcp/TraceWeave"

[mcp_servers.traceweave.env]
VERDI_HOME = "/tools/synopsys/verdi/O-2018.09-SP2-11"
VCS_HOME   = "/tools/synopsys/vcs/O-2018.09-SP2-11"
XLM_ROOT   = "/tools/cadence/XCELIUM1803"
PATH       = "/tools/synopsys/verdi/O-2018.09-SP2-11/bin:/tools/synopsys/vcs/O-2018.09-SP2-11/bin:/tools/cadence/XCELIUM1803/tools/bin:/usr/local/bin:/usr/bin:/bin"
```

If the file already contains other configuration, append this block instead of overwriting it.

## Standard MCP Workflow

This is the default workflow for simulation-log and waveform debug:

1. Call `get_sim_paths(verif_root, case_name?)`.
2. Choose the `phase == "elaborate"` compile log.
3. Run `build_tb_hierarchy` and `scan_structural_risks` in parallel on that same compile log.
4. If a sim log is present, call `parse_sim_log`.
5. Use `recommend_failure_debug_next_steps` or `analyze_failure_event`.
6. Use `search_signals` and `analyze_failures` when you need waveform snapshots for explicit signals.
7. Use `explain_signal_driver`, `trace_x_source`, or `get_signals_by_cycle` for deeper investigation.
8. Use `get_diagnostic_snapshot` at any time to inspect reusable cached session state.

Important workflow rules:

- `scan_structural_risks` is part of the default workflow and should not be skipped unless the user explicitly asks to skip it.
- Use the same `compile_log` for both `build_tb_hierarchy` and `scan_structural_risks`.
- Prefer `failure_events[].time_ps` from `parse_sim_log` as the waveform time anchor.
- If `fsdb_runtime.enabled == false`, prefer `.vcd` over `.fsdb`.

## Tool Quick Reference

### Session Overview

- `get_diagnostic_snapshot`: Read-only summary of cached session data and suggested next calls

### Paths and Hierarchy

- `get_sim_paths`: Discover compile logs, sim logs, waveforms, simulator, and cases
- `build_tb_hierarchy`: Build testbench hierarchy, source grouping, and interface metadata
- `scan_structural_risks`: Scan compiled RTL/TB sources for structural risk patterns

### Log Analysis

- `parse_sim_log`: Parse and normalize runtime failures into grouped summaries and `failure_events`
- `diff_sim_failure_results`: Compare two simulation runs
- `get_error_context`: Extract raw log context around a specific line

### Waveform Analysis

- `search_signals`: Resolve full hierarchical signal paths
- `get_signal_at_time`: Query a signal value at a specific timestamp
- `get_signal_transitions`: Retrieve transitions for a signal over time
- `get_signals_around_time`: Retrieve context around a failure timestamp
- `get_signals_by_cycle`: Sample signals cycle-by-cycle on a clock edge
- `get_waveform_summary`: Return waveform metadata

### Deep-Dive Analysis

- `analyze_failures`: Focus on one grouped failure and return log plus waveform context
- `analyze_failure_event`: Rank likely instances, source files, and signals for a specific `failure_event`
- `recommend_failure_debug_next_steps`: Return the default next debug target
- `explain_signal_driver`: Trace a waveform signal back to likely RTL driver logic
- `trace_x_source`: Trace X/Z propagation upstream

## Testing

Run the full test suite from the repo root:

```bash
python3.11 -m pytest
```

Run a single file:

```bash
python3.11 -m pytest tests/test_server.py
```

Run a single test:

```bash
python3.11 -m pytest tests/test_server.py -k diagnostic_snapshot
```

Recommended change flow:

1. Make the code change.
2. Run the relevant tests first.
3. Run the full suite if the change affects shared behavior.
4. Restart the MCP client so it reconnects to the updated server.
