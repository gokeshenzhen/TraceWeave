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
  - session state / prerequisite checks
  - diagnostic snapshot and result caching

Core log and failure analysis
  src/path_discovery.py
  src/compile_log_parser.py
  src/log_parser.py
  src/analyzer.py

Source-aware structure analysis
  src/tb_hierarchy_builder.py
  src/signal_driver.py
  src/signal_load.py

Connectivity backends (driver/load resolution)
  src/connectivity_backend.py     # protocol + Static + select_backend
  src/verdi_npi_backend.py        # Verdi NPI backend, lazy, license-tolerant
  src/verdi_backend.py            # KDB / license probe, kdb_hint generator
  src/kdb_builder.py              # Auto-build Verdi KDB (vericom + elabcom) for Xcelium

Waveform backends
  src/vcd_parser.py
  src/fsdb_parser.py
  src/fsdb_signal_index.py
  src/cycle_query.py
  src/waveform_batch.py           # FSDB+VCD batch reader (time-window)

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
  src/txn_reconstruct.py          # reconstruct_transactions: id-correlated transaction layer

Native integration
  libfsdb_wrapper.so
  fsdb_wrapper.cpp
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

- `server.py` is both the composition root and the workflow gate; tool ordering,
  prerequisite enforcement, session-compatible cache reuse, and in-process
  parsed-log snapshots for same-path simulation reruns live there.
- `src/path_discovery.py`, `src/compile_log_parser.py`, `src/log_parser.py`, and
  `src/analyzer.py` form the main failure-analysis path from artifacts to
  normalized failures and recommended next steps.
- `src/tb_hierarchy_builder.py`, `src/signal_driver.py` and `src/signal_load.py`
  turn the system into a source-aware debug assistant rather than a parser-only
  tool. `signal_driver` traces back to drivers; `signal_load` finds the
  consumers (fanout) of a signal.
- `src/connectivity_backend.py` defines a `ConnectivityBackend` protocol with
  `find_driver`, `find_loads`, and `find_path` methods. `select_backend()`
  returns `VerdiNpiBackend` when a Verdi KDB is available, otherwise the
  static source-regex backend. The NPI backend wraps Static internally, so
  any per-call NPI failure (license unavailable, KDB stale, query exception)
  silently degrades for driver/load queries — the dispatch layer sees one
  protocol regardless. `find_path` is NPI-only: Static returns
  `unsupported_reason="static_backend_no_path_api"` rather than approximating
  with regex, since `sig_to_sig_conn_list` walks the elaborated netlist
  across assigns / interfaces / generates that source-regex cannot follow
  reliably.
- `src/verdi_backend.py` is a pure-detection probe: it locates KDB at
  `simv.daidir/kdb.elab++` (VCS two-step) or via `synopsys_sim.setup` work-lib
  mappings (three-step / vericom standalone) and emits a per-simulator
  `kdb_hint` (e.g. the exact `vcs -kdb=only` command for a VCS user, the
  `vericom -kdb` command for an Xcelium user) when KDB is missing.
- `src/verdi_npi_backend.py` lazily imports `pynpi` from `$VERDI_HOME` (zero
  hardcoded prefixes), holds a single design across calls keyed on
  `kdb_path`, and re-issues `npisys.load_design` to switch cases within one
  session. Synthesized PinHdl paths (`scope:Construct#Op:line:line:Cell.Port`)
  are normalized to FSDB-visible scopes; raw form is preserved in `expr` for
  diagnostics. Additional NPI-only capabilities: `find_path` wraps
  `sig_to_sig_conn_list` for the `trace_signal_path` MCP tool, and
  `collect_instance_src_map` walks `netlist.get_top_inst_list()` recursively
  to overlay elaborated `file:line` onto compile-log-derived hierarchy
  nodes; `LoadHop` / `DriverChainHop` / hierarchy nodes carry a
  `source_info_origin` field (`"compile_log"` vs `"npi"`) so consumers can
  tell which provenance produced each `file:line`.
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
  from repo-local links first, then `VERDI_HOME`.
- `src/waveform_batch.py` provides `WaveformBatchReader` — a time-window
  multi-signal reader with FSDB and VCD implementations sharing the same
  shape. The FSDB path uses `ffrCreateTimeBasedVCTrvsHdl` for a single
  chronological walk; the VCD path is pure Python.

## Handle-based Hierarchy Access

`build_tb_hierarchy` generates a full hierarchy result server-side (project
metadata, grouped file list, complete `component_tree`, `class_hierarchy`,
raw `compile_result`, per-file scan results) but returns only a **slim
payload** to the LLM: project, stats, depth-2 `tree_skeleton`, interfaces,
`ambiguous_basenames`, and a content-addressed `hierarchy_handle`. The
full result is registered in an in-process `HandleStore`
(`src/hierarchy_handles.py`) keyed by the handle.

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

## Connectivity Backend Cooperation (NPI vs Static)

NPI is the **deep / accurate** path; Static is the **cheap fallback** that
runs when NPI cannot be loaded. Once NPI is loaded, queries stay in NPI even
when the result is `unsupported` — the backend tag in the response always
reflects who actually answered.

```text
select_backend(probe_status)
├── KDB present  → VerdiNpiBackend(fallback=Static)
└── KDB absent   → Static directly  (don't start NPI just to burn a license)

VerdiNpiBackend.find_driver / find_loads / find_path
├── parse_compile_log fails / no kdb_path / no top   → fall back to Static
├── _ensure_loaded fails (pynpi import, npisys.init, load_design rc != 1)
│                                                    → fall back to Static
├── top-level exception                              → fall back to Static
└── _npi_find_driver  (NPI happy path; backend="verdi_npi" in every branch)
    ├── net resolve fails             → backend="verdi_npi", stopped_at="signal_path_unresolved_in_npi"
    ├── driver_list raises            → backend="verdi_npi", stopped_at="npi_driver_list_failed"
    ├── driver_list empty             → backend="verdi_npi", stopped_at="no_npi_drivers"
    ├── boundary-only drivers OR recursive=True
    │       → net.fan_in_reg_list(stop_at_pin, report_primary_port, top_scope_name)
    │       ├── fan_in succeeds       → build driver_chain (queried + boundary points)
    │       └── fan_in raises         → fall through to single-hop formatting (still NPI)
    └── normal driver                 → single-hop format
```

**Key properties:**

- Static appears in answers only when NPI could not be loaded at all (KDB
  missing, license unavailable, pynpi import failure). Inside the NPI happy
  path Static is never consulted.
- The "boundary-only" detection upgrades dead-end results (where
  `driver_list` returns the queried net's own hierarchy port — i.e. no
  synthesized cell tag, no `:` in the name) to a `fan_in_reg_list` walk,
  which transparently crosses module port boundaries on the elaborated
  netlist. This is why NPI can resolve drivers that Static cannot reach.
- `top_scope_name` for fan-in is derived from `signal_path.split(".", 1)[0]`
  — driven by the query, not by project-specific config — so the bound is
  correct across designs without hardcoding any top name.
- For Xcelium / `xrun` flows there is no KDB by default. NPI requires a
  separate `vericom -kdb` + `elabcom -elab kdb` pass over the same
  sources. When `AUTO_KDB_BUILD` is on (default), TraceWeave's
  `build_kdb` MCP tool will run those two commands for the user; the
  Static fallback is only used while no KDB exists yet.

## Auto-KDB build for Xcelium (`build_kdb` tool)

When the active simulator is Xcelium and the KDB probe finds nothing,
the diagnostic snapshot lists `build_kdb` in `missing_steps`. Calling
`build_kdb(compile_log=...)` runs vericom + elabcom against the file
list, defines, and include paths parsed out of the compile log, and
caches the resulting KDB under a project-agnostic cache root.

```text
build_kdb(compile_log)
├── parse_compile_log → top, files, defines, incdirs, UVM flag
├── hash = sha256(top + sorted(files + mtimes) + sorted(defines)
│                 + sorted(incdirs) + uvm_bit)
├── cache_dir = $TRACEWEAVE_CACHE_DIR/kdb/<hash>/
├── if cache_dir/state.json says ok → return cached, no Verdi spawn
└── else build in $TRACEWEAVE_CACHE_DIR/kdb/.tmp-<hash>-<pid>/
    ├── write build.sh (regenerated every rebuild; runnable standalone)
    ├── vericom -sv -kdb [-ntb_opts uvm] [+define+...] [+incdir+...]
    │           <files in compile order> -top <top>
    │   → vericom.log
    ├── elabcom -lib work.lib++ -elab kdb -top <top>
    │   → elabcom.log
    ├── on success: rename tmp → cache_dir (atomic, replaces stale entry)
    └── on failure: rename tmp → .failed-<hash>/ (preserved for inspection;
                      existing cache_dir untouched)
```

Cache layout under `$TRACEWEAVE_CACHE_DIR/kdb/<hash>/`:

| File / dir | Purpose |
|---|---|
| `kdb.elab++/` | Elaborated KDB. NPI's `-simflow -dbdir` target. |
| `work.lib++/` | vericom source-lib output. |
| `build.sh` | Runnable reproducer; written every build. Lets users see/run the exact vericom+elabcom commands TraceWeave invoked. |
| `vericom.log` | stdout+stderr of vericom phase. |
| `elabcom.log` | stdout+stderr of elabcom phase. |
| `state.json` | Inputs hash, status (`ok`/`failed`), timestamps. |

The probe picks up these cached KDBs automatically (`kdb_flow:
"traceweave_cached"`), so the same find_driver / find_loads call that
falls back to Static today starts answering through NPI after one
`build_kdb` invocation. User-managed KDBs (`simv.daidir/kdb.elab++` or
`vericom`-built `*.lib++` in the user's work dir) still win the probe
order — TraceWeave's cache is the fallback, never the override.

Cross-environment generality:

- All inputs (top, files, defines, incdirs) come from the generic
  `compile_result` shape, not from any project-specific paths.
- Include-path syntax `+incdir+<path>` (VCS) **and** `-incdir <path>`
  (xrun) are both extracted.
- UVM detection is heuristic: `-ntb_opts uvm`, `-uvm`,
  `+define+UVM*`, or any source path containing `uvm`. Any one
  signal triggers `-ntb_opts uvm` for vericom.
- Top-module selection prefers names not matching
  `uvm_custom_install*` (Synopsys recorder shims), falling back to
  the first listed top.
- `VERDI_HOME` provides tool paths; no hardcoded install prefixes.
- Cache root honours `TRACEWEAVE_CACHE_DIR`, then `XDG_CACHE_HOME`,
  then `~/.cache/traceweave/`.

`AUTO_KDB_BUILD` defaults to True. Set `TRACEWEAVE_AUTO_KDB=0` (or
`false`/`no`/`off`) to disable the snapshot suggestion. The
`build_kdb` MCP tool itself is always callable.

VCS flows are not auto-built. Recompiling with `-kdb=only` is a
one-line change to the existing compile command and reuses the VCS
license token, so the verdi_backend hint surfaces that command
verbatim instead of suggesting `build_kdb`.

## Usage Telemetry (`src/usage_telemetry.py`)

Passive, local-only instrumentation built to answer the auto-debug v2
retrospective's open question with data rather than guesses: *how often
are the shipped primitives (cursor / period / diff_first_divergence)
actually used on real workloads, and in what fraction of debug sessions?*

- `server.call_tool` is the single choke point every tool call passes
  through. It wraps `_dispatch` in a `finally` that calls
  `usage_telemetry.record_call(...)`, appending one JSONL line per call to
  `$TRACEWEAVE_CACHE_DIR/telemetry/usage.jsonl`.
- Each line records: timestamp, `session_id`, `case` (case-dir basename),
  tool name, **argument keys + a small whitelist of scalar flags** (never
  argument values or paths — noise + privacy), `ok`/`blocked`, `result_bytes`
  (a token proxy), and `latency_ms`.
- **A session = a `get_sim_paths` case.** The get_sim_paths handler calls
  `note_session(identity)`; a new case identity mints a new `session_id`,
  re-discovering the same case keeps it. This makes "sessions in which a
  primitive was used at least once" a meaningful presence metric.
- Recording is strictly best-effort — every public function swallows its own
  exceptions so telemetry can never break a tool call.
- `aggregate(records)` is a pure function (per-tool counts, ok-rate,
  per-session distributions, tracked-feature presence) backing the offline
  `scripts/telemetry_report.py` CLI; it is deliberately NOT an MCP tool.

`TELEMETRY_ENABLED` defaults to True. Opt out with `TRACEWEAVE_TELEMETRY=0`
(or `false`/`no`/`off`). Telemetry is local-only; nothing is sent anywhere.
