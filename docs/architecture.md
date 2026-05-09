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
