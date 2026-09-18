# 🐙 TraceWeave

<!-- mcp-name: io.github.gokeshenzhen/traceweave -->

<p align="right">
  <strong>English</strong> · <a href="https://github.com/gokeshenzhen/TraceWeave/blob/main/README.zh.md">简体中文</a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/gokeshenzhen/TraceWeave/main/assets/logo.png" alt="TraceWeave" width="160">
</p>

<p align="center">
  <strong>An MCP server for evidence-driven simulation debugging</strong>
</p>

<p align="center">
  <a href="https://github.com/gokeshenzhen/TraceWeave/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/gokeshenzhen/TraceWeave/ci.yml?branch=main&style=for-the-badge" alt="CI status"></a>
  <a href="https://pypi.org/project/traceweave-mcp/"><img src="https://img.shields.io/pypi/v/traceweave-mcp?style=for-the-badge" alt="PyPI version"></a>
  <a href="https://github.com/gokeshenzhen/TraceWeave/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge" alt="MIT License"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11%2B-blue?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+"></a>
  <a href="https://github.com/gokeshenzhen/TraceWeave/stargazers"><img src="https://img.shields.io/github/stars/gokeshenzhen/TraceWeave?style=for-the-badge" alt="Stars"></a>
</p>

TraceWeave is an MCP server for RTL / SoC debugging. It connects compile records, simulation logs, VCD/FSDB waveforms, and RTL source to your AI assistant, helping you work from a failed simulation to the relevant time, signals, and driving logic.

Use it with Claude Code, Codex, Copilot, or another MCP client. Describe the problem in natural language; the assistant uses tools to investigate, trace, and verify, with evidence you can review.

[Use Cases](#use-cases) · [Installation](#installation) · [Client Setup](#client-setup) · [Start Debugging](#start-debugging) · [Tool Quick Reference](#tool-quick-reference) · [FAQ](#faq) · [Documentation and Feedback](#documentation-and-feedback)

<p align="center">
  <img src="https://raw.githubusercontent.com/gokeshenzhen/TraceWeave/main/assets/onepage-en.png" alt="TraceWeave debugging workflow from logs and waveforms to source tracing" width="900">
</p>

## Use Cases

| Problem | How TraceWeave helps |
|---|---|
| A simulation times out or hangs, with no clear starting point | Summarize failures and scan interface handshakes to narrow down the affected interfaces and time window |
| A scoreboard mismatch or different results between runs | Compare failure records and waveforms, locate observed differences, and trace data and control sources on both sides |
| A signal becomes X/Z | Inspect the surrounding waveform and follow upstream drivers to trace unknown-value propagation |
| Suspected ties, unconnected inputs, or magic-word conditions | Extract constant connections, open inputs, and constant comparisons as leads for further investigation |
| A deep SoC hierarchy with many modules and interfaces | Browse hierarchy on demand, locate instances and source files, and follow drivers, consumers, and connectivity paths |
| A debugging hypothesis needs evidence | Sample by cycle and check timing conditions, handshake stability, and transaction completion |

Supports VCS / Xcelium simulation logs and VCD / FSDB waveforms. Structural queries can use Source Graph without a commercial license, or integrate with Verdi NPI. Exported formal waveforms can also be queried; automatic artifact discovery currently supports JasperGold.

## Installation

Requires **Python 3.11+**. Choose the installation that fits your environment:

| Installation | Intended use |
|---|---|
| Repository | Simulation hosts with an existing Verdi installation, for FSDB, Source Graph, and optional NPI / LSF |
| PyPI | Log and VCD analysis, or Source Graph without a commercial license |

### Repository Installation

```bash
git clone https://github.com/gokeshenzhen/TraceWeave.git
cd TraceWeave
export VERDI_HOME=/path/to/verdi
bash scripts/install.sh
```

The installer prepares the Python environment, Source Graph, and FSDB reader, then checks the runtime. It leaves shell startup files and MCP client configuration untouched. NPI still requires the appropriate EDA runtime and license from your site.

For an existing installation, start with a read-only check:

```bash
bash scripts/install.sh --check
```

### PyPI Installation

```bash
python3.11 -m pip install "traceweave-mcp[source-graph]"
traceweave-mcp --doctor
```

For logs, VCD, and basic static analysis only, install `traceweave-mcp` without `[source-graph]`. The PyPI package does not include the FSDB reader; use the repository installation for FSDB.

## Client Setup

After a repository installation, generate a client configuration template with absolute paths:

| Client | Generate configuration template |
|---|---|
| Claude Code | `bash scripts/install.sh --print-config claude` |
| Codex | `bash scripts/install.sh --print-config codex` |
| Copilot | `bash scripts/install.sh --print-config copilot` |

These commands only print templates. Add the output to your client's MCP configuration, supply any required site EDA environment variables, and reconnect the server. See the [client configuration reference](https://github.com/gokeshenzhen/TraceWeave/blob/main/docs/architecture.md#client-configuration-reference) for complete examples.

Other MCP clients supporting stdio can use these connection settings:

| Installation | `command` | `args` |
|---|---|---|
| Repository | `<absolute-repository-path>/.venv/bin/python` | `["<absolute-repository-path>/server.py"]` |
| PyPI | `traceweave-mcp`, or its absolute path | `[]` |

### LSF-only NPI licenses

If Verdi/NPI licenses are available only on LSF compute nodes, provide these settings to the MCP server process. Replace `digital` with your queue:

```bash
export TRACEWEAVE_NPI_EXECUTION=lsf
export TRACEWEAVE_NPI_LSF_QUEUE="digital"
```

The client must inherit or explicitly pass these variables. Project files, the TraceWeave installation, and cache directories must be visible at the same absolute paths on submission and compute nodes. See the [LSF configuration reference](https://github.com/gokeshenzhen/TraceWeave/blob/main/docs/architecture.md#lsf-only-npi-licenses) for bash / tcsh examples, client environment forwarding, and verification steps.

## Start Debugging

Once connected, try a request like this, replacing the path with your project directory:

> Use TraceWeave to investigate the simulation failure in `my_case` under `/path/to/verif`. Start with the logs, design structure, and interface handshakes, then trace suspicious signals. Include timestamps, signals, and source references in your findings.

You can also start with a specific question:

> Compare `tb.dut.result` in these two waveforms and trace the logic producing the difference on each side.

> Use deep mode to scan the design described by this compile log for suspicious ties, unconnected inputs, and constant comparisons.

The assistant's default investigation follows these steps:

1. **Find the run's artifacts:** locate compile logs, simulation logs, and waveforms.
2. **Establish design context:** build the hierarchy and scan structural risks in parallel using the same compile log.
3. **Narrow the investigation:** parse failures and scan handshakes for failed runs with a waveform.
4. **Trace and verify:** inspect signals, drivers, and consumers, then test hypotheses with window or cycle checks.
5. **Compare after a fix:** check how failure records and waveforms change in the next run.

For a first connection check, ask the assistant to call `get_sim_paths` and confirm that actual MCP tool calls run. See the [debug workflow](https://github.com/gokeshenzhen/TraceWeave/blob/main/docs/workflow.md) for the full procedure.

## Tool Quick Reference

Usually, you describe the debugging goal and let the assistant select the tools. This table lists every tool by purpose; MCP tool definitions provide the parameters.

| Category | Tool | Description |
|---|---|---|
| Session and artifacts | `get_sim_paths` | Find simulation cases, compile logs, runtime logs, and waveforms |
| Session and artifacts | `get_formal_paths` | Find formal projects, logs, and exported waveforms; currently supports JasperGold |
| Session and artifacts | `get_diagnostic_snapshot` | Review collected debugging information and outstanding steps |
| Hierarchy and source | `build_tb_hierarchy` | Build an RTL / testbench hierarchy view from compile records |
| Hierarchy and source | `get_tb_subtree` | Browse a selected instance's local hierarchy |
| Hierarchy and source | `find_tb_instance` | Find instances by path or module name |
| Hierarchy and source | `lookup_tb_files` | Find source files in the actual compiled file set |
| Hierarchy and source | `get_tb_file_detail` | Inspect modules, interfaces, and classes defined in a source file |
| Hierarchy and source | `get_tb_class_hierarchy` | Browse UVM / SystemVerilog class inheritance |
| Hierarchy and source | `dump_tb_section` | Retrieve a complete section of hierarchy analysis data |
| Logs and failures | `parse_sim_log` | Group runtime failures and extract timestamps and error summaries |
| Logs and failures | `get_error_context` | Read the original log around an error |
| Logs and failures | `diff_sim_failure_results` | Compare new, persistent, and resolved failures between runs |
| Logs and failures | `analyze_failures` | Combine log and waveform context for a failure group |
| Logs and failures | `analyze_failure_event` | Identify candidate instances, signals, and source files for one failure |
| Logs and failures | `recommend_failure_debug_next_steps` | Recommend investigation targets and follow-up tool calls |
| Structure and tracing | `scan_structural_risks` | Scan suspicious structures; semantic mode checks ties, open inputs, and constant comparisons |
| Structure and tracing | `explain_signal_driver` | Trace a signal's driver and relevant RTL |
| Structure and tracing | `find_signal_loads` | Find a signal's consumers and potential impact |
| Structure and tracing | `trace_signal_path` | Query structural connectivity between two signals |
| Structure and tracing | `trace_x_source` | Follow upstream drivers to trace X/Z propagation |
| Waveform queries | `get_waveform_summary` | Inspect waveform format, duration, timescale, and top modules |
| Waveform queries | `search_signals` | Find full signal paths by name |
| Waveform queries | `get_signal_at_time` | Read a signal's value at a specific time |
| Waveform queries | `get_signal_transitions` | Inspect signal transitions within a time window |
| Waveform queries | `get_signals_around_time` | Inspect multiple signals around a selected time |
| Waveform queries | `get_signals_by_cycle` | Sample multiple signals on clock edges |
| Differences and timing | `diff_first_divergence` | Find the first observed known-value difference between two signals |
| Differences and timing | `trace_divergence` | Verify a waveform difference and trace relevant data, control, and prior state on both sides |
| Differences and timing | `period` | Check signal periods and cadence anomalies |
| Differences and timing | `verify_window` | Verify temporal conditions within a waveform window and return concrete evidence |
| Protocols and transactions | `suggest_handshakes` | Discover valid/ready interfaces and signal bundles for inspection |
| Protocols and transactions | `suggest_protocol_bundles` | Discover AHB / APB interface signal bundles |
| Protocols and transactions | `sweep_handshakes` | Scan discovered valid/ready and AHB interfaces across the design and summarize anomalies |
| Protocols and transactions | `inspect_handshake` | Check one interface for stalls, stability, and handshake anomalies |
| Protocols and transactions | `reconstruct_transactions` | Reconstruct requests and responses to inspect latency, outstanding requests, and ordering |
| Time cursors | `cursor_set` | Name a key timestamp for reuse in later queries |
| Time cursors | `cursor_list` | List time cursors in the current session |
| Time cursors | `cursor_delete` | Remove a time cursor |
| EDA integration | `build_kdb` | Build and cache a Verdi KDB from compile records for NPI queries |

## FAQ

**Can I use TraceWeave without a commercial license?**

Log analysis, VCD queries, and Source Graph do not need a commercial license. FSDB reading requires local Verdi reader libraries; Verdi NPI and KDB builds require the corresponding EDA environment and license. When NPI is unavailable, structural queries try Source Graph, then fall back to basic static analysis where supported.

**How do I choose auto or deep for structural scanning?**

The default `auto` mode runs static checks and reuses compatible semantic results already available. Ask the assistant to use `deep` when you need a new semantic scan. The `analysis_mode` parameter applies to each call; no client configuration change is needed. Findings are leads to investigate: legitimate tie-offs and protocol constants may also appear.

**Does a scan with no findings mean the design is correct?**

Conclusions depend on actual coverage. Missing signals, resource limits, or unsupported structures can limit analysis. Zero findings with zero or partial coverage do not establish that a design is free of problems. Tools report coverage and truncation so the assistant can decide whether to narrow the scope and investigate further.

**Can I inspect formal waveforms?**

You can query exported VCD / FSDB files and automatically discover JasperGold artifacts. Property results, trace classification, and reachability semantics must come from the formal tool or the user.

**Where is design data processed?**

File parsing and EDA queries run locally or on the configured LSF compute nodes. Returned debugging evidence enters your AI client's context. TraceWeave usage telemetry is off by default and writes only to local files when enabled.

## Documentation and Feedback

| Learn more about | Resource |
|---|---|
| Debugging steps and tool cooperation | [Debug workflow](https://github.com/gokeshenzhen/TraceWeave/blob/main/docs/workflow.md) |
| Verifying a root cause with evidence | [Debug discipline](https://github.com/gokeshenzhen/TraceWeave/blob/main/docs/debug-discipline.md) |
| Architecture, backend capabilities, and resource limits | [Architecture](https://github.com/gokeshenzhen/TraceWeave/blob/main/docs/architecture.md) |
| Complete environment configuration and advanced settings | [Configuration reference](https://github.com/gokeshenzhen/TraceWeave/blob/main/docs/architecture.md#client-configuration-reference) |
| Reporting a problem or suggesting a feature | [GitHub Issues](https://github.com/gokeshenzhen/TraceWeave/issues) |

Before contributing, read [AGENTS.md](https://github.com/gokeshenzhen/TraceWeave/blob/main/AGENTS.md). With test dependencies installed, run `python3.11 -m pytest` from the repository root.

TraceWeave is available under the [MIT License](https://github.com/gokeshenzhen/TraceWeave/blob/main/LICENSE).

## WeChat

Follow the WeChat public account for project updates and debugging examples:

<p align="center">
  <img src="https://raw.githubusercontent.com/gokeshenzhen/TraceWeave/main/assets/QR.png" alt="WeChat public account QR code" width="200">
</p>
