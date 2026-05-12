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

Waveform backends
  src/vcd_parser.py
  src/fsdb_parser.py
  src/fsdb_signal_index.py
  src/cycle_query.py
  src/waveform_batch.py           # FSDB+VCD batch reader (time-window)

Extended analysis capabilities
  src/structural_scanner.py
  src/x_trace.py

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
  prerequisite enforcement, and session-compatible cache reuse live there.
- `src/path_discovery.py`, `src/compile_log_parser.py`, `src/log_parser.py`, and
  `src/analyzer.py` form the main failure-analysis path from artifacts to
  normalized failures and recommended next steps.
- `src/tb_hierarchy_builder.py`, `src/signal_driver.py` and `src/signal_load.py`
  turn the system into a source-aware debug assistant rather than a parser-only
  tool. `signal_driver` traces back to drivers; `signal_load` finds the
  consumers (fanout) of a signal.
- `src/connectivity_backend.py` defines a `ConnectivityBackend` protocol with
  `find_driver` and `find_loads` methods. `select_backend()` returns
  `VerdiNpiBackend` when a Verdi KDB is available, otherwise the static
  source-regex backend. The NPI backend wraps Static internally, so any
  per-call NPI failure (license unavailable, KDB stale, query exception)
  silently degrades — the dispatch layer sees one protocol regardless.
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
  diagnostics.
- `src/structural_scanner.py` and `src/x_trace.py` are first-class extended
  analysis capabilities and should not be treated as optional side scripts.
- `src/schemas.py` and `src/problem_hints.py` are support layers for structured
  output contracts and lightweight analysis annotations.
- `src/fsdb_parser.py` is the Python/native boundary and resolves FSDB runtime
  from repo-local links first, then `VERDI_HOME`.
- `src/waveform_batch.py` provides `WaveformBatchReader` — a time-window
  multi-signal reader with FSDB and VCD implementations sharing the same
  shape. The FSDB path uses `ffrCreateTimeBasedVCTrvsHdl` for a single
  chronological walk; the VCD path is pure Python.

## Connectivity Backend Cooperation (NPI vs Static)

NPI is the **deep / accurate** path; Static is the **cheap fallback** that
runs when NPI cannot be loaded. Once NPI is loaded, queries stay in NPI even
when the result is `unsupported` — the backend tag in the response always
reflects who actually answered.

```text
select_backend(probe_status)
├── KDB present  → VerdiNpiBackend(fallback=Static)
└── KDB absent   → Static directly  (don't start NPI just to burn a license)

VerdiNpiBackend.find_driver / find_loads
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
  separate `vericom -kdb` pass over the same sources; without that,
  `select_backend()` returns Static directly and the kdb_hint surfaces
  the exact `vericom -kdb` command to run.
