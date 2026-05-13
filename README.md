# 🐙 TraceWeave

<p align="center">
  <img src="assets/logo.png" alt="TraceWeave" width="160">
</p>

<p align="center">
  <strong>MCP server for simulation-failure debug through log parsing and waveform analysis</strong>
</p>

<p align="center">
  <a href="https://github.com/gokeshenzhen/TraceWeave/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/gokeshenzhen/TraceWeave/ci.yml?branch=main&style=for-the-badge" alt="CI status"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge" alt="MIT License"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11%2B-blue?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+"></a>
  <a href="https://github.com/gokeshenzhen/TraceWeave/stargazers"><img src="https://img.shields.io/github/stars/gokeshenzhen/TraceWeave?style=for-the-badge" alt="Stars"></a>
</p>

<h2 align="center">波形日志根因分析 MCP，不想再调试，就用 TraceWeave。</h2>

TraceWeave 的特色：有 Verdi license 时启用 KDB/NPI 获得更精确的跨层级 driver/load/connectivity 分析；没有 license 时也可用内置 Static 后端、日志解析、VCD/FSDB 波形读取继续定位问题；支持 driver 回溯、load/fanout 查找、指定时刻取值、指定周期数采样、任意信号窗口查询、轻量化 X/Z trace、结构风险扫描、失败分组对比，并给 MCP 客户端输出结构化的下一步调试建议。

TraceWeave is a workflow-oriented debug server rather than a loose collection of parsers. It combines:

- An MCP server with session state, workflow gates, and recommended tool ordering
- Path discovery for compile logs, simulation logs, and waveform artifacts
- Compile-log-driven hierarchy building and source-aware driver correlation
- VCD and FSDB waveform backends with signal search
- Failure-centric recommendations, structural risk scanning, and X/Z propagation tracing
- Structured output schemas designed for MCP clients

[Architecture](docs/architecture.md) · [Installation](#installation) · [Client Setup](#client-setup) · [Standard MCP Workflow](#standard-mcp-workflow) · [Tool Quick Reference](#tool-quick-reference) · [Testing](#testing) · [WeChat](#wechat)

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
├── scripts/                  # setup_fsdb.sh / verify_fsdb.sh
├── tests/                    # Unit and integration tests
└── src/
    ├── path_discovery.py
    ├── compile_log_parser.py
    ├── tb_hierarchy_builder.py
    ├── vcd_parser.py
    ├── fsdb_parser.py
    ├── fsdb_signal_index.py
    ├── waveform_batch.py         # FSDB+VCD time-window batch reader
    ├── log_parser.py
    ├── analyzer.py
    ├── signal_driver.py
    ├── signal_load.py            # Load/fanout finder, Static + NPI
    ├── connectivity_backend.py   # ConnectivityBackend protocol + select_backend
    ├── verdi_backend.py          # KDB / license probe + kdb_hint generator
    ├── verdi_npi_backend.py      # NPI-backed driver/load resolution
    ├── kdb_builder.py            # Auto-build Verdi KDB (vericom + elabcom) for Xcelium flows
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

Enable FSDB support (links the Verdi runtime into the repo and builds
`libfsdb_wrapper.so` in one step):

```bash
# Example only — replace with your site's Verdi install path
export VERDI_HOME=/tools/synopsys/verdi/O-2018.09-SP2-11
bash scripts/setup_fsdb.sh
```

Verify the runtime and wrapper load correctly. This script does **not**
require `$VERDI_HOME` and is safe to run on any host that already has the
repo-local artefacts:

```bash
bash scripts/verify_fsdb.sh
```

## Client Setup

### Generic MCP Client

Any MCP client that supports stdio transport can connect to this server. The minimum configuration is:

- command: `python3.11`
- args: `["/home/robin/Projects/mcp/TraceWeave/server.py"]`
- env: provide either repo-local `third_party/verdi_runtime/linux64` or `VERDI_HOME` if FSDB support is required

If the client supports server instructions, it can follow the built-in workflow directly. Otherwise, use the workflow below.

### Claude Code

Neither Claude Code nor Codex inherits your interactive shell env into the spawned MCP stdio server, so list every variable the server needs — tool roots plus the `dlopen` chain (`LD_LIBRARY_PATH` is the one most often missed; without it NPI silently falls back to Static and `trace_signal_path` returns `found: false`).

Add this to `~/.claude.json`:

```json
{
  "mcpServers": {
    "TraceWeave": {
      "command": "python3.11",
      "args": ["/home/robin/Projects/mcp/TraceWeave/server.py"],
      "env": {
        "VERDI_HOME": "/tools/synopsys/verdi/V-2023.12-SP2",
        "NOVAS_HOME": "/tools/synopsys/verdi/V-2023.12-SP2",
        "VCS_HOME": "/tools/synopsys/vcs/V-2023.12-SP2",
        "XLM_ROOT": "/tools/cadence/XCELIUM2603",
        "CDS_INST_DIR": "/tools/cadence/XCELIUM2603",
        "SNPSLMD_LICENSE_FILE": "27000@synopsys-license.example.com",
        "LM_LICENSE_FILE": "5280@license-server.example.com",
        "CDS_LICENSE_FILE": "5280@cadence-license.example.com",
        "LD_LIBRARY_PATH": "/tools/synopsys/verdi/V-2023.12-SP2/share/PLI/IUS/LINUX64:/tools/synopsys/verdi/V-2023.12-SP2/share/PLI/VCS/LINUX64",
        "PATH": "/tools/synopsys/verdi/V-2023.12-SP2/bin:/tools/synopsys/vcs/V-2023.12-SP2/bin:/tools/synopsys/vcs/V-2023.12-SP2/amd64/bin:/tools/cadence/XCELIUM2603/tools/bin/64bit:/tools/cadence/XCELIUM2603/tools/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
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

Same idea as Claude Code — list everything explicitly. Add to `~/.codex/config.toml`:

```toml
[mcp_servers.TraceWeave]
command = "python3.11"
args = ["/home/robin/Projects/mcp/TraceWeave/server.py"]
cwd = "/home/robin/Projects/mcp/TraceWeave"
env = {
  VERDI_HOME      = "/tools/synopsys/verdi/V-2023.12-SP2",
  NOVAS_HOME      = "/tools/synopsys/verdi/V-2023.12-SP2",
  VCS_HOME        = "/tools/synopsys/vcs/V-2023.12-SP2",
  XLM_ROOT        = "/tools/cadence/XCELIUM2603",
  CDS_INST_DIR    = "/tools/cadence/XCELIUM2603",
  SNPSLMD_LICENSE_FILE = "27000@synopsys-license.example.com",
  LM_LICENSE_FILE     = "28000@license-server.example.com",
  CDS_LICENSE_FILE    = "28000@cadence-license.example.com",
  LD_LIBRARY_PATH = "/tools/synopsys/verdi/V-2023.12-SP2/share/PLI/IUS/LINUX64:/tools/synopsys/verdi/V-2023.12-SP2/share/PLI/VCS/LINUX64",
  PATH            = "/tools/synopsys/verdi/V-2023.12-SP2/bin:/tools/synopsys/vcs/V-2023.12-SP2/bin:/tools/synopsys/vcs/V-2023.12-SP2/amd64/bin:/tools/cadence/XCELIUM2603/tools/bin/64bit:/tools/cadence/XCELIUM2603/tools/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
}
```

Verify the connection:

```bash
codex mcp list
# Should show TraceWeave with Status: enabled
```

### Functional Verification

After connecting either client, run a quick end-to-end smoke test:

1. Start `codex` or `claude` inside a project directory that contains a sim log and waveform files.
2. Submit a direct waveform-debug request, for example: "Call the TraceWeave MCP. Start with `get_sim_paths` to list the logs and waves for this case."
3. Confirm that the execution log shows actual MCP tool calls such as `get_sim_paths`, `parse_sim_log`, and `search_signals` — not just shell commands reading files manually.

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

- `search_signals`: Resolve full hierarchical signal paths. Each result also carries `direction` (`input`/`output`/`inout`/`implicit`/`null`) and `var_type` (`wire`/`reg`/`integer`/`real`/`parameter`/…), so clients can filter ports/nets/variables in a chosen scope without a separate tool. **FSDB** populates both fields; **VCD** populates only `var_type` and returns `direction: null` (the VCD format does not encode port direction)
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
- `find_signal_loads`: List the consumers (fanout) of a signal — module-input ports, RHS uses, always-block sensitivity
- `trace_signal_path`: Find a connectivity path between two signals in the elaborated netlist (NPI-only). Returns connectivity, NOT temporal driver direction — use `explain_signal_driver` for driver semantics. Without a Verdi KDB this tool returns `unsupported_reason="static_backend_no_path_api"` because source-regex cannot honestly reproduce `sig_to_sig_conn_list`; fall back to `explain_signal_driver` + `find_signal_loads` in that case.
- `trace_x_source`: Trace X/Z propagation upstream
- `build_kdb`: Auto-build a Verdi KDB from the parsed compile log (vericom + elabcom). Use when the simulator is Xcelium (xrun) and the NPI backend reports no KDB. Output is cached under `TRACEWEAVE_CACHE_DIR` (default `~/.cache/traceweave/kdb/<hash>/`); cache hits skip re-running Verdi. A runnable `build.sh` is written next to the KDB for inspection or manual reproduction. Requires `VERDI_HOME` with `bin/vericom` and `bin/elabcom`.

`explain_signal_driver`, `find_signal_loads`, and `trace_signal_path` automatically engage a Verdi NPI backend when a KDB is detected. The first two transparently fall back to a Static source-regex backend when NPI is unavailable; `trace_signal_path` is NPI-only and returns a structured `unsupported_reason` instead of an approximation, since `sig_to_sig_conn_list` has no honest static equivalent. NPI is the deep / accurate path: it walks the elaborated netlist with `fan_in_reg_list` / `sig_to_sig_conn_list`, so it can cross instance port boundaries, interface positional bindings, and assign chains that Static cannot follow. When a KDB is present, `build_tb_hierarchy` also overlays each component-tree node's `source_file` / `source_line` with NPI's elaborated `file:line`; affected hops in `find_driver` / `find_loads` results carry a `source_info_origin: "npi"` tag so consumers can tell NPI-annotated entries from compile-log-derived ones. The result envelope carries a `backend_status` block with the active backend, KDB flow, and a per-simulator `kdb_hint`.

For VCS flows the cheapest way to get a KDB is to recompile with `-kdb=only` — the hint surfaces the exact command. For Xcelium flows there is no native KDB; `get_diagnostic_snapshot` will list `build_kdb` in `missing_steps` so the LLM agent can produce one on demand. Set `TRACEWEAVE_AUTO_KDB=0` to opt out of the auto-build suggestion.

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

## WeChat

Follow the WeChat public account:

<p align="center">
  <img src="assets/QR.png" alt="WeChat public account QR code" width="200">
</p>
