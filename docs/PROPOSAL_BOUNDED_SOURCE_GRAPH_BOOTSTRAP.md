# Proposal: Bounded Source Graph Bootstrap for Connectivity Queries

Status: implemented. The implementation preserves the full hierarchy as the
default panorama, removes its aggregate raw-source retention, and adds an
explicit positive-facts-only bootstrap for driver/load queries.

## Summary

`find_signal_loads` normally requires a successful `build_tb_hierarchy` call.
For a large compilation, hierarchy construction scans every user source file
before the Source Graph adapter can build its naturally bounded projection. In
the old implementation it also retained every raw source body in the hierarchy
handle. A simple connectivity request for one signal could therefore fail or
incur a large startup delay even when the target module was independently
available.

The implemented design adds a conservative, opt-in-by-routing fallback: when full
hierarchy construction is unavailable or exceeds a resource/time budget,
TraceWeave can build a **bounded bootstrap hierarchy** for a
single endpoint and execute a Source Graph load/driver query over that explicitly
limited scope.

The result must be labeled as scoped/incomplete unless the normal hierarchy
path later proves complete coverage.

## Problem

The public connectivity route has this prerequisite:

```text
build_tb_hierarchy -> find_signal_loads / explain_signal_driver / trace_signal_path
```

`build_tb_hierarchy` is intentionally rich: it scans the complete user file
set, builds module/class indexes, derives a component tree, and exposes browsing
handles. It now discards each raw source body after extracting compact facts.
That remains appropriate for hierarchy browsing
and whole-design analysis, but it is disproportionate for a request such as:

```text
"Find loads of <one hierarchical signal>."
```

Large compile logs can reference thousands of source files, packages, generated
files, and unavailable environment-dependent paths. In a constrained process,
the full scan may be slow or terminated before it produces a handle. The
existing Source Graph implementation is already designed to project only an
ancestor chain/cone, but it cannot start because its adapter requires the
full-hierarchy result as proof of that chain.

## Observed Behavior

For a large VCS-style compile log:

1. Full hierarchy construction began scanning a multi-thousand-file user set.
2. The process was terminated before a hierarchy handle was returned.
3. A manually constructed one-module Source Graph projection successfully
   returned positive load facts in roughly 1--2 seconds.
4. Its coverage correctly remained `inconclusive` because child modules and
   package dependencies outside that projection were not included.

This demonstrates that the expensive prerequisite, rather than the Source
Graph frontend/query itself, is the limiting factor for targeted connectivity
work.

## Goals

- Preserve the existing public APIs and normal complete-hierarchy behavior.
- Let a single-endpoint drive/load query make useful, provenance-correct
  progress when full hierarchy construction is unavailable.
- Never present a bounded result as a complete fanout/driver enumeration.
- Reuse existing Source Graph scope, coverage, fingerprint, cancellation, and
  fallback semantics where possible.
- Avoid retaining or projecting the full compile-input source content merely to
  answer a narrowly scoped positive query. If definition-to-file evidence is
  absent, a capped streaming inventory may still read the ordered inputs to
  prove uniqueness; it never retains those bodies or searches the filesystem.

## Non-goals

- Replacing `build_tb_hierarchy` for hierarchy browsing, UVM inspection, or
  whole-design structural analysis.
- Claiming cross-instance completeness without a proved ancestor chain.
- Bypassing content validation for the inputs actually used in a Source Graph
  artifact.
- Changing NPI precedence or the normal full-hierarchy Static fallback contract.

## Proposed Design

### 1. Add a bounded bootstrap context

The internal helper is:

```python
build_bounded_connectivity_context(
    compile_result: Mapping[str, Any],
    hierarchy_snapshot_sha256: str,
    signal_path: str,
    top_hint: str | None,
    config: BoundedBootstrapConfig,
) -> BoundedBootstrapResult
```

It performs only enough work to prove a safe local scope:

1. Parse compile evidence with the streaming compile-log parser and reuse the
   compact context cached by an attempted full build when available.
2. Resolve the selected top/module from explicit `top_hint`, compile evidence,
   or a uniquely supported target prefix.
3. Identify the target definition and the direct source inputs needed to parse
   it. This uses paired simulator evidence when available; otherwise it runs a
   byte/file/time-capped lexical inventory over ordered compile inputs, never a
   broad filesystem search.
4. Build a minimal component tree containing the proved ancestor chain. If the
   chain cannot be proved, stop with a structured blocker instead of guessing.
5. Build a Source Graph request with an explicit coverage boundary and an
   objective exclusion such as `bootstrap_hierarchy_scoped`.

The bootstrap must use real, content-fingerprinted inputs. It must not invent a
top/module relationship from a filename alone.

### 2. Route only after the ordinary hierarchy path is unavailable

The current route stays unchanged when a compatible full hierarchy handle is
present. Bootstrap is used only when no compatible handle exists and the caller
explicitly sets `allow_bounded_bootstrap=true` on
`explain_signal_driver` or `find_signal_loads`.

The public argument is:

```text
allow_bounded_bootstrap: boolean = false
```

It defaults to `false`, so existing workflow behavior is unchanged. A
structured hierarchy resource/timeout blocker caches enough compile context for
the explicit retry, but does not silently switch modes.

### 3. Make incompleteness unmistakable

All bootstrap results carry additive receipt fields such as:

```json
{
  "bootstrap_context": {
    "used": true,
    "scope": "single_endpoint",
    "ancestor_chain_proved": true,
    "coverage_status": "inconclusive",
    "objective_exclusions": [
      "bootstrap_compile_inputs_scoped",
      "bootstrap_hierarchy_scoped"
    ]
  }
}
```

Rules:

- Positive, IR-proved facts may be returned with their normal Source Graph
  provenance.
- `exhaustive_search` must be `false` unless the normal hierarchy/coverage
  contract proves otherwise.
- A no-match must not be reported as "no loads" or "no driver". It returns an
  honest no-fact Source Graph receipt and does not launch Legacy Static, because
  a full-source regex scan would violate the route's resource bound.
- `trace_signal_path` and `trace_x_source` remain excluded.

### 4. Bound resource use explicitly

Bootstrap work has independently configurable caps:

```text
TRACEWEAVE_BOOTSTRAP_MAX_SOURCE_INPUTS=32
TRACEWEAVE_BOOTSTRAP_MAX_SOURCE_BYTES=16777216
TRACEWEAVE_BOOTSTRAP_TIMEOUT=15
TRACEWEAVE_BOOTSTRAP_MAX_INVENTORY_FILES=4096
TRACEWEAVE_BOOTSTRAP_MAX_INVENTORY_BYTES=268435456
TRACEWEAVE_BOOTSTRAP_MAX_INCLUDE_DEPTH=16
TRACEWEAVE_BOOTSTRAP_MAX_HIERARCHY_DEPTH=64
```

Exceeding a cap must return a fixed structured blocker, not silently expand to
a full scan. The receipt should state only fixed labels and numeric counts;
do not expose user paths, source names, or source contents in telemetry.

The full hierarchy also has optional, default-disabled guardrails:

```text
TRACEWEAVE_HIERARCHY_TIMEOUT=0
TRACEWEAVE_HIERARCHY_MAX_SOURCE_BYTES=0
```

A nonzero hit returns `build_status="blocked"` with a fixed blocker and no
handle. It never registers a partial panorama.

## Implementation

Implemented areas:

- `src/compile_log_parser.py`: streamed VCS/Xcelium transcript parsing and
  paired Xcelium definition evidence.
- `src/tb_hierarchy_builder.py`: compact scans, per-parent-module instance maps,
  cancellation checkpoints, and numeric build/RSS metrics.
- `src/bounded_hierarchy_bootstrap.py`: proof, closure, and hard resource caps.
- `src/source_graph_adapter.py`: scoped compile subset admission, incomplete
  manifest/exclusion semantics, removal of broad `-v`/`-y` library replay, and
  no cross-request bootstrap reuse.
- `server.py`: structured full-build blockers, compact compile-context cache,
  explicit routing, positive-only admission, and no unbounded Static retry.
- `src/schemas.py`: additive build and bootstrap receipts.
- focused parser/hierarchy/bootstrap/routing/resource-control tests and
  `scripts/benchmark_hierarchy_bootstrap.py`.

The Source Graph runtime, IR query engine, and worker protocol remain unchanged.

## Reproduction

This can be reproduced entirely with synthetic fixtures; no customer/project
source is required.

### Fixture layout

Create a test fixture with:

- one top module containing a local assignment,
- many unrelated small source files referenced by a VCS-style filelist or
  compile log,
- optional unresolved child/package references so coverage is intentionally
  incomplete.

Example target module:

```systemverilog
module target_top(input logic a, input logic b, output logic y);
  logic gate_en;
  assign gate_en = a & b;
  assign y = gate_en;
endmodule
```

Target query:

```text
find_signal_loads(signal_path="target_top.gate_en", ...)
```

Expected positive fact:

```text
load_path = target_top.y
kind = rhs_expr
source_info_origin = source_graph
```

### Regression tests

1. **Full hierarchy remains preferred**
   - Build a normal hierarchy fixture first.
   - Verify the existing full-context Source Graph route is selected and no
     bootstrap receipt is present.

2. **Bootstrap produces a positive fact**
   - Make full hierarchy construction return a controlled resource blocker.
   - Enable bounded bootstrap.
   - Verify the positive load above is returned by Source Graph.
   - Verify `bootstrap_context.used=true` and
     `claim_semantics.exhaustive_search=false`.

3. **Bootstrap no-match is never a clean negative**
   - Query a signal with no local proven load.
   - Verify Source Graph does not claim an exhaustive negative and Legacy Static
     is not invoked.

4. **Unsafe scope is blocked**
   - Use an ambiguous top/module or an unproved hierarchical prefix.
   - Verify a fixed bootstrap blocker is returned and no source worker starts.

5. **Resource caps and cancellation**
   - Exceed each bootstrap cap independently.
   - Verify no full hierarchy scan begins and cancellation prevents worker
     launch or exits promptly.

## Acceptance Criteria

- A targeted local load/driver query can return a Source Graph positive fact
  when complete hierarchy construction is unavailable.
- The result never claims complete coverage, a complete negative, or a complete
  fanout under bootstrap-only scope.
- Existing normal hierarchy and NPI routing tests remain unchanged/passing.
- New tests demonstrate positive facts, safety blockers, no negative claim or
  unbounded fallback, resource controls, and cancellation-compatible routing.
- A benchmark reports both the full-hierarchy baseline and the bounded
  bootstrap path on the same synthetic large-filelist fixture, including wall
  time and peak RSS.

## Measured Result

The reproducible default fixture matches the report: a 51 MiB / 200,000-line
compile log, a 658 KiB / 8,913-line elaboration log, and 3,843 sources. On the
development host with 16 KiB per source (about 60 MiB total), the implemented
full hierarchy completed in about 0.58 s after compile parsing, peaked near
28.2 MiB RSS, and retained zero raw-source bytes. Bounded bootstrap completed
its conservative all-input definition inventory and selected one source in
about 0.38 s, peaking near 23.1 MiB RSS.

With 64 KiB per source (about 240 MiB total), full hierarchy completed in about
1.79 s and still peaked near 28.1 MiB RSS; bootstrap took about 0.54 s and
peaked near 23.4 MiB. The pre-change 64 KiB hierarchy baseline retained about
240.2 MiB of source text and peaked near 262.3 MiB RSS. Measurements are local
synthetic runs, not a guarantee for network filesystems or an unknown external
27-second watchdog.

## Trade-offs

The bootstrap route trades global completeness for responsiveness. That is
appropriate only when the returned facts are explicitly positive and their
scope/coverage limitations remain visible. It should not become a silent
replacement for the normal hierarchy build.
