# Design: Mixed Log Detection (compile + sim in one file)

## Problem

`vcs -R` and `xrun` one-step flows produce a single log file containing compile, elaborate, and simulate output. Current behavior:

- `vcs.log` matches `SIM_LOG_PATTERNS` (`vcs.log`), goes into `sim_logs`
- `xrun.log` matches `SIM_LOG_PATTERNS` (`*run*.log`), goes into `sim_logs`
- Neither matches `COMPILE_LOG_PATTERNS` (`*comp*.log`, `*elab*.log`)
- `compile_logs` is empty → `build_tb_hierarchy` cannot be called
- Agent loses the hierarchy building step, skipping the recommended workflow step 2

Both `parse_vcs_compile_log` and `parse_xcelium_compile_log` already handle mixed logs correctly — unrecognized simulation output lines are naturally skipped by the parsers.

## Design

### Core idea

When `compile_logs` is empty after normal discovery, inspect the already-selected `sim_logs` for explicit compile/elaborate markers. If a sim log clearly contains compile or elaborate content, reuse that same file as a compile log entry.

This is intentionally conservative:

- Mixed-log detection should only fire when the file contains direct compile/elab evidence
- Simulator banner detection alone is **not** enough
- The fallback should only run in branches that already resolved a specific case and therefore already have case-local `sim_logs`

### Detection logic

For each selected sim log entry:

1. Run the existing `_detect_log_phase()` fast scan over the first 50 lines.
2. If no compile/elab marker is found, run a second content scan over a larger window such as the first 200-500 lines.
3. Return a mixed compile-log entry only when compile/elab keywords are found in one of those scans.

Accepted evidence is still based on the existing keyword sets:

- `_ELABORATE_KEYWORDS`
- `_COMPILE_KEYWORDS`

Rejected evidence:

- `Chronologic VCS`
- `xrun(64)`
- any other simulator signature from `detect_simulator()`

Reason: `detect_simulator()` answers “which simulator produced this file”, not “does this file contain enough compile/elab content to drive `build_tb_hierarchy`”.

### Result shape

Do not set `phase` to `"mixed"` as the primary phase.

Instead:

- preserve the detected phase as `phase: "elaborate"` or `phase: "compile"`
- add an extra marker such as `is_mixed: true` or `source: "mixed_sim_log"`

This keeps the result compatible with the current server workflow, which prefers compile logs whose `phase == "elaborate"`.

### What changes

Only `src/path_discovery.py` needs functional changes.

No parser changes are required in:

- `src/compile_log_parser.py`
- `src/tb_hierarchy_builder.py`

No behavior change is required in:

- `server.py`
- `config.py`

### Implementation steps

#### Step 1: Add helper function for conservative mixed detection

Location: `src/path_discovery.py`, near `_detect_log_phase`.

Suggested split:

```python
def _scan_log_phase(log_path: Path, max_lines: int) -> str:
    ...


def _detect_mixed_compile_log_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Reuse a sim log as a compile log only when explicit compile/elab
    markers are present in the file content."""
    path = Path(entry["path"])

    phase = _scan_log_phase(path, max_lines=50)
    if phase == "unknown":
        phase = _scan_log_phase(path, max_lines=300)
    if phase not in {"compile", "elaborate"}:
        return None

    mixed_entry = dict(entry)
    mixed_entry["phase"] = phase
    mixed_entry["is_mixed"] = True
    return mixed_entry
```

Notes:

- `_detect_log_phase()` can be refactored to call `_scan_log_phase(log_path, max_lines=50)`
- do not use `detect_simulator()` as a fallback signal here

#### Step 2: Apply fallback only in case-resolved auto-discovery branches

In `_discover_auto`, run the fallback only when `sim_logs` already belong to a concrete case:

- `case_dir` branch
- `root_dir` with exactly one matched `case_name`

Pattern:

```python
if not compile_logs and sim_logs:
    mixed_entry = next(
        (item for item in (_detect_mixed_compile_log_entry(log) for log in sim_logs) if item),
        None,
    )
    if mixed_entry is not None:
        compile_logs = [mixed_entry]
```

Do not apply this fallback in:

- `root_dir` with no `case_name`
- `unknown`

Those branches intentionally do not expose `sim_logs`, preserving current case-isolation semantics.

#### Step 3: Apply the same fallback in config-driven case resolution

In `_discover_from_config`, use the same fallback only when config resolution already produced case-local `sim_logs`.

This includes:

- `discovery_mode == "case_dir"`
- `case_name` resolved through configured `case_dir`

Do not run the fallback in branches that are only listing available cases.

#### Step 4: Update hints without weakening existing warnings

Keep the existing missing-compile hint when no compile log was found at all.

If a mixed sim log is reused, add an additional hint such as:

```python
mixed_logs = [log for log in compile_logs if log.get("is_mixed")]
if mixed_logs:
    names = ", ".join(Path(log["path"]).name for log in mixed_logs)
    hints.append(
        f"{names} reused from sim_logs because it contains compile/elaborate markers"
    )
```

Preserve the existing hint:

- if no `compile_logs` exist, still report `No compile/elab log found ...`
- if compile logs exist but none has `phase == "elaborate"`, still report that `build_tb_hierarchy` may be partial

### Expected result after change

Given a `vcs -R` project directory containing only `vcs.log` and `test.fsdb`:

```json
{
  "discovery_mode": "case_dir",
  "compile_logs": [
    {
      "path": "/path/to/vcs.log",
      "phase": "elaborate",
      "is_mixed": true,
      "size": 123456,
      "mtime": "2026-03-15 10:00:00",
      "age_hours": 1.0
    }
  ],
  "sim_logs": [
    {
      "path": "/path/to/vcs.log",
      "size": 123456,
      "mtime": "2026-03-15 10:00:00",
      "age_hours": 1.0
    }
  ],
  "hints": [
    "vcs.log reused from sim_logs because it contains compile/elaborate markers"
  ]
}
```

Agent sees `compile_logs` is non-empty and still has a valid `phase` field, so the existing workflow can proceed to `build_tb_hierarchy` without special-case handling.

### Tests

Add tests in `tests/test_path_discovery.py`:

1. **VCS mixed log**: create a temp dir with a `vcs.log` containing `Chronologic VCS ... Parsing design file '...'` and a dummy `.fsdb`. Verify `compile_logs` contains the same path, `is_mixed == true`, and `phase` is `elaborate`.
2. **Xrun mixed log**: create a temp dir with an `xrun.log` containing `xrun(64) ... xmvlog ...`. Verify the same reuse behavior and `phase` is `compile` or `elaborate` according to content.
3. **Long filelist but explicit compile marker after line 50**: create a log where compile/elab evidence appears after the first 50 lines but within the extended scan window. Verify mixed detection still works.
4. **Simulator banner only (no false positive)**: create a `sim.log` containing only `Chronologic VCS` or `xrun(64)` plus runtime text, but no compile/elab markers. Verify it is **not** added to `compile_logs`.
5. **Separate logs**: create a dir with `comp.log` + `sim.log`. Verify `compile_logs` uses `comp.log`, not the sim log, and no mixed marker is added.
6. **Config-driven case path**: under `.mcp.yaml`, omit a dedicated compile log but provide a mixed `sim_log`. Verify `_discover_from_config()` reuses it correctly.
7. **Empty sim_logs**: verify no crash when `sim_logs` is empty.
