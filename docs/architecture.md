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
  A --> K[src/signal_driver.py]
  A --> L[mcp.server / mcp.types]

  C --> B
  C --> I

  E --> D
  E --> B
  E --> F
  E --> G

  D --> B
  D --> M[custom_patterns.yaml]
  D --> N[PyYAML]

  K --> I
  K --> J

  H --> G
  H --> B

  G --> B
  G --> O[libfsdb_wrapper.so]
  G --> P[ctypes]
  G --> Q[Verdi FSDB libs<br/>libnsys.so / libnffr.so]

  O --> R[fsdb_wrapper.cpp]
  R --> S[Verdi ffrAPI]

  T[build_wrapper.sh] --> R
  T --> Q

  U[tests/conftest.py] --> A
  V[tests/test_log_parser.py] --> D
  V --> B
  W[tests/test_fsdb_parser.py] --> G
  W --> B
  X[tests/test_analyzer.py] --> E
  X --> G
  X --> D
  Y[tests/test_compile_log_parser.py] --> I
  Z[tests/test_server.py] --> A
  AA[tests/test_path_discovery.py] --> C
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
  src/signal_driver.py

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
- `src/signal_driver.py` is a lightweight source-link layer built on compile-log discovery and source scanning.
- `src/fsdb_parser.py` is the Python/native boundary and resolves FSDB runtime from repo-local links first, then `VERDI_HOME`.
- `fsdb_wrapper.cpp` is the native/tool-vendor boundary.
