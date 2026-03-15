# Architecture

## Dependency Graph

```mermaid
graph TD
  A[server.py] --> B[config.py]
  A --> C[src/path_discovery.py]
  A --> D[src/log_parser.py]
  A --> E[src/analyzer.py]
  A --> F[src/vcd_parser.py]
  A --> G[src/fsdb_parser.py]
  A --> H[src/fsdb_signal_index.py]
  A --> I[src/compile_log_parser.py]
  A --> J[src/tb_hierarchy_builder.py]
  A --> K[mcp.server / mcp.types]

  C --> B
  C --> I

  E --> D
  E --> B
  E --> F
  E --> G

  D --> B
  D --> L[custom_patterns.yaml]
  D --> M[PyYAML]

  H --> G
  H --> B

  G --> B
  G --> N[libfsdb_wrapper.so]
  G --> O[ctypes]
  G --> P[Verdi FSDB libs<br/>libnsys.so / libnffr.so]

  N --> Q[fsdb_wrapper.cpp]
  Q --> R[Verdi ffrAPI]

  S[build_wrapper.sh] --> Q
  S --> P

  T[tests/conftest.py] --> A
  U[tests/test_log_parser.py] --> D
  U --> B
  V[tests/test_fsdb_parser.py] --> G
  V --> B
  W[tests/test_analyzer.py] --> E
  W --> G
  W --> D
  X[tests/test_compile_log_parser.py] --> I
  Y[tests/test_server.py] --> A
  Z[tests/test_path_discovery.py] --> C
```

## Layering

```text
MCP interface
  server.py

Core logic
  src/path_discovery.py
  src/compile_log_parser.py
  src/tb_hierarchy_builder.py
  src/analyzer.py
  src/log_parser.py

Waveform backends
  src/vcd_parser.py
  src/fsdb_parser.py
  src/fsdb_signal_index.py

Native integration
  libfsdb_wrapper.so
  fsdb_wrapper.cpp
  Verdi ffrAPI/libs or repo-local runtime symlinks

Config and extension
  config.py
  custom_patterns.yaml

Verification
  tests/*
```

## Notes

- `server.py` is the composition root and runtime entry point.
- `src/path_discovery.py` is the path discovery layer for compile logs, sim logs, and waveforms.
- `src/compile_log_parser.py` and `src/tb_hierarchy_builder.py` provide compile-log-based structure extraction.
- `src/analyzer.py` depends on the shared parser interface implemented by `VCDParser` and `FSDBParser`.
- `src/fsdb_parser.py` is the Python/native boundary and resolves FSDB runtime from repo-local links first, then `VERDI_HOME`.
- `fsdb_wrapper.cpp` is the native/tool-vendor boundary.
