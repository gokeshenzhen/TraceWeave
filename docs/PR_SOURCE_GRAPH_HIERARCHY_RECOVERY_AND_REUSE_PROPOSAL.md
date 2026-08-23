# PR Proposal: Full-Hierarchy Recovery Across Imperfect Include Contexts, Safe Instance Admission, and Source Graph Build Reuse

**Status:** Implemented and validated as of 2026-08-23; the original hierarchy
failure and cold-build timeout are resolved, while persistent semantic-session
reuse remains a guarded opt-in pending broader operational evidence.

**Scope:** TraceWeave hierarchy construction and Source Graph production routing

**Privacy note:** This document deliberately uses only anonymized names and synthetic paths. It contains no customer/project names, user names, machine paths, test names, source file paths, waveform paths, repository URLs, or proprietary signal/module names. All examples are representative abstractions of a locally authorized RTL/simulation-debugging case.

---

## 1. Why this proposal exists

A real production-sized VCS/UVM simulation case exposed a failure chain in which TraceWeave could not analyze a deep DUT signal through the Source Graph route—not because the signal was absent, and not because the Source Graph query logic was inherently incapable, but because the hierarchy evidence delivered to Source Graph was incomplete or discarded at several earlier layers.

The observed user-facing result was an honest but unhelpful refusal similar to:

```text
instance_not_in_projected_scope
```

TraceWeave was correct not to invent hierarchy. However, the source needed to prove that hierarchy was already locally available in the compilation set. The failure was therefore a hierarchy recovery and lifecycle problem, not a legitimate absence of evidence.

This document proposes and justifies a focused correctness patch set with four goals:

1. Preserve a valid completed hierarchy during same-case discovery operations.
2. Resolve full hierarchy handles correctly for split compile/elaboration flows.
3. Recover real module instances contained in successfully expanded include fragments even when an unrelated include problem makes the whole preprocessing context incomplete.
4. Prevent the expanded-text recovery path from admitting false module instances extracted from UVM/helper code.

The proposal also records the next issue discovered after the correctness fixes: **large Source Graph builds can time out while preparing a driver query even after hierarchy scope resolution succeeds.** That performance problem is intentionally described as follow-up work, not silently folded into the correctness change.

---

## 2. Problem statement

### 2.1 Target shape

The failing target was a scalar control/status signal nested in a deep DUT hierarchy. Names are anonymized below:

```text
tb_top.u_chip_wrap.u_digital_wrap.u_core_wrap.u_active_wrap.
u_subsystem_wrap.u_subsystem.u_receiver.target_status
```

The intended hierarchy was structurally ordinary:

```text
tb_top
└─ u_chip_wrap        : chip_wrapper
   └─ u_digital_wrap  : digital_wrapper
      └─ u_core_wrap
         └─ u_active_wrap
            └─ u_subsystem_wrap
               └─ u_subsystem
                  └─ u_receiver
                     └─ target_status
```

The top module's DUT instance was not written directly in the top source body. It lived in an included connection fragment selected by normal preprocessor conditionals:

```systemverilog
`ifdef FPGA_BUILD
  fpga_top u_chip_wrap(...);
`else
  chip_wrapper u_chip_wrap(.*);
`endif
```

The production compile configuration did **not** define `FPGA_BUILD`, so the non-FPGA `else` branch was active and represented the actual DUT hierarchy.

### 2.2 Original observed hierarchy

Before the proposed fixes, `build_tb_hierarchy` returned a suspiciously shallow tree:

```text
module_count: 7
instance_count: 10
tree_depth: 4
```

Only helper/verification objects directly visible in the top file were present. The DUT instance `u_chip_wrap` was absent, so a Source Graph request could only prove the root:

```text
hierarchy_resolution.status: truncated
max_matched_instance_count: 1
first_stop_depth: 1
```

The Source Graph adapter correctly refused to project an unproved child chain and produced a structured scope blocker. Static fallback could not resolve the deeply dotted target as a simple source symbol, so it returned unsupported rather than inventing a driver/load result.

### 2.3 Why this matters

This is not a cosmetic hierarchy-display issue. The `component_tree` is the proof boundary that Source Graph uses to decide which instance paths it is permitted to project. A missing DUT instance blocks every downstream driver/load/path query under that DUT, even when the source file containing the real instantiation was compiled and locally available.

---

## 3. Root-cause analysis

The failure consisted of three separate issues that interacted.

### 3.1 A completed full hierarchy could be invalidated by same-case path discovery

`get_sim_paths` is conceptually a read-only discovery operation. It locates logs and waveforms and records session context. It does not modify compilation sources, compile options, file ordering, elaboration inputs, or the hierarchy itself.

However, it was configured as an upstream invalidator for `build_tb_hierarchy`. Calling it after a successful hierarchy build cleared the hierarchy state and invalidated the in-process handle store.

A natural user sequence therefore failed unnecessarily:

```text
1. build_tb_hierarchy
2. get_sim_paths (same case; perhaps to attach FSDB)
3. explain_signal_driver / find_signal_loads
```

At step 3, the completed hierarchy was gone. The connectivity route then attempted bounded bootstrap instead of using the more complete hierarchy already built earlier in the session.

A true case change needs to clear state. Same-case rediscovery does not.

### 3.2 Split compile/elaboration hierarchy handles were not reliably rediscovered

The full hierarchy was built from a primary compile log plus a supplementary elaboration log. This is expected in split VCS-style flows: compile and elaboration evidence together define the correct hierarchy context.

The hierarchy handle incorporates supplementary logs. But a later lookup fallback used only a bare identity equivalent to:

```python
compute_handle(compile_log, simulator)
```

This cannot reproduce the identity of a hierarchy built from:

```python
compute_handle(
    compile_log,
    simulator,
    supplementary_compile_logs=[elaboration_log],
)
```

Therefore even if a valid full hierarchy existed, a fallback lookup could miss it unless exact in-memory provenance still happened to carry the original handle.

### 3.3 The hierarchy scanner threw away successfully expanded include evidence

This was the direct cause of the missing DUT instance.

`scan_preprocessed_sv()` first scans the root source text. It can then replace module-instance data with a scan of include-expanded text. Before this proposal, the replacement was conditioned on total preprocessor completeness:

```python
if source.complete:
    result["module_instances"], result["module_instance_map"] = \
        _extract_module_instances(_strip_comments(source.text))
```

This condition is too coarse.

The include-expanded source text has the following useful properties:

- It begins from the root source.
- It includes every include that was successfully resolved.
- It evaluates the active `ifdef` / `ifndef` / `elsif` / `else` branches.
- It contains the real DUT instantiation in the selected include fragment.

The root-only text does **not** contain the instance inside the include fragment.

An unrelated include problem elsewhere in the same preprocessing closure sets `source.complete=False`. Under the old behavior, this discarded all include-expanded instance data, including the successfully expanded DUT connection fragment, and reverted to root-only extraction.

That creates a counterproductive outcome:

> One unrelated missing/mismatched include makes TraceWeave ignore a successfully resolved active include that contains the exact DUT instance needed for hierarchy proof.

### 3.4 Removing the completeness gate exposed a false-positive parser path

The immediate correction is to always use include-expanded text for instance extraction. But that change revealed a separate weakness in the lightweight tokenizer/parser.

The instance parser intentionally recognizes an approximate SystemVerilog pattern:

```text
identifier [#(...)] identifier (...);
```

The tokenizer omits many operators, including assignment operators. Thus ordinary helper/UVM expressions can collapse into an instance-shaped token sequence.

For example, source resembling:

```systemverilog
bit matched = uvm_is_match(re, s);
```

may look to the simplified parser like:

```text
matched uvm_is_match ( re , s ) ;
```

Similarly:

```systemverilog
arr = new(item_a, item_b);
```

can become an identifier sequence that resembles a type plus instance name plus parentheses.

After expanded UVM/helper include content entered the lexical extent of the top module, this produced false hierarchy nodes such as generic short identifiers, helper function names, and constructor-like calls.

The hierarchy builder therefore needed a second trust boundary: not every token sequence that looks like an instance is a hierarchy instance.

---

## 4. Proposed change set

### 4.1 Preserve full hierarchy across same-case `get_sim_paths`

#### Proposed behavior

Remove `build_tb_hierarchy` from the normal downstream invalidation list of `get_sim_paths`.

#### Required retained behavior

A genuine case change must still clear all state. The existing session identity comparison already detects a different verification root/case/compile context and invokes the stronger global cleanup path.

#### Rationale

The invalidation policy should distinguish these two cases:

| Situation | Correct action |
|---|---|
| Same case re-discovered; same compile identity | Keep hierarchy/handle state. |
| Different case or compile identity | Clear hierarchy, handle store, result cache, and dependent state. |

This preserves the intuitive workflow while retaining stale-data protection for real case changes.

#### Required tests

1. Build hierarchy, rediscover the same case, then verify hierarchy state remains available.
2. Discover a different case, then verify hierarchy state and handle store are invalidated.
3. Preserve existing prerequisite behavior for consumers that require a hierarchy.

---

### 4.2 Make hierarchy lookup supplementary-log aware

#### Proposed behavior

When resolving a hierarchy context from stored build provenance/session state, use the known supplementary compile/elaboration logs to derive the same handle identity used by the builder.

The resolution order should be:

1. Exact stored hierarchy handle, if present and valid.
2. Recomputed supplement-aware handle from stored matching provenance.
3. Bare compile-log handle only as a legacy/single-log fallback.

#### Rationale

The supplementary logs are not incidental metadata. They are part of the semantic evidence used to construct the hierarchy. A fallback that ignores them cannot safely identify the same artifact.

#### Required tests

1. A hierarchy built with primary + supplementary logs is found again through resolution logic.
2. A bare primary-log lookup does not accidentally alias a split-log hierarchy.
3. Existing single-log behavior remains compatible.

---

### 4.3 Always use include-expanded text for module-instance extraction

#### Proposed behavior

In `scan_preprocessed_sv`, extract instances from the include-expanded `source.text` regardless of `source.complete`.

Continue to expose:

```text
include_context_complete
include_resolution_issues
```

so no caller can confuse an imperfect preprocessing closure with a globally complete build context.

#### Rationale

The expanded source is a strict information improvement over root-only source for hierarchy extraction:

- It contains the root source plus successful include expansions.
- It selects the actual conditional branch.
- It never fabricates the contents of an unresolved include.

It is therefore safe and necessary to use it for **positive local hierarchy discovery**. Any uncertainty remains recorded in coverage/diagnostic receipts, and Source Graph still must not make an exhaustive negative claim from incomplete context.

#### Important semantic distinction

This change does **not** mean:

> An incomplete include context is now treated as fully sound for all queries.

It means only:

> Successfully resolved local include content is not discarded merely because another unrelated include is unresolved or mismatched.

This is analogous to Source Graph's general rule that a positive local fact can survive incomplete global coverage, while negative/exhaustive claims cannot.

#### Required tests

Build a fixture containing:

```systemverilog
module tb_top;
  `include "connect.svh"
  `include "missing.svh"
endmodule
```

where `connect.svh` contains:

```systemverilog
`ifdef FPGA_BUILD
  fpga_stub u_fpga_stub();
`else
  dut u_dut();
`endif
```

Expected assertions:

- Preprocessor context is incomplete because `missing.svh` is unavailable.
- `u_dut` exists in the component tree.
- `u_fpga_stub` is absent when `FPGA_BUILD` is not defined.
- The hierarchy does not claim the include context is complete.

---

### 4.4 Filter component-tree candidates to actual scanned module/interface definitions

#### Proposed behavior

Keep raw parser candidates in `module_instance_map` for diagnostics, but only create a `component_tree` node when the candidate type is known to be a module or interface defined in the scanned compilation set.

Conceptually:

```python
known_definition_names = all_scanned_module_names | all_scanned_interface_names

for candidate in module_instance_map[parent_module]:
    if candidate["module_name"] not in known_definition_names:
        continue
    add_component_tree_node(candidate)
```

#### Why filtering occurs at tree construction

The raw scanner may still be useful for diagnostics. For example, Source Graph can distinguish an unprojected real-looking child candidate from a simple signal/member suffix when explaining a scope failure.

But the component tree is stronger than raw lexical evidence: it is used as a proof boundary for Source Graph instance projection. Only types that have a real scanned module/interface definition should become addressable hierarchy nodes.

#### Why interfaces must be whitelisted

Legitimate interface instances use the same syntactic form as module instances.

```systemverilog
bus_if bus();
```

Filtering only against module names would create a regression by removing real interface instances. The allowed type set must therefore include both:

```text
module definitions
interface definitions
```

#### Trade-off: opaque/black-box types

A valid elaborated black-box instance with no scanned module/interface source will be omitted from this static component tree.

This is an intentional conservative trade-off:

- The tree must not elevate arbitrary identifier pairs into proven hierarchy.
- Opaque instances can later be represented using authoritative elaborated/NPI evidence if available.
- It is safer for Source Graph to stop at an unproved boundary than to analyze a fictitious scope.

#### Required tests

1. A known real module instance is kept.
2. A known real interface instance is kept.
3. A syntactically instance-shaped unknown type is omitted.
4. UVM-like assignment forms cannot generate tree nodes.
5. A type defined later in compile order is retained, proving the definition set is collected globally before tree construction.

---

### 4.5 Improve bounded-bootstrap diagnostics

#### Problem

Bounded bootstrap previously reduced several materially different preprocessing failures into a single opaque reason:

```text
bootstrap_include_context_unproved
```

#### Proposed behavior

Keep the stable top-level blocker code but add fixed, privacy-safe categories:

```json
{
  "code": "bootstrap_include_context_unproved",
  "stage": "source_closure",
  "preprocessor_issue_categories": [
    "include_path_unresolved"
  ]
}
```

Suitable categories include:

```text
include_path_unresolved
include_expression_unresolved
compile_options_incomplete
include_evidence_mismatch
```

No paths, source fragments, macro values, or protected-IP data should be emitted.

#### `include_evidence_mismatch` treatment

An evidence mismatch is not equivalent to an unresolved include. It can mean the lightweight preprocessor's local reconstruction differs from simulator-recorded include evidence even though the target-side expanded text remains usable.

Treat it as an explicit scoped coverage exclusion rather than an unconditional bootstrap abort. Continue to hard-block situations that make the expanded text untrustworthy, such as unresolved include paths/expressions or exceeded include depth.

---

## 5. Files expected to change

The exact final patch may differ after maintainer review, but the local implementation/validation work involved:

```text
server.py
src/bounded_hierarchy_bootstrap.py
src/tb_hierarchy_builder.py
tests/test_bounded_hierarchy_bootstrap.py
tests/test_diagnostic_snapshot.py
tests/test_server.py
tests/test_tb_hierarchy_builder.py
```

No public MCP tool signature change is required for the correctness portion.

---

## 6. Test plan and observed test outcome

### Focused regression suite

The following focused suite passed after the changes:

```bash
.venv/bin/pytest \
  tests/test_tb_hierarchy_builder.py \
  tests/test_bounded_hierarchy_bootstrap.py \
  tests/test_source_graph_public_routing.py \
  tests/test_source_graph_adapter.py \
  tests/test_server.py \
  tests/test_diagnostic_snapshot.py \
  -q
```

Observed result:

```text
301 passed
```

### What this suite verifies

| Area | Coverage intent |
|---|---|
| Hierarchy builder | Include expansion, conditional branch selection, parameterized instances, generated instances, interface instances, false-positive containment. |
| Bounded bootstrap | Structured preprocessor blockers and scoped evidence handling. |
| Source Graph routing | Public routing contracts and conservative fallback semantics. |
| Source Graph adapter | Hierarchy scope resolution and projection semantics. |
| Server | Session state, prerequisite handling, same-case versus case-change invalidation. |
| Diagnostic snapshot | Cache/provenance visibility under updated invalidation behavior. |

### Full-suite note

The original correctness slice had environment-sensitive broad-suite gaps, but
the completed recovery/reuse program has since been validated by the full local
suite: **1,842 passed, 38 skipped**. The skipped tests retain their explicit
optional native-tool/fixture prerequisites. Scoped Ruff checks and
`git diff --check` also pass.

---

## 7. Production validation results (anonymized)

### 7.1 Hierarchy result before and after

| Metric | Before | After include recovery | After false-node filter |
|---|---:|---:|---:|
| Discovered module definitions | 7 | 802 | 525 |
| Component-tree instances | 10 | 8,753 | 3,396 |
| Tree depth | 4 | 15 | 15 |
| Top-level children | 2 | 41 | 26 |
| Real DUT chain present | No | Yes | Yes |
| Obvious UVM/header pseudo-nodes | No (because real include was lost) | Yes | No |

The intermediate increase in apparent nodes is expected: it proves include-expanded content is now being observed. The final filtered state retains the deep real hierarchy while removing untrusted lexical false positives.

### 7.2 Source Graph hierarchy resolution

After the fix, Source Graph resolved the deep target hierarchy completely:

```text
hierarchy_resolution.status: resolved
resolved_endpoint_count: 1
truncated_endpoint_count: 0
max_matched_instance_count: 8
ancestor_count: 8
max_remaining_path_segment_count: 1
```

This is the central correctness success criterion. The former scope blocker was eliminated without weakening the requirement that every projected instance segment be proved.

### 7.3 Positive load facts

The Source Graph load query reached the real leaf module and returned two positive RHS-consumption facts for the anonymized target signal:

```text
target_status_pos   - source line L1
target_status_dly   - source line L2
```

The specific local file and line data have been intentionally omitted from this document. Maintainers can reproduce with their own authorized local input set.

The query semantics were:

```text
positive_fact_confidence: conditional
target_bit_coverage: complete
global_coverage_status: inconclusive
exhaustive_search: false
negative_claim_allowed: false
```

Correct reviewer interpretation:

> The listed load facts are positively proved for the target bits.

Incorrect interpretation:

> The listed load facts are an exhaustive enumeration of all loads.

### 7.4 Driver query reached a new performance boundary

The driver query no longer failed at hierarchy scope. It reached Source Graph worker preparation and then exceeded the configured worker deadline:

```text
prepare_status: timed_out
blocker.code: worker_timeout
blocker.stage: worker_process
build_wall_ms: approximately 120 seconds
```

Static fallback then could not resolve the deeply dotted/aggregate target and returned unsupported. This must **not** be read as “no driver exists.” It means the target was correctly scoped but Source Graph did not finish building the required artifact in time.

---

## 8. Historical Source Graph driver-timeout baseline

> This section preserves the baseline that motivated the performance work. The
> current implementation no longer has this cold-build shape on the reported
> workload; see Section 14 for the measured resolution.

### 8.1 What was observed

For the same deep target scope:

- A Source Graph **load** preparation completed near the timeout boundary.
- A Source Graph **driver** preparation timed out shortly after reaching the default limit.
- Both preparations required large memory footprints (roughly multiple GiB RSS at peak in the observed environment).
- The compile manifest was incomplete, so normal artifact reuse was deliberately disabled.

Representative receipt semantics:

```text
manifest.complete: false
artifact_reuse: bypass_incomplete
cache_lookup_reason: identity_not_reusable
```

This is correctness-preserving: an incomplete manifest must not be treated as a generally reusable, long-lived content-addressed artifact.

### 8.2 Why duplicate builds are likely expensive

A driver query and a load query can have nearly identical compile context and hierarchy projection but still trigger separate Source Graph builds when artifact reuse is bypassed. On a large design, each build can approach or exceed the worker deadline.

The issue is not that incomplete artifacts should become globally cacheable. The issue is that the system needs a safe way to avoid duplicate **in-flight** work for the same currently requested artifact inputs.

---

## 9. Historical follow-up performance proposal

At proposal time, the hierarchy fixes needed to land independently from the
following scheduling, caching, and timeout work. Those follow-ups were later
implemented as separate benchmark-backed commits. The subsections remain here
as the design rationale; their current status is summarized in Section 14.

### 9.1 Priority 1 — in-flight coalescing for exact same-request builds

#### Goal

Allow concurrent or overlapping requests with the exact same preparation identity to share one worker build even when the resulting artifact is not eligible for general persistent reuse.

#### Non-goal

Do **not** make incomplete manifests globally reusable or persist them as trusted cache entries.

#### Proposed model

1. Compute an ephemeral flight identity from the exact inputs available to the request: ordered inputs/options/tops, hierarchy snapshot, projection scope, frontend version, and relevant preparation parameters.
2. Register a process-local in-flight future/promise under that identity.
3. A second identical requester joins the existing flight rather than launching a second worker.
4. On success, hand the result to current waiters. A short, bounded session-local retention may be considered, but it must not be represented as cross-request persistent cache reuse.
5. On worker failure, timeout, or total cancellation, remove the flight deterministically.

#### Cancellation requirements

- Cancelling one waiter must not kill the shared worker while another waiter still depends on it.
- If all waiters cancel, terminate the worker using existing cancellation semantics.
- No code path may leak worker processes or leave stale flight entries after timeout/error.

#### Required tests

- Two identical overlapping requests launch one worker.
- Both waiters receive the same successful result.
- One waiter cancellation leaves the other alive.
- All waiter cancellation stops the worker.
- A failed/timeout worker clears the flight and allows a later retry.
- No persistent cache hit is reported for a non-reusable/incomplete artifact.

### 9.2 Priority 2 — bounded configurable worker timeout

A build that often completes slightly below a fixed deadline but occasionally exceeds it should be configurable in controlled deployments.

#### Proposal

- Add an environment-controlled Source Graph worker timeout with strict type/range validation.
- Keep the current default unchanged initially.
- Add an explicit numeric receipt field recording the effective timeout.
- Maintain a finite upper bound; do not introduce unbounded worker execution.

#### Benchmark requirement

For a representative large design, measure at least:

```text
prepare wall time
worker CPU time
peak RSS
success/timeout rate
cancellation latency
queue/admission wait
```

Compare default, moderate extension, and any proposed new default. The cost of longer resource occupancy must be reported alongside the improved completion rate.

### 9.3 Priority 3 — prove a smaller source closure before frontend launch

The current Source Graph preparation can project nearly all compile inputs even for a narrow instance chain. There may be a performance opportunity to create a smaller, proof-backed closure.

Potential directions:

1. Use the hierarchy scan to identify module definition files along the proved ancestor chain.
2. Add exact required package/import/include closure.
3. Preserve simulator option and macro context exactly.
4. Build a bounded dependency closure only when it can be proven complete for the requested chain.
5. Fall back to the existing full input projection whenever closure proof is insufficient.

#### Critical guardrail

Do not substitute a guessed filename/module-name subset for the ordered compilation context. Any optimization must preserve Source Graph's current conservative semantics and coverage receipts.

### 9.4 Priority 4 — parser hardening beyond tree filtering

The definition whitelist safely prevents false candidates from becoming hierarchy nodes, but raw token extraction can still contain noisy candidates.

Possible future improvements:

- Retain assignment and other syntactically relevant operators in the tokenizer.
- Track function/task/class/property/sequence/covergroup regions inside modules and exclude those lexical spans from structural instance extraction.
- Use a parser/frontend-backed structural scan where available.

This should be treated as a separate compatibility-sensitive parser change with a larger SystemVerilog corpus.

---

## 10. Security, privacy, and semantic guardrails

This proposal intentionally preserves TraceWeave's conservative behavior:

1. **No unproved hierarchy invention** — the Source Graph boundary is still a proved component tree.
2. **No exhaustive claim from incomplete coverage** — positive facts may be returned with explicit incomplete coverage; negative claims remain blocked.
3. **No protected source retention** — hierarchy scan output remains metadata-only; source text is not retained in the handle store.
4. **No sensitive diagnostics in new receipts** — preprocessor categories are fixed labels only, with no paths/content/macro values.
5. **No persistent trust upgrade for incomplete artifacts** — future coalescing must remain in-flight/session-local and must not become a persistent cache correctness shortcut.
6. **Preserve cancellation semantics** — any worker/cache optimization must retain the existing request cancellation behavior.

---

## 11. Reviewer checklist

### Correctness

- [x] Same-case discovery preserves a valid hierarchy.
- [x] Real case identity changes still clear hierarchy and handles.
- [x] Supplementary log identity participates in hierarchy lookup.
- [x] A real included active-branch DUT instance is recovered despite an unrelated include problem.
- [x] Inactive conditional branch instances remain excluded.
- [x] Unknown type candidates do not become component-tree nodes.
- [x] Legitimate interface instances remain represented.
- [x] A later-defined valid module type is not filtered accidentally.

### Source Graph semantics

- [x] Deep target hierarchy resolves as `resolved`, not `truncated`.
- [x] Positive load facts retain incomplete-coverage semantics.
- [x] No negative/exhaustive claim is enabled by these changes.
- [x] Source Graph fallback remains honest on timeout.

### Operational quality

- [x] Handle-store lifetime remains process/session bounded.
- [x] New invalidation behavior does not leak cross-case hierarchy state.
- [x] No sensitive data is newly exposed.
- [x] Focused tests cover every changed behavioral boundary.

---

## 12. Historical suggested commit decomposition

To make review and rollback easy, split the correctness patch into logical commits:

1. `Preserve hierarchy across same-case path discovery`
2. `Resolve supplementary-log hierarchy handles`
3. `Expose bounded bootstrap include blocker categories`
4. `Recover instances from incomplete include expansion`
5. `Filter component-tree nodes to known module/interface types`
6. `Add hierarchy recovery and parser false-positive regressions`

The Source Graph timeout/reuse improvements were subsequently kept in separate
benchmarked commits.

---

## 13. Disposition of the original recommendation

The implementation followed the proposed sequence: land the
hierarchy/session/parser correctness boundary first, then baseline the cold
driver path, add exact in-flight coalescing, retain a bounded timeout, implement
proof-backed source closure, and measure wall time, memory, cancellation, and
semantic equivalence before expanding reuse. Section 14 records the resulting
architecture and evidence. The sequencing and guardrails remain useful review
history, but the listed performance steps are no longer open tasks.

---

## 14. Current implementation status (2026-08-23)

This proposal is now **implemented for both the original hierarchy-recovery
failure and its actionable performance follow-up**. Persistent semantic-session
reuse is also implemented, but deliberately remains an opt-in policy: the
remaining gate is broader operational evidence, not an unresolved correctness
or cold-build defect.

### 14.1 Hierarchy recovery and proof boundaries are complete

The current implementation:

- preserves completed hierarchy state across same-case discovery while a real
  case/snapshot change still invalidates handles and dependent state;
- resolves supplementary-log hierarchy identity for split compile/elaboration
  flows;
- recovers positive instance evidence from successfully expanded active include
  branches without upgrading an incomplete preprocessing context to complete;
- admits only known module/interface definitions into the compatibility tree;
- annotates hierarchy candidates and materialized edges with provenance and
  fixed gap codes, and rejects unproved generate/array/bind/ambiguous-definition
  edges instead of flattening lexical guesses;
- routes hierarchy lookup through a bounded provider contract. The lexical
  provider remains the dependency-free default, while existing compact semantic
  IR and exact target-prefix NPI evidence can supply authoritative bindings
  without a full-design topology walk.

The lightweight preprocessor remains a conservative lexical recovery layer; it
has not been promoted into a replacement SystemVerilog compiler. Slang or NPI
continues to own elaborated semantics, and incomplete coverage still forbids
exhaustive negative claims.

### 14.2 The reported cold-build timeout is resolved

The production adapter now constructs a proof-backed hierarchy dependency
closure before launching the frontend. On the representative large-SoC target
that originally failed:

| Metric | Historical full replay | Current bounded closure |
|---|---:|---:|
| Ordered frontend inputs | 784 | 126 |
| Cold worker build | Did not finish within 180 s | 4,176.488 ms |
| Parent IR load/index | Unavailable | 271.863 ms |
| Second exact prepare | Unavailable | 0.860 ms |
| Worker peak RSS | No completed sample | 513,280 KiB |
| Compact IR | Unavailable | 2,251,984 bytes |

The measured cold-build improvement is at least 43.1x relative to the former
180-second timeout lower bound. Driver and load both return their proved
positive facts from the same artifact; compile-projection exclusions remain
visible, so this result does not claim full-design connectivity completeness.

Content identity still includes the complete ordered manifest/options/tops and
compile/hierarchy snapshots. A changed or incomplete captured snapshot cannot
pair stale hierarchy with new source. The first Source Graph request reuses
digests captured during hierarchy/source-index reads and hashes only unseen
support inputs.

### 14.3 Reuse is bounded and provenance-preserving

The lifecycle now has several distinct, explicit reuse mechanisms:

1. Exact concurrent cold requests share one live worker, including incomplete
   identities. One waiter may cancel without killing work needed by another;
   final-waiter cancellation stops and clears the flight.
2. A successful content-anchored incomplete build may publish one consumed-once
   60-second/512-MiB handoff for the next exact request. It remains
   `bypass_incomplete_key`, never enters the memory/disk cache, and cannot serve
   dominating-scope lookup.
3. Reusable compact IR enters a bounded process-memory cache. A cached artifact
   may serve a smaller compile projection or hierarchy scope only after exact
   design/snapshot/version/exclusion checks prove that the single cached
   artifact dominates the request.
4. The initial deep driver/load plan may include a strictly bounded proved
   adjacent scope, replacing a reactive two-build sequence with one build. On
   the representative public driver-then-load sequence, total wall time fell
   from 10,462.869 ms to 6,046.970 ms while preserving the result fingerprint
   and one-artifact provenance.
5. The optional exact disk tier remains default-off, validates current content
   before lookup, performs no startup scan, and treats corrupt entries as safe
   misses.

No result merges facts from multiple artifacts or backends. Timeout,
cancellation, corruption, unsafe scope, and incomplete negative coverage still
restart or fall back according to the existing conservative route.

### 14.4 Large-project hierarchy and query work are bounded

The recovery work was extended into the large-project path that feeds Source
Graph:

- compilation-context, interface-reference, include-mask, and literal-admission
  indexes remove known quadratic/redundant lexical scans while preserving
  compilation-unit macro state;
- an exact-identity transient `CompileSourceIndex` lets concurrent hierarchy and
  structural scans share source reads, then releases source text when the last
  consumer exits;
- the compatibility hierarchy uses a template DAG with copy-on-write NPI
  annotation. A 1,001,000-logical-instance stress case reduced tree
  materialization from 1,793.482 ms to 3.488 ms and retained RSS delta from
  293,628 KiB to 528 KiB with identical semantics;
- the representative hierarchy scan fell from a 16,541.634-ms baseline to
  4,785.746 ms while preserving its structural oracle; the remaining work is
  linear compilation-unit macro replay rather than a known quadratic hotspot;
- structural-risk scanning removed its repeated brace/source-prefix scans,
  reducing the representative workload from 58,988.707 ms to a 3,441.750-ms
  median with an identical finding digest;
- Source Graph driver/load queries enforce state, edge, match, and frontier
  budgets with cancellation checkpoints and explicit partial/truncation
  receipts. A 50,000-load stress case reduced query/serialization medians from
  1,324.266/5,575.989 ms to 3.696/16.311 ms by returning a stable bounded prefix,
  not by pretending to enumerate all loads;
- hierarchy prefix lookup, wide-bus membership, and predecessor-based path
  reconstruction remove measured warm-query hotspots. Public schemas and
  coverage semantics remain compatible.

NPI remains the preferred backend when trustworthy KDB evidence is available.
Its direct loads avoid unnecessary whole-cone work, and recursive driver
traversal is capped inside the native callback: the measured long-tail query
fell from roughly 59.4 seconds to roughly 2.24 seconds, with work/output
truncation explicitly reported. An offline NPI/Source Graph differential
harness classifies expected coverage/provenance differences without treating
NPI as an infallible oracle.

### 14.5 Persistent semantic session is implemented but stays opt-in

The selected design is a hybrid: one isolated, short-lived, RSS/TTL-bounded
Slang semantic context may be retained, while only scoped compact IR is
cacheable. It does not create an eager full-design IR or keep AST state in the
MCP process. Default guards are one active design, 60-second idle TTL, 768-MiB
RSS limit, 64 proved instances, and 256 ordered inputs. Context change, crash,
protocol error, timeout, cancellation, or RSS excess terminates the child and
cannot publish a partial artifact.

In a 20-query eligible large-SoC sequence, one-shot and persistent totals were
81,224.832 ms and 43,562.067 ms respectively (46.369% lower). Twenty frontend
launches became one miss plus 19 hits; fact/status/coverage hashes were
identical. The trade-off was a retained child near 553 MiB. In an independent
smaller-SoC sequence, the first bounded compact artifact already covered the
remaining 19 queries, so both modes used ordinary memory hits and the semantic
session added no material benefit.

Local-only operational telemetry now reports metric-bearing Source Graph calls
per case, repeated-case frequency, timestamp/pair coverage, and adjacent calls
inside the default 60-second window as an **upper bound** on reuse opportunity.
The current sample has 2,394 total records across 167 cases but only three
attributable Source Graph calls, each in a different case. That sample cannot
justify retaining roughly 553 MiB after a single fallback query. Therefore
`TRACEWEAVE_SOURCE_GRAPH_SEMANTIC_SESSION` remains default-off; this is a product
policy decision, not an implementation failure.

### 14.6 Final validation and remaining evidence gate

The current full regression result is **1,842 passed, 38 skipped**. Focused
benchmarks use fresh processes, stable result fingerprints, explicit workload
conditions, wall/RSS measurements, and compatibility/coverage assertions.
Public MCP inputs are unchanged; receipts gained additive metrics, provenance,
work-budget, and reuse fields. FSDB locking and observable cancellation/timeout
behavior are unchanged.

The original proposal may now be closed. Reconsidering semantic-session
default-on requires both:

1. more independent designs that actually qualify for semantic-session reuse,
   rather than designs already covered by bounded compact IR; and
2. a larger operational sample showing repeated eligible Source Graph queries
   inside the reuse window often enough to justify the retained memory.

Until those data exist, the safe product route remains trusted NPI, then bounded
on-demand Source Graph, then honest Legacy Static fallback. No synthetic query
sequence should be presented as real operational frequency evidence.
