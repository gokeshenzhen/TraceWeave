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
| A signal becomes X/Z | Follow drivers and review waveform history to check whether a register sampled and retained an unknown value |
| Packed buses are hard to read, or TL-UL requests are not completing | Inspect address, data, and control fields individually, then check request/response handshakes, latency, and completion |
| Suspected ties, unconnected inputs, or magic-word conditions | Statically scan source code for constant connections, open inputs, and constant comparisons as investigation leads; no simulation run or waveform required |
| A deep SoC hierarchy with many modules and interfaces | Browse hierarchy on demand, locate instances and source files, and follow drivers, consumers, and connectivity paths |
| A debugging hypothesis needs evidence | Sample by cycle and check timing conditions, handshake stability, and transaction completion |

Supports VCS / Xcelium simulation logs and VCD / FSDB waveforms. Exported formal waveforms can also be queried; automatic artifact discovery currently supports JasperGold.

Signal tracing (driver, load, and connectivity path queries) follows **Verdi NPI → Source Graph → basic static analysis (Legacy Static)** by default. It first queries the elaborated KDB; when NPI is unavailable or cannot provide a trustworthy result, it tries Source Graph without a commercial license, then falls back to basic static analysis where supported.

Capabilities for large designs, with selected examples of validated scale:

- **Hierarchy and source browsing on demand**: the server builds and retains hierarchy and file indexes, then returns local results by instance, subtree, or file to keep large SoC queries manageable in the assistant's context. Hierarchy construction and local queries have been verified on a synthetic design with **50,500 logical instances**; the initial build still scans compilation records and sources.
- **Bulk handshake checks**: `sweep_handshakes` discovers AHB / valid-ready interfaces and checks stalls, payload stability, and premature valid/HTRANS deassertion. A recorded design with **78,817 total signals and a 2.59 ms waveform** yielded 49 candidate interfaces involving 262 clock/protocol signals; **35 interfaces were checked in about 4.65 minutes**, with 14 skipped and partial coverage. Runtime and coverage depend on the waveform, interface types, and resource limits.
- **GiB-scale log parsing**: `parse_sim_log` has been verified on a **1 GiB synthetic log with 2,097,152 lines**, finding all four errors at the beginning, middle, and end.

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

For a repository readback check without an EDA license, run
`.venv/bin/python scripts/check_waveform_readback.py --work-dir /tmp/traceweave-readback-check`.
It checks the server catalog and actual point/batch values through MCP, and
prints explicit requests for a separate AI-client smoke session. Server checks
leave AI-client visibility and model adoption unverified; see the
[readback check procedure](docs/architecture.md#readback-client-check).

### Find Waveform Differences and Trace X/Z Sources

When two simulation runs produce different results, TraceWeave can compare selected signals, locate the earliest difference it can confirm within a time window, and trace the related data, control, and RTL logic on both sides.

For X/Z, inspect the propagation path at the time of interest or look back through history to check whether a register sampled an unknown value and continued to hold it. You can investigate earlier events even if the input is now known:

> `tb.dut.q` is X at 36 ns, but its input is now known. Review the waveform from 0 to 36 ns, check earlier sampling and hold behavior, and trace possible sources.

Results distinguish relationships supported by evidence from leads that need more investigation. When history is missing or a structure is unsupported, TraceWeave explains where tracing stopped. The first recorded anomaly is not automatically its true point of origin.

See [waveform comparison](https://github.com/gokeshenzhen/TraceWeave/blob/main/docs/architecture.md#divergence-evidence-and-backtrace) and [X/Z history tracing](https://github.com/gokeshenzhen/TraceWeave/blob/main/docs/architecture.md#bounded-x-history) for tool parameters and detailed support limits.

### Evaluate SV Expressions

Existing waveform tools accept explicit `{ "expr": "a[b] + c", ... }` inputs. Expressions use SV precedence, widths, signedness and four-state values; indices are sampled again at each observation. Point, cycle, transition, window, handshake, transaction and TL-UL queries share this core with RTL backtracing. No additional MCP tool is required.

Use exact `bindings` and explicit `types`, or choose `typing: "wave_bits"` for an unsigned four-state view of dumped vectors. Fixed arrays use bounded element mappings; reading a formula does not replace the recorded output signal. See [expression inputs, examples and limits](docs/expressions.md).

### Inspect Bus Fields and TL-UL Interfaces

When address, data, and control information share a packed bus, TraceWeave can identify field positions from source types that match the waveform. The assistant can then inspect individual fields, reducing manual work to look up widths and calculate offsets. You can also provide a field-to-bit mapping when automatic resolution is unavailable.

For TL-UL interfaces, once the assistant confirms the field mapping and clock, it can check handshake stalls, request/response matching, response latency, and outstanding requests within a specified time window:

> Check the TL-UL interface under `tb.dut` from 10 to 20 μs for handshake stalls or outstanding requests. List the relevant times and signals.

Results state which checks ran and what information is missing. A request still outstanding at the end of the window is not automatically a deadlock, and these checks do not replace full TL-UL protocol verification.

For a long debugging conversation, ask for compact evidence output. Repeated field mappings are shown once, while checked scope, missing evidence, time boundaries and next steps remain available. Existing clients keep their usual output by default. See [compact evidence output](docs/architecture.md#compact-evidence-output) for examples and client integration.

See [bus fields and TL-UL](https://github.com/gokeshenzhen/TraceWeave/blob/main/docs/architecture.md#packed-waveform-selections-and-tl-ul) for field configuration and detailed support limits.

### Custom Runtime Error Formats

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

## Tool Quick Reference

Usually, you describe the debugging goal and let the assistant select the tools. This table lists every tool by purpose; MCP tool definitions provide the parameters.

<table>
  <thead>
    <tr>
      <th>Category</th>
      <th>Tool</th>
      <th>Description</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td rowspan="3">Session and artifacts</td>
      <td><code>get_sim_paths</code></td>
      <td>Find simulation cases, compile logs, runtime logs, and waveforms</td>
    </tr>
    <tr>
      <td><code>get_formal_paths</code></td>
      <td>Find formal projects, logs, and exported waveforms; currently supports JasperGold</td>
    </tr>
    <tr>
      <td><code>get_diagnostic_snapshot</code></td>
      <td>Review collected debugging information and outstanding steps</td>
    </tr>
    <tr>
      <td rowspan="7">Hierarchy and source</td>
      <td><code>build_tb_hierarchy</code></td>
      <td>Build an RTL / testbench hierarchy view from compile records</td>
    </tr>
    <tr>
      <td><code>get_tb_subtree</code></td>
      <td>Browse a selected instance&#x27;s local hierarchy</td>
    </tr>
    <tr>
      <td><code>find_tb_instance</code></td>
      <td>Find instances by path or module name</td>
    </tr>
    <tr>
      <td><code>lookup_tb_files</code></td>
      <td>Find source files in the actual compiled file set</td>
    </tr>
    <tr>
      <td><code>get_tb_file_detail</code></td>
      <td>Inspect modules, interfaces, and classes defined in a source file</td>
    </tr>
    <tr>
      <td><code>get_tb_class_hierarchy</code></td>
      <td>Browse UVM / SystemVerilog class inheritance</td>
    </tr>
    <tr>
      <td><code>dump_tb_section</code></td>
      <td>Retrieve a complete section of hierarchy analysis data</td>
    </tr>
    <tr>
      <td rowspan="6">Logs and failures</td>
      <td><code>parse_sim_log</code></td>
      <td>Group runtime failures and extract timestamps and error summaries</td>
    </tr>
    <tr>
      <td><code>get_error_context</code></td>
      <td>Read the original log around an error</td>
    </tr>
    <tr>
      <td><code>diff_sim_failure_results</code></td>
      <td>Compare new, persistent, and resolved failures between runs</td>
    </tr>
    <tr>
      <td><code>analyze_failures</code></td>
      <td>Combine log and waveform context for a failure group</td>
    </tr>
    <tr>
      <td><code>analyze_failure_event</code></td>
      <td>Identify candidate instances, signals, and source files for one failure</td>
    </tr>
    <tr>
      <td><code>recommend_failure_debug_next_steps</code></td>
      <td>Recommend investigation targets and follow-up tool calls</td>
    </tr>
    <tr>
      <td>Static structural scanning</td>
      <td><code>scan_structural_risks</code></td>
      <td>Statically scan source code for suspicious structures; semantic mode checks ties, open inputs, and constant comparisons</td>
    </tr>
    <tr>
      <td rowspan="4">Signal tracing</td>
      <td><code>explain_signal_driver</code></td>
      <td>Trace a signal&#x27;s driver and relevant RTL</td>
    </tr>
    <tr>
      <td><code>find_signal_loads</code></td>
      <td>Find a signal&#x27;s consumers and potential impact</td>
    </tr>
    <tr>
      <td><code>trace_signal_path</code></td>
      <td>Query structural connectivity between two signals</td>
    </tr>
    <tr>
      <td><code>trace_x_source</code></td>
      <td>Trace X/Z propagation and inspect historical register sampling and hold behavior</td>
    </tr>
    <tr>
      <td rowspan="6">Waveform queries</td>
      <td><code>get_waveform_summary</code></td>
      <td>Inspect waveform format, duration, timescale, and top modules</td>
    </tr>
    <tr>
      <td><code>search_signals</code></td>
      <td>Find full signal paths by name</td>
    </tr>
    <tr>
      <td><code>get_signal_at_time</code></td>
      <td>Read a signal&#x27;s value at a specific time</td>
    </tr>
    <tr>
      <td><code>get_signal_transitions</code></td>
      <td>Inspect signal transitions within a time window</td>
    </tr>
    <tr>
      <td><code>get_signals_around_time</code></td>
      <td>Inspect multiple signals around a selected time</td>
    </tr>
    <tr>
      <td><code>get_signals_by_cycle</code></td>
      <td>Sample multiple signals on clock edges</td>
    </tr>
    <tr>
      <td rowspan="4">Differences and timing</td>
      <td><code>diff_first_divergence</code></td>
      <td>Find the first observed known-value difference between two signals</td>
    </tr>
    <tr>
      <td><code>trace_divergence</code></td>
      <td>Verify a waveform difference and trace relevant data, control, and prior state on both sides</td>
    </tr>
    <tr>
      <td><code>period</code></td>
      <td>Check signal periods and cadence anomalies</td>
    </tr>
    <tr>
      <td><code>verify_window</code></td>
      <td>Verify temporal conditions within a waveform window and return concrete evidence</td>
    </tr>
    <tr>
      <td rowspan="7">Protocols and transactions</td>
      <td><code>suggest_handshakes</code></td>
      <td>Discover valid/ready interfaces and signal bundles for inspection</td>
    </tr>
    <tr>
      <td><code>suggest_protocol_bundles</code></td>
      <td>Discover AHB / APB interface signal bundles</td>
    </tr>
    <tr>
      <td><code>sweep_handshakes</code></td>
      <td>Scan discovered valid/ready and AHB interfaces across the design and summarize anomalies</td>
    </tr>
    <tr>
      <td><code>inspect_handshake</code></td>
      <td>Check one interface for stalls, stability, and handshake anomalies</td>
    </tr>
    <tr>
      <td><code>reconstruct_transactions</code></td>
      <td>Reconstruct requests and responses to inspect latency, outstanding requests, and ordering</td>
    </tr>
    <tr>
      <td><code>resolve_packed_fields</code></td>
      <td>Identify packed bus fields to inspect address, data, and control signals individually</td>
    </tr>
    <tr>
      <td><code>inspect_tlul</code></td>
      <td>Check TL-UL requests and responses for handshake stalls, response latency, and outstanding requests</td>
    </tr>
    <tr>
      <td rowspan="3">Time cursors</td>
      <td><code>cursor_set</code></td>
      <td>Name a key timestamp for reuse in later queries</td>
    </tr>
    <tr>
      <td><code>cursor_list</code></td>
      <td>List time cursors in the current session</td>
    </tr>
    <tr>
      <td><code>cursor_delete</code></td>
      <td>Remove a time cursor</td>
    </tr>
    <tr>
      <td>EDA integration</td>
      <td><code>build_kdb</code></td>
      <td>Build and cache a Verdi KDB from compile records for NPI queries</td>
    </tr>
  </tbody>
</table>

## FAQ

**Can I use TraceWeave without a commercial license?**

Log analysis, VCD queries, static structural scanning, and Source Graph do not need a commercial license. **Direct value and transition queries on existing waveforms do not need an NPI license**: VCD uses the built-in parser, while FSDB uses local Verdi FSDB Reader libraries and the wrapper. Verdi NPI signal tracing and KDB builds require the corresponding EDA environment and license.

**Can missing path variables be recovered automatically?**

Yes. You can use a CLI agent in a terminal where the project's setup script has not been sourced, or give it logs and waveforms from a run launched in another terminal. TraceWeave can recover compilation input path variables from VCS compile logs and filelists when their values can be determined unambiguously, enabling hierarchy analysis, KDB builds, and Source Graph queries.

Recovery covers compilation input paths; the corresponding source and include files must remain accessible. The runtime prerequisites must already be available: `VERDI_HOME`, installed EDA tools, and license configuration for KDB builds, plus Python and `pyslang` dependencies for Source Graph.

**How should I judge signal-tracing accuracy?**

NPI uses an elaborated KDB matching the current design; Source Graph builds a semantic connectivity graph from source. With complete compilation context, supported target semantics, and **complete query coverage, exact resolution, and no truncation**, the results can be treated as **exact structural connectivity facts within the current query scope** for driver, load, and connectivity path analysis.

**Can I run static structural scans without simulation results?**

Yes. `scan_structural_risks` performs static source analysis without running a simulation or reading simulation run logs or VCD / FSDB waveforms. The current interface requires a compile/elaboration log (`compile_log`) and access to the corresponding sources and include files, so it can be used after compilation/elaboration, before running the simulation.

**How do I choose auto or deep for static structural scanning?**

The default `auto` mode runs source-text checks and reuses compatible semantic results already available. Ask the assistant to use `deep` when you need a new semantic scan for constant connections, open inputs, and similar facts. The `analysis_mode` parameter applies to each call; no client configuration change is needed. Findings are leads to investigate: legitimate tie-offs and protocol constants may also appear.

**Does a scan with no findings mean the design is correct?**

Conclusions depend on actual coverage. Missing signals, resource limits, or unsupported structures can limit analysis. Zero findings with zero or partial coverage do not establish that a design is free of problems. Tools report coverage and truncation so the assistant can decide whether to narrow the scope and investigate further.

**Can I inspect formal waveforms?**

You can query exported VCD / FSDB files and automatically discover JasperGold artifacts. Property results, trace classification, and reachability semantics must come from the formal tool or the user.

Standalone exports do not require a recognized formal project. When discovery finds waveforms without project entries, `get_formal_paths` includes a short hint for the existing point and batch readers; the caller selects the waveform, signals, and timestamp.

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
