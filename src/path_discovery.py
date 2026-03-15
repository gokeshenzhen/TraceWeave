"""
path_discovery.py
自动发现 verif/ 下的编译日志、仿真日志和波形文件。
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
    DISCOVER_MAX_DEPTH_ROOT,
    get_fsdb_runtime_info,
    MCP_CONFIG_FILE,
    SIM_LOG_PATTERNS,
    WAVE_PATTERNS,
)
from src.compile_log_parser import detect_simulator


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


def discover_sim_paths(verif_root: str, case_name: str | None = None) -> dict[str, Any]:
    root = Path(verif_root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"verif_root 不是目录: {verif_root}")

    config_match = _load_mcp_config(root)
    if config_match is not None:
        return _discover_from_config(root, case_name, *config_match)

    return _discover_auto(root, case_name)


def _discover_auto(root: Path, case_name: str | None) -> dict[str, Any]:
    discovery_mode, local_sim_logs, local_wave_files, child_case_dirs = _classify_directory(root)

    target_case_dir: Path | None = None
    hints: list[str] = []

    if discovery_mode == "case_dir":
        target_case_dir = root
        sim_logs = local_sim_logs
        wave_files = local_wave_files
        local_compile_logs = _search_files([root], COMPILE_LOG_PATTERNS, 0)
        parent_compile_logs = _search_files([root.parent], COMPILE_LOG_PATTERNS, 0) if root.parent.is_dir() else []
        compile_logs = local_compile_logs or parent_compile_logs
        if not compile_logs:
            compile_logs = _reuse_mixed_sim_logs(sim_logs)
        available_cases = []
        if case_name and not _case_name_matches_dir(root, case_name):
            hints.append(
                f"Requested case_name '{case_name}' does not match current case directory '{root.name}'"
            )
    elif discovery_mode == "root_dir":
        root_compile_logs = _search_files([root], COMPILE_LOG_PATTERNS, 0)
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
                sim_logs = _search_files([target_case_dir], SIM_LOG_PATTERNS, DISCOVER_MAX_DEPTH_CASE)
                wave_files = _search_files([target_case_dir], WAVE_PATTERNS, DISCOVER_MAX_DEPTH_CASE)
                case_compile_logs = _search_files([target_case_dir], COMPILE_LOG_PATTERNS, 0)
                compile_logs = root_compile_logs or case_compile_logs
                if not compile_logs:
                    compile_logs = _reuse_mixed_sim_logs(sim_logs)
                available_cases = []
            else:
                sim_logs = []
                wave_files = []
                compile_logs = root_compile_logs
                available_cases = []
                hints.append(
                    f"No case directory matched case_name '{case_name}' under {root}"
                )
        else:
            compile_logs = root_compile_logs
            sim_logs = []
            wave_files = []
            available_cases = _describe_case_dirs(child_case_dirs)
    else:
        compile_logs = _search_files([root], COMPILE_LOG_PATTERNS, 0)
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


def _load_mcp_config(start_dir: Path) -> tuple[Path, dict[str, Any]] | None:
    current = start_dir
    while True:
        config_path = current / MCP_CONFIG_FILE
        if config_path.exists():
            with config_path.open("r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"{config_path} 必须是 YAML object")
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
    return {
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


def _classify_directory(root: Path) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], list[Path]]:
    local_sim_logs = _search_files([root], SIM_LOG_PATTERNS, 0)
    local_wave_files = _search_files([root], WAVE_PATTERNS, 0)
    child_case_dirs = _find_immediate_case_dirs(root)
    if local_sim_logs or local_wave_files:
        return "case_dir", local_sim_logs, local_wave_files, child_case_dirs
    if child_case_dirs:
        return "root_dir", local_sim_logs, local_wave_files, child_case_dirs
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


def _find_case_dirs(verif_root: Path, case_name: str, max_depth: int) -> list[Path]:
    needle = case_name.lower()
    matches: list[Path] = []
    for path in _iter_dirs(verif_root, max_depth):
        if needle in path.name.lower():
            matches.append(path)
    return sorted(matches)


def _find_immediate_case_dirs(root: Path) -> list[Path]:
    matches: list[Path] = []
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError:
        return matches
    for child in children:
        if not child.is_dir():
            continue
        if _search_files([child], SIM_LOG_PATTERNS, 0) or _search_files([child], WAVE_PATTERNS, 0):
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
        sim_logs = _search_files([directory], SIM_LOG_PATTERNS, 0)
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
        sim_logs = _search_files([directory], SIM_LOG_PATTERNS, 0)
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


def _iter_files(root: Path, max_depth: int):
    root_depth = len(root.parts)
    for current, dirnames, filenames in os.walk(root):
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
