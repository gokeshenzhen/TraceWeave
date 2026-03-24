# Architecture

## System Shape

Waveform MCP is a workflow-oriented debug server. The core architecture is not
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

Waveform backends
  src/vcd_parser.py
  src/fsdb_parser.py
  src/fsdb_signal_index.py

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
- `src/tb_hierarchy_builder.py` and `src/signal_driver.py` turn the system into
  a source-aware debug assistant rather than a parser-only tool.
- `src/structural_scanner.py` and `src/x_trace.py` are first-class extended
  analysis capabilities and should not be treated as optional side scripts.
- `src/schemas.py` and `src/problem_hints.py` are support layers for structured
  output contracts and lightweight analysis annotations.
- `src/fsdb_parser.py` is the Python/native boundary and resolves FSDB runtime
  from repo-local links first, then `VERDI_HOME`.
