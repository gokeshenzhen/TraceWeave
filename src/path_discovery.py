"""
path_discovery.py
Auto-discover compile logs, simulation logs, and waveform files under verif/.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
import fnmatch
import os

import yaml

from config import (
    CASE_DIR_MAX_DEPTH,
    COMPILE_LOG_PATTERNS,
    DISCOVER_MAX_DEPTH_CASE,
    get_fsdb_runtime_info,
    MCP_CONFIG_FILE,
    SIM_LOG_PATTERNS,
    WAVE_PATTERNS,
    WORK_CONTAINER_NAMES,
)
from src.compile_log_parser import detect_simulator


# Depth bound for basename recovery of a mis-specified explicit path: artifacts
# live at work/<case>/<file> (depth 2 from verif_root, or 3 via a container).
_BASENAME_RECOVERY_DEPTH = 3
_ELABORATE_KEYWORDS = (
    "parsing design file",
    "top level modules",
    "xmelab",
    "-elaborate",
)
_COMPILE_KEYWORDS = (
    "xmvlog",
    "xmvhdl",
    "vlogan",
    "vhdlan",
)
_CASE_PREFIXES = ("work_", "sim_", "case_")
_DEFAULT_LOG_PHASE_SCAN_LINES = 50
_EXTENDED_LOG_PHASE_SCAN_LINES = 300


def discover_sim_paths(
    verif_root: str,
    case_name: str | None = None,
    *,
    sim_log: str | None = None,
    wave_file: str | None = None,
    compile_log: str | None = None,
) -> dict[str, Any]:
    root = Path(verif_root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"verif_root is not a directory: {verif_root}")

    # Explicit user-supplied paths take precedence over both .mcp.yaml and
    # auto-discovery: when the caller already knows a path, use it verbatim and
    # only auto-discover the fields they left out.
    if sim_log or wave_file or compile_log:
        return _discover_from_explicit(root, case_name, sim_log, wave_file, compile_log)

    config_match = _load_mcp_config(root)
    if config_match is not None:
        return _discover_from_config(root, case_name, *config_match)

    return _discover_auto(root, case_name)


def _discover_from_explicit(
    root: Path,
    case_name: str | None,
    sim_log: str | None,
    wave_file: str | None,
    compile_log: str | None,
) -> dict[str, Any]:
    """Discover paths from user-supplied explicit overrides.

    Any field the caller provides is used as-is; the fields they omit fall back
    to auto-discovery anchored at the directory of the provided sim log (or, if
    no sim log, the provided waveform). So a caller who passes only a sim-log
    path still gets the waveform and compile/elab logs discovered for them, and
    a caller who passes a sim log + waveform but no compile/elab log still gets
    the compile/elab log found in the case dir or a sibling build dir.
    """
    hints: list[str] = []
    sim_path = _resolve_explicit_path(root, sim_log, "sim_log", hints)
    wave_path = _resolve_explicit_path(root, wave_file, "wave_file", hints)
    compile_path = _resolve_explicit_path(root, compile_log, "compile_log", hints)

    anchor: Path | None = None
    if sim_path is not None:
        anchor = sim_path.parent
    elif wave_path is not None:
        anchor = wave_path.parent

    if anchor is not None:
        sim_logs = (
            [_explicit_entry(sim_path, "sim")]
            if sim_path is not None
            else _sim_logs_in_dir(anchor)
        )
        wave_files = (
            [_explicit_entry(wave_path, "wave")]
            if wave_path is not None
            else _search_files([anchor], WAVE_PATTERNS, 0)
        )
        if compile_path is not None:
            compile_logs = [_explicit_entry(compile_path, "compile")]
        else:
            compile_logs = _discover_case_compile_logs(anchor, sim_logs)
        discovery_mode = "case_dir"
        target_case_dir = anchor
        if case_name and not _case_name_matches_dir(anchor, case_name):
            hints.append(
                f"Requested case_name '{case_name}' does not match the anchored "
                f"case directory '{anchor.name}'"
            )
    else:
        # No sim/wave path resolved to an existing file, so there is no case dir
        # to anchor sim/waveform discovery to.
        compile_logs = (
            [_explicit_entry(compile_path, "compile")] if compile_path is not None else []
        )
        sim_logs = []
        wave_files = []
        discovery_mode = "unknown"
        target_case_dir = None
        if (sim_log or wave_file) and sim_path is None and wave_path is None:
            hints.append(
                "Could not anchor discovery: the provided sim_log/wave_file did not "
                "resolve to an existing file (see the path hint above)"
            )
        elif compile_path is not None:
            hints.append(
                "Only compile_log was provided; also pass sim_log (or wave_file) to "
                "anchor simulation log and waveform discovery"
            )

    return _build_discovery_result(
        request_root=root,
        case_name=case_name,
        config_source="explicit",
        config_root=None,
        discovery_mode=discovery_mode,
        target_case_dir=target_case_dir,
        compile_logs=compile_logs,
        sim_logs=sim_logs,
        wave_files=wave_files,
        available_cases=[],
        hints=hints,
    )


def _resolve_explicit_path(
    root: Path, value: str | None, label: str, hints: list[str]
) -> Path | None:
    """Resolve a user-supplied path robustly.

    An absolute path is used as-is. A relative path is tried against ``root`` and
    each of its ancestors (nearest first) — this resolves a path given relative
    to the repo root (an ancestor of verif_root) and transparently collapses a
    doubled verif_root tail like ``top/verification/top/verification/…``. If no
    candidate exists, fall back to basename recovery: a unique file with the same
    name found under verif_root is used (with a hint), ambiguous matches surface
    a did-you-mean list. A bad path is dropped with a hint rather than raising,
    so one mistyped override does not sink the whole discovery.
    """
    if not value:
        return None
    raw = Path(str(value)).expanduser()
    candidates = _explicit_path_candidates(root, raw)
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved

    recovered = _recover_by_basename(root, raw.name)
    if len(recovered) == 1:
        hints.append(
            f"Provided {label} '{value}' was not found as given; resolved by "
            f"basename to {recovered[0]}"
        )
        return recovered[0]
    if len(recovered) > 1:
        listed = ", ".join(str(path) for path in recovered[:5])
        hints.append(
            f"Provided {label} '{value}' was not found; multiple files named "
            f"'{raw.name}' exist: {listed}"
        )
        return None
    hints.append(
        f"Provided {label} path does not exist (tried {len(candidates)} location"
        f"{'s' if len(candidates) != 1 else ''}): {value}"
    )
    return None


def _explicit_path_candidates(root: Path, raw: Path) -> list[Path]:
    """Base anchors to try for an explicit path. Absolute → itself; relative →
    verif_root then each ancestor up to the filesystem root, nearest first."""
    if raw.is_absolute():
        return [raw]
    candidates: list[Path] = []
    base = root
    while True:
        candidates.append(base / raw)
        if base.parent == base:
            break
        base = base.parent
    return candidates


def _recover_by_basename(root: Path, name: str) -> list[Path]:
    """Bounded, symlink-following search under ``root`` for files named ``name``.
    followlinks=True because the artifact dir (work/) is commonly a symlink; the
    depth bound keeps a symlink cycle from running away."""
    if not name:
        return []
    matches: list[Path] = []
    seen: set[str] = set()
    for path in _iter_files(root, _BASENAME_RECOVERY_DEPTH, followlinks=True):
        if path.name != name:
            continue
        resolved = path.resolve()
        key = str(resolved)
        if key not in seen and resolved.is_file():
            seen.add(key)
            matches.append(resolved)
    return matches


def _explicit_entry(path: Path, kind: str) -> dict[str, Any]:
    info = _collect_file_info(path)
    if kind == "compile":
        info["phase"] = _detect_log_phase(path)
    elif kind == "wave":
        info["format"] = path.suffix.lstrip(".").lower()
    return info


def _discover_auto(root: Path, case_name: str | None) -> dict[str, Any]:
    classification = _classify_directory(root)
    hints: list[str] = []

    # When verif_root holds no artifacts directly and its immediate children are
    # source/script dirs, the artifacts often live one level down under a work
    # container (work/ etc.). Descend into the first such child that reclassifies
    # to a real case/shared root; all subsequent file ops use that search_root,
    # while the reported verif_root stays the original request.
    descended_into: Path | None = None
    if classification[0] == "unknown":
        for container in _work_container_children(root):
            reclassified = _classify_directory(container)
            if reclassified[0] != "unknown":
                descended_into = container
                classification = reclassified
                break

    discovery_mode, local_sim_logs, local_wave_files, child_case_dirs = classification
    search_root = descended_into or root
    if descended_into is not None:
        hints.append(
            f"verif_root holds no artifacts directly; descended into work "
            f"container '{descended_into.name}' to locate them"
        )

    target_case_dir: Path | None = None

    if discovery_mode == "case_dir":
        target_case_dir = search_root
        sim_logs = local_sim_logs
        wave_files = local_wave_files
        compile_logs = _discover_case_compile_logs(search_root, sim_logs)
        available_cases = []
        if case_name and not _case_name_matches_dir(search_root, case_name):
            hints.append(
                f"Requested case_name '{case_name}' does not match current case directory '{search_root.name}'"
            )
    elif discovery_mode == "root_dir":
        root_compile_logs = _search_files([search_root], COMPILE_LOG_PATTERNS, 0)
        if case_name:
            matched_case_dirs = _match_case_dirs(child_case_dirs, case_name)
            if len(matched_case_dirs) > 1:
                sim_logs = []
                wave_files = []
                compile_logs = root_compile_logs
                available_cases = []
                hints.append(
                    "Ambiguous case_name "
                    f"'{case_name}': matched {', '.join(path.name for path in matched_case_dirs)}"
                )
            elif len(matched_case_dirs) == 1:
                target_case_dir = matched_case_dirs[0]
                sim_logs = _dedupe_sorted(
                    _search_files([target_case_dir], SIM_LOG_PATTERNS, DISCOVER_MAX_DEPTH_CASE)
                    + _stem_named_sim_logs(target_case_dir)
                )
                wave_files = _search_files([target_case_dir], WAVE_PATTERNS, DISCOVER_MAX_DEPTH_CASE)
                compile_logs = root_compile_logs or _discover_case_compile_logs(target_case_dir, sim_logs)
                available_cases = []
            else:
                sim_logs = []
                wave_files = []
                compile_logs = root_compile_logs
                available_cases = []
                hints.append(
                    f"No case directory matched case_name '{case_name}' under {search_root}"
                )
        else:
            compile_logs = root_compile_logs
            sim_logs = []
            wave_files = []
            available_cases = _describe_case_dirs(child_case_dirs)
    else:
        compile_logs = _search_files([search_root], COMPILE_LOG_PATTERNS, 0)
        sim_logs = []
        wave_files = []
        available_cases = []
        hints.append(
            "verif_root does not look like a case directory or a shared simulation root"
        )
        hints.append(
            "Point get_sim_paths to either a case directory containing sim logs/waves or a root directory whose immediate subdirectories are case directories"
        )

    return _build_discovery_result(
        request_root=root,
        case_name=case_name,
        config_source="auto",
        config_root=None,
        discovery_mode=discovery_mode,
        target_case_dir=target_case_dir,
        compile_logs=compile_logs,
        sim_logs=sim_logs,
        wave_files=wave_files,
        available_cases=available_cases,
        hints=hints,
    )


def _work_container_children(root: Path) -> list[Path]:
    """Immediate children of ``root`` whose name marks a conventional work/build
    container (see WORK_CONTAINER_NAMES). Whether to actually descend is decided
    by the caller via reclassification, so this only narrows the candidates."""
    return [
        child
        for child in _list_child_dirs(root)
        if child.name.lower() in WORK_CONTAINER_NAMES
    ]


def _load_mcp_config(start_dir: Path) -> tuple[Path, dict[str, Any]] | None:
    current = start_dir
    while True:
        config_path = current / MCP_CONFIG_FILE
        if config_path.exists():
            with config_path.open("r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"{config_path} must contain a YAML object")
            return current, loaded
        if current.parent == current:
            return None
        current = current.parent


def _discover_from_config(
    request_root: Path, case_name: str | None, config_root: Path, config: dict[str, Any]
) -> dict[str, Any]:
    discovery_mode, local_sim_logs, local_wave_files, child_case_dirs = _classify_directory(request_root)
    compile_logs = _resolve_config_entries(config_root, [config.get("compile_log")], "compile")
    case_dir = None
    hints: list[str] = []
    if "case_dir" in config:
        if discovery_mode == "case_dir":
            case_dir = request_root
            sim_logs = _resolve_config_entries(case_dir, [config.get("sim_log")], "sim")
            wave_files = _resolve_config_entries(case_dir, [config.get("wave_file")], "wave")
            if not sim_logs:
                sim_logs = local_sim_logs
            if not wave_files:
                wave_files = local_wave_files
            if not compile_logs:
                compile_logs = _reuse_mixed_sim_logs(sim_logs)
            available_cases = []
            if case_name and not _case_name_matches_dir(case_dir, case_name):
                hints.append(
                    f"Requested case_name '{case_name}' does not match current case directory '{case_dir.name}'"
                )
        elif case_name is None:
            if discovery_mode == "root_dir":
                available_cases = _describe_case_dirs(child_case_dirs)
            else:
                available_cases = _discover_cases(config_root)
            sim_logs: list[dict[str, Any]] = []
            wave_files: list[dict[str, Any]] = []
        else:
            case_dir_rel = str(config["case_dir"]).format(case=case_name)
            case_dir = (config_root / case_dir_rel).resolve()
            sim_logs = _resolve_config_entries(case_dir, [config.get("sim_log")], "sim")
            wave_files = _resolve_config_entries(case_dir, [config.get("wave_file")], "wave")
            if not compile_logs:
                compile_logs = _reuse_mixed_sim_logs(sim_logs)
            available_cases = []
    else:
        sim_base = request_root if discovery_mode == "case_dir" else config_root
        sim_logs = _resolve_config_entries(sim_base, [config.get("sim_log")], "sim")
        wave_files = _resolve_config_entries(sim_base, [config.get("wave_file")], "wave")
        available_cases = []
    return _build_discovery_result(
        request_root=request_root,
        case_name=case_name,
        config_source=MCP_CONFIG_FILE,
        config_root=config_root,
        discovery_mode=discovery_mode,
        target_case_dir=case_dir,
        compile_logs=compile_logs,
        sim_logs=sim_logs,
        wave_files=wave_files,
        available_cases=available_cases,
        hints=hints,
    )


def _build_discovery_result(
    request_root: Path,
    case_name: str | None,
    config_source: str,
    config_root: Path | None,
    discovery_mode: str,
    target_case_dir: Path | None,
    compile_logs: list[dict[str, Any]],
    sim_logs: list[dict[str, Any]],
    wave_files: list[dict[str, Any]],
    available_cases: list[dict[str, Any]],
    hints: list[str],
) -> dict[str, Any]:
    simulator = _detect_simulator_from_logs(compile_logs, sim_logs)
    fsdb_runtime = get_fsdb_runtime_info()
    merged_hints = list(hints)
    merged_hints.extend(_generate_hints(request_root, case_name, compile_logs, sim_logs, wave_files, fsdb_runtime))
    merged_hints = list(dict.fromkeys(merged_hints))
    result = {
        "verif_root": str(request_root),
        "case_name": case_name,
        "config_source": config_source,
        "config_root": str(config_root) if config_root else None,
        "discovery_mode": discovery_mode,
        "case_dir": str(target_case_dir) if target_case_dir else None,
        "simulator": simulator,
        "fsdb_runtime": fsdb_runtime,
        "compile_logs": _strip_sort_fields(compile_logs),
        "sim_logs": _strip_sort_fields(sim_logs),
        "wave_files": _strip_sort_fields(wave_files),
        "available_cases": available_cases,
        "hints": merged_hints,
    }

    elaborate_log = next(
        (log for log in compile_logs if log.get("phase") == "elaborate"),
        None,
    )
    target_log = elaborate_log or (compile_logs[0] if compile_logs else None)
    if target_log:
        result["next_required_step"] = {
            "tool": "build_tb_hierarchy",
            "compile_log": target_log["path"],
            "simulator": simulator or "auto",
            "reason": "Must be called before reading any RTL/TB source files. "
                      "Returns a slim payload with a hierarchy_handle; "
                      "verify any file path via lookup_tb_files / get_tb_file_detail "
                      "before reading source.",
        }

    return result


def _stem_named_sim_logs(directory: Path) -> list[dict[str, Any]]:
    """A ``<dirname>.log`` inside a case dir is the simulation log even when its
    name matches no SIM_LOG_PATTERNS (e.g. ``work/<case>/<case>.log``). Zero-FP:
    keyed on the file stem equalling the directory name, so it never picks up
    ``comp.log`` / ``elab.log`` or another case's log."""
    candidate = directory / f"{directory.name}.log"
    if candidate.is_file():
        return [_collect_file_info(candidate)]
    return []


def _sim_logs_in_dir(directory: Path) -> list[dict[str, Any]]:
    """Simulation logs directly in ``directory`` (depth 0): SIM_LOG_PATTERNS
    matches plus a ``<dirname>.log`` stem-named sim log."""
    return _dedupe_sorted(
        _search_files([directory], SIM_LOG_PATTERNS, 0) + _stem_named_sim_logs(directory)
    )


def _classify_directory(root: Path) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], list[Path]]:
    local_sim_logs = _sim_logs_in_dir(root)
    local_wave_files = _search_files([root], WAVE_PATTERNS, 0)
    child_case_dirs = _find_immediate_case_dirs(root)
    # Prefix-named nested case dirs (work_*/sim_*/case_*) mark a cases-container:
    # they outrank loose aggregate logs sitting at the container top (e.g. a
    # results dir holding both run_*.log/comp_*.log AND work_<case>/ subdirs), so
    # pointing at such a container classifies as root_dir rather than letting the
    # loose logs make it look like a single case dir and shadow the real cases.
    if child_case_dirs:
        return "root_dir", local_sim_logs, local_wave_files, child_case_dirs
    if local_sim_logs or local_wave_files:
        return "case_dir", local_sim_logs, local_wave_files, child_case_dirs
    return "unknown", local_sim_logs, local_wave_files, child_case_dirs


def _resolve_config_entries(base_dir: Path, entries: list[Any], kind: str) -> list[dict[str, Any]]:
    results = []
    for entry in entries:
        if not entry:
            continue
        path = (base_dir / str(entry)).resolve()
        if path.exists():
            info = _collect_file_info(path)
            if kind == "compile":
                info["phase"] = _detect_log_phase(path)
            elif kind == "wave":
                info["format"] = path.suffix.lstrip(".").lower()
            results.append(info)
    return _dedupe_sorted(results)


def _list_child_dirs(root: Path) -> list[Path]:
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError:
        return []
    return [child for child in children if child.is_dir()]


def _holds_wave_or_simlog(directory: Path) -> bool:
    return bool(
        _sim_logs_in_dir(directory)
        or _search_files([directory], WAVE_PATTERNS, 0)
    )


def _has_case_prefix(name: str) -> bool:
    return name.lower().startswith(_CASE_PREFIXES)


def _find_immediate_case_dirs(root: Path) -> list[Path]:
    """Enumerate the case directories under ``root``.

    A case directory directly holds a sim log or a waveform. To handle the
    common results-container layout — ``root/work/work_<case>/dump.fsdb``, where
    ``work/`` also carries loose aggregate ``run_*.log``/``comp_*.log`` at its
    top — a child that contains prefix-named (``work_``/``sim_``/``case_``)
    sub-directories holding waves/logs is treated as a container: its nested
    per-case dirs are the case dirs, not the container itself. This stops the
    container's loose aggregate logs from shadowing the real per-case dirs (the
    failure where pointing at the project root found only ``work/`` and missed
    every ``work_<case>/`` inside it). The prefix gate keeps a flat case dir's
    build artifacts (``csrc``/``simv.daidir``/``dump``) from being misread as
    nested cases, so a real flat case dir still resolves to itself.
    """
    matches: list[Path] = []
    for child in _list_child_dirs(root):
        nested = [
            grandchild
            for grandchild in _list_child_dirs(child)
            if _has_case_prefix(grandchild.name) and _holds_wave_or_simlog(grandchild)
        ]
        if nested:
            matches.extend(grandchild.resolve() for grandchild in nested)
        elif _holds_wave_or_simlog(child):
            matches.append(child.resolve())
    return matches


def _normalize_case_token(name: str) -> str:
    lowered = name.lower()
    for prefix in _CASE_PREFIXES:
        if lowered.startswith(prefix):
            return lowered[len(prefix):]
    return lowered


def _match_case_dirs(case_dirs: list[Path], case_name: str) -> list[Path]:
    needle = case_name.lower()
    exact_matches = [path for path in case_dirs if path.name.lower() == needle]
    if exact_matches:
        return exact_matches

    normalized = _normalize_case_token(case_name)
    normalized_matches = [
        path for path in case_dirs
        if _normalize_case_token(path.name) == normalized
    ]
    return normalized_matches


def _case_name_matches_dir(case_dir: Path, case_name: str) -> bool:
    return bool(_match_case_dirs([case_dir], case_name))


def _describe_case_dirs(case_dirs: list[Path]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for directory in case_dirs:
        sim_logs = _sim_logs_in_dir(directory)
        wave_files = _search_files([directory], WAVE_PATTERNS, 0)
        cases.append(
            {
                "name": _extract_case_name(directory.name),
                "dir": str(directory.resolve()),
                "has_sim_log": bool(sim_logs),
                "has_wave": bool(wave_files),
            }
        )
    cases.sort(key=lambda item: item["name"])
    return cases


def _search_files(dirs: list[Path], patterns: list[str], max_depth: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for base_dir in dirs:
        base = Path(base_dir)
        if not base.is_dir():
            continue
        for path in _iter_files(base, max_depth):
            if not any(fnmatch.fnmatch(path.name.lower(), pattern.lower()) for pattern in patterns):
                continue
            info = _collect_file_info(path)
            if patterns == COMPILE_LOG_PATTERNS:
                info["phase"] = _detect_log_phase(path)
            elif path.suffix.lower() in {".fsdb", ".vcd"}:
                info["format"] = path.suffix.lstrip(".").lower()
            results.append(info)
    return _dedupe_sorted(results)


def _detect_log_phase(log_path: Path) -> str:
    return _scan_log_phase(log_path, _DEFAULT_LOG_PHASE_SCAN_LINES)


def _scan_log_phase(log_path: Path, max_lines: int) -> str:
    try:
        with log_path.open("r", errors="replace") as handle:
            sample = "".join(line.lower() for _, line in zip(range(max_lines), handle))
    except OSError:
        return "unknown"
    if any(keyword in sample for keyword in _ELABORATE_KEYWORDS):
        return "elaborate"
    if any(keyword in sample for keyword in _COMPILE_KEYWORDS):
        return "compile"
    return "unknown"


def _detect_mixed_compile_log_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    path = Path(entry["path"])
    phase = _scan_log_phase(path, _DEFAULT_LOG_PHASE_SCAN_LINES)
    if phase == "unknown":
        phase = _scan_log_phase(path, _EXTENDED_LOG_PHASE_SCAN_LINES)
    if phase not in {"compile", "elaborate"}:
        return None
    mixed_entry = dict(entry)
    mixed_entry["phase"] = phase
    mixed_entry["is_mixed"] = True
    return mixed_entry


def _reuse_mixed_sim_logs(sim_logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for entry in sim_logs:
        mixed_entry = _detect_mixed_compile_log_entry(entry)
        if mixed_entry is not None:
            return [mixed_entry]
    return []


def _discover_case_compile_logs(
    case_dir: Path, sim_logs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Locate compile/elab logs for a case directory, in priority order.

    ``① case dir → ② parent top → ③ sibling build/elab dir (E_NAME layout) →
    ④ a mixed sim log reused as a compile log``. The sibling step covers the
    makefile flow that keeps run artifacts in ``work/<case>/`` and elaboration
    artifacts in a separate ``work/<E_NAME>/`` sibling, so the compile/elab log
    is neither inside the case dir nor at the parent top.
    """
    local = _search_files([case_dir], COMPILE_LOG_PATTERNS, 0)
    parent = (
        _search_files([case_dir.parent], COMPILE_LOG_PATTERNS, 0)
        if case_dir.parent.is_dir()
        else []
    )
    compile_logs = local or parent
    if not compile_logs:
        compile_logs = _discover_sibling_build_compile_logs(case_dir)
    if not compile_logs:
        compile_logs = _reuse_mixed_sim_logs(sim_logs)
    return compile_logs


def _is_build_dir(directory: Path) -> bool:
    """Structural test for an elaboration/build output directory.

    A build dir holds a compile/elab log but no sim log or waveform — the
    signature that separates an elaboration output dir (e.g. ``work/DEF_ELAB/``)
    from a sibling case directory, which holds the run log + waveform. ``E_NAME``
    is configurable (``DEF_ELAB``/``PG_ELAB``/``FPGA_ELAB``/…), so discovery keys
    on this structure rather than the directory name.
    """
    if not _search_files([directory], COMPILE_LOG_PATTERNS, 0):
        return False
    return not _holds_wave_or_simlog(directory)


def _discover_sibling_build_compile_logs(case_dir: Path) -> list[dict[str, Any]]:
    """Compile/elab logs from build dirs that are siblings of ``case_dir``.

    Covers the makefile layout where run artifacts live in ``work/<case>/`` and
    elaboration artifacts live in a separate ``work/<E_NAME>/`` sibling, so the
    compile/elab log is not co-located with — and not inside — the case dir. A
    sibling qualifies only if it is a build dir per :func:`_is_build_dir`, which
    keeps another case directory's own compile log from being pulled in.
    """
    parent = case_dir.parent
    if not parent.is_dir():
        return []
    case_dir_resolved = case_dir.resolve()
    build_dirs = [
        child
        for child in _list_child_dirs(parent)
        if child.resolve() != case_dir_resolved and _is_build_dir(child)
    ]
    return _search_files(build_dirs, COMPILE_LOG_PATTERNS, 0)


def _detect_simulator_from_logs(
    compile_logs: list[dict[str, Any]], sim_logs: list[dict[str, Any]]
) -> str | None:
    for entry in compile_logs + sim_logs:
        sim = detect_simulator(entry["path"])
        if sim != "unknown":
            return sim
    return None


def _discover_cases(verif_root: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for directory in _iter_dirs(verif_root, CASE_DIR_MAX_DEPTH):
        sim_logs = _sim_logs_in_dir(directory)
        wave_files = _search_files([directory], WAVE_PATTERNS, 0)
        if not sim_logs and not wave_files:
            continue
        cases.append(
            {
                "name": _extract_case_name(directory.name),
                "dir": str(directory.resolve()),
                "has_sim_log": bool(sim_logs),
                "has_wave": bool(wave_files),
            }
        )
    cases.sort(key=lambda item: item["name"])
    return cases


def _collect_file_info(path: Path) -> dict[str, Any]:
    stat = path.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime)
    age_hours = round((datetime.now() - mtime).total_seconds() / 3600, 1)
    return {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime": mtime.strftime("%Y-%m-%d %H:%M:%S"),
        "mtime_epoch": stat.st_mtime,
        "age_hours": age_hours,
    }


def _dedupe_sorted(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for entry in entries:
        deduped[entry["path"]] = entry
    return sorted(deduped.values(), key=lambda item: (-item["mtime_epoch"], item["path"]))


def _strip_sort_fields(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in entry.items() if key != "mtime_epoch"} for entry in entries]


def _generate_hints(
    verif_root: Path,
    case_name: str | None,
    compile_logs: list[dict[str, Any]],
    sim_logs: list[dict[str, Any]],
    wave_files: list[dict[str, Any]],
    fsdb_runtime: dict[str, Any],
) -> list[str]:
    hints: list[str] = []
    if not compile_logs:
        hints.append(f"No compile/elab log found under {verif_root}")
    mixed_logs = [log for log in compile_logs if log.get("is_mixed")]
    if mixed_logs:
        names = ", ".join(Path(log["path"]).name for log in mixed_logs)
        hints.append(
            f"{names} reused from sim_logs because it contains compile/elaborate markers"
        )
    if len(compile_logs) > 1:
        hints.append(f"Found {len(compile_logs)} compile logs, using the newest one is recommended")
    if compile_logs and not any(log.get("phase") == "elaborate" for log in compile_logs):
        hints.append("No elaborate-phase log found. build_tb_hierarchy may return partial results")

    if case_name is not None and not sim_logs:
        hints.append(f"No simulation log found for case {case_name}")
    if case_name is not None and not wave_files:
        hints.append("No waveform file found. Simulation may not have dumped waves")

    for entry in sim_logs:
        if entry["size"] == 0:
            hints.append("Simulation log is empty (0 bytes), simulation may not have completed")
            break

    for entry in wave_files:
        if entry["size"] < 1024:
            hints.append("Waveform file is very small, simulation may have aborted early")
            break

    for entry in compile_logs + sim_logs + wave_files:
        if entry["age_hours"] > 24:
            hints.append(f"File is {int(entry['age_hours'])} hours old, may not match current source code")

    has_fsdb = any(entry.get("format") == "fsdb" or entry["path"].lower().endswith(".fsdb") for entry in wave_files)
    has_vcd = any(entry.get("format") == "vcd" or entry["path"].lower().endswith(".vcd") for entry in wave_files)
    if has_fsdb and not fsdb_runtime["enabled"]:
        hint = f"{fsdb_runtime['message']}. FSDB parsing is disabled"
        if has_vcd:
            hint += "; prefer VCD waveforms in downstream workflow"
        hints.append(hint)
    return hints


def _iter_dirs(root: Path, max_depth: int):
    root_depth = len(root.parts)
    for current, dirnames, _ in os.walk(root):
        current_path = Path(current)
        depth = len(current_path.parts) - root_depth
        if depth > max_depth:
            dirnames[:] = []
            continue
        if depth > 0:
            yield current_path


def _iter_files(root: Path, max_depth: int, followlinks: bool = False):
    root_depth = len(root.parts)
    for current, dirnames, filenames in os.walk(root, followlinks=followlinks):
        current_path = Path(current)
        depth = len(current_path.parts) - root_depth
        if depth > max_depth:
            dirnames[:] = []
            continue
        for filename in filenames:
            yield current_path / filename


def _extract_case_name(dirname: str) -> str:
    lowered = dirname.lower()
    for prefix in _CASE_PREFIXES:
        if lowered.startswith(prefix):
            return dirname[len(prefix):]
    return dirname
