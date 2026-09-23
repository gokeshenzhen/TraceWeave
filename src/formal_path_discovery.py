"""Bounded, semantics-free discovery of local formal artifacts."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol, Sequence
import fnmatch
import os

from config import (
    FORMAL_DISCOVERY_MAX_DEPTH,
    FORMAL_DISCOVERY_MAX_ENTRIES,
    FORMAL_DISCOVERY_MAX_FILES_PER_ROLE,
    FORMAL_DISCOVERY_MAX_PROJECTS,
    JASPERGOLD_EXCLUDED_DIR_NAMES,
    JASPERGOLD_EXCLUDED_FILE_SUFFIXES,
)


_WAVE_SUFFIXES = {".fsdb", ".vcd"}
_MAX_HINTS = 16


@dataclass(frozen=True)
class FormalDiscoveryLimits:
    max_depth: int = FORMAL_DISCOVERY_MAX_DEPTH
    max_entries: int = FORMAL_DISCOVERY_MAX_ENTRIES
    max_projects: int = FORMAL_DISCOVERY_MAX_PROJECTS
    max_files_per_role: int = FORMAL_DISCOVERY_MAX_FILES_PER_ROLE

    def __post_init__(self) -> None:
        for name, value in (
            ("max_depth", self.max_depth),
            ("max_entries", self.max_entries),
            ("max_projects", self.max_projects),
            ("max_files_per_role", self.max_files_per_role),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class _IndexedEntry:
    logical_path: Path
    canonical_path: Path
    relative_parts: tuple[str, ...]
    is_dir: bool
    size: int
    mtime: float


@dataclass(frozen=True)
class _ProviderProject:
    path: Path
    evidence: tuple[str, ...]
    has_session_log: bool


@dataclass(frozen=True)
class _ProviderLog:
    entry: _IndexedEntry
    role: str
    project_path: Path


@dataclass(frozen=True)
class _ProviderResult:
    projects: tuple[_ProviderProject, ...]
    logs: tuple[_ProviderLog, ...]


class FormalArtifactProvider(Protocol):
    """Small internal boundary for tool-specific path conventions."""

    name: str

    def excludes(self, relative_parts: tuple[str, ...], *, is_dir: bool) -> bool: ...

    def analyze(self, entries: Sequence[_IndexedEntry]) -> _ProviderResult: ...

    def classify_explicit_log(
        self, entry: _IndexedEntry
    ) -> tuple[str, Path | None]: ...


class JasperGoldArtifactProvider:
    name = "jaspergold"

    _excluded_dirs = frozenset(JASPERGOLD_EXCLUDED_DIR_NAMES)
    _excluded_suffixes = tuple(JASPERGOLD_EXCLUDED_FILE_SUFFIXES)

    def excludes(self, relative_parts: tuple[str, ...], *, is_dir: bool) -> bool:
        name = relative_parts[-1]
        lower_name = name.lower()
        if is_dir:
            if lower_name in self._excluded_dirs:
                return True
            return self._is_session_generated_dir(relative_parts)
        if lower_name.startswith(".#"):
            return True
        return lower_name.endswith(self._excluded_suffixes)

    @staticmethod
    def _is_session_generated_dir(parts: tuple[str, ...]) -> bool:
        if not parts or len(parts) < 3:
            return False
        return (
            parts[-1].lower() in {"profile", "settings", "work"}
            and parts[-3].lower() == "sessionlogs"
            and parts[-2].lower().startswith("session_")
        )

    def analyze(self, entries: Sequence[_IndexedEntry]) -> _ProviderResult:
        evidence: dict[Path, set[str]] = defaultdict(set)
        session_projects: set[Path] = set()
        log_candidates: list[_ProviderLog] = []

        for entry in entries:
            if entry.is_dir:
                continue
            role, project_path, marker = self._classify(entry)
            if project_path is None:
                continue
            if marker is not None:
                evidence[project_path].add(marker)
            if role == "session":
                session_projects.add(project_path)
            if role is not None:
                log_candidates.append(
                    _ProviderLog(
                        entry=entry,
                        role=role,
                        project_path=project_path,
                    )
                )

        strong_markers = {
            "jg_console_log",
            "current_session_log",
            "current_session_command",
        }
        detected_paths = {
            path for path, markers in evidence.items() if markers & strong_markers
        }
        projects = tuple(
            _ProviderProject(
                path=path,
                evidence=tuple(sorted(evidence[path])),
                has_session_log=path in session_projects,
            )
            for path in sorted(detected_paths, key=os.fspath)
        )
        logs = tuple(
            item
            for item in sorted(
                log_candidates,
                key=lambda item: (
                    os.fspath(item.project_path),
                    item.role,
                    os.fspath(item.entry.canonical_path),
                ),
            )
            if item.project_path in detected_paths
        )
        return _ProviderResult(projects=projects, logs=logs)

    def classify_explicit_log(self, entry: _IndexedEntry) -> tuple[str, Path | None]:
        role, project_path, _marker = self._classify(entry)
        return role or "unclassified", project_path

    def _classify(
        self, entry: _IndexedEntry
    ) -> tuple[str | None, Path | None, str | None]:
        parts = entry.relative_parts
        name = parts[-1]
        lower_name = name.lower()

        if lower_name == "jg_console.log":
            return "console", entry.canonical_path.parent, "jg_console_log"
        if lower_name == "bridge.log":
            return "infrastructure", entry.canonical_path.parent, "bridge_log"

        session_index = _current_session_index(parts)
        if session_index is not None:
            project_path = _canonical_ancestor(
                entry.logical_path, len(parts) - session_index
            )
            if fnmatch.fnmatchcase(lower_name, "jg_session_*.log"):
                return "session", project_path, "current_session_log"
            if fnmatch.fnmatchcase(lower_name, "jg_session_*.cmd"):
                return "command", project_path, "current_session_command"

        if len(parts) >= 2 and parts[-2].lower() == ".tmp":
            project_path = entry.canonical_path.parent.parent
            if lower_name == ".initcmds.tcl":
                return None, project_path, "init_commands"
            if lower_name == ".postcmds.tcl":
                return None, project_path, "post_commands"

        return None, None, None


_JASPERGOLD_PROVIDER = JasperGoldArtifactProvider()
_PROVIDERS: dict[str, FormalArtifactProvider] = {
    _JASPERGOLD_PROVIDER.name: _JASPERGOLD_PROVIDER,
}


def discover_formal_paths(
    formal_root: str,
    *,
    formal_tool: str = "auto",
    project_dir: str | None = None,
    formal_log: str | None = None,
    wave_file: str | None = None,
    limits: FormalDiscoveryLimits | None = None,
) -> dict:
    """Discover formal projects, role-labelled logs, and VCD/FSDB files.

    This function intentionally returns no property or trace semantics.
    ``limits`` is an internal/test seam; it is not an MCP argument.
    """

    root = Path(formal_root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"formal_root is not a directory: {formal_root}")

    requested_formal_tool = str(formal_tool).strip().lower()
    providers = _select_providers(requested_formal_tool)
    effective_limits = limits or FormalDiscoveryLimits()
    explicit_project = _resolve_explicit_path(
        root, project_dir, label="project_dir", expect="directory"
    )
    explicit_log = _resolve_explicit_path(
        root, formal_log, label="formal_log", expect="file"
    )
    explicit_wave = _resolve_explicit_path(
        root, wave_file, label="wave_file", expect="file"
    )
    if explicit_log is not None and _is_proprietary_database(explicit_log):
        raise ValueError("formal_log must not select a proprietary proof database")
    if explicit_wave is not None and explicit_wave.suffix.lower() not in _WAVE_SUFFIXES:
        raise ValueError("wave_file must have a .vcd or .fsdb suffix")

    scan_root = explicit_project or root
    walk = _walk_formal_root(scan_root, root, providers, effective_limits)
    provider_results = [
        (provider, provider.analyze(walk.entries)) for provider in providers
    ]

    truncation_reasons = list(walk.truncation_reasons)
    degradation_reasons = list(walk.degradation_reasons)
    projects = _collect_projects(
        provider_results,
        explicit_project=explicit_project,
        max_projects=effective_limits.max_projects,
        truncation_reasons=truncation_reasons,
    )
    formal_logs = _collect_logs(
        provider_results,
        projects=projects,
        explicit_log=explicit_log,
        scan_root=scan_root,
        providers=providers,
        max_files_per_role=effective_limits.max_files_per_role,
        truncation_reasons=truncation_reasons,
    )
    wave_files = _collect_waves(
        walk.entries,
        explicit_wave=explicit_wave,
        max_files=effective_limits.max_files_per_role,
        truncation_reasons=truncation_reasons,
    )

    project_paths = {Path(item["path"]) for item in projects}
    selected_project_dir = os.fspath(explicit_project) if explicit_project else None
    available_runs = [
        {
            "name": _relative_display_name(root, project_path),
            "dir": os.fspath(project_path),
            "detected_formal_tool": next(
                item["detected_formal_tool"]
                for item in projects
                if item["path"] == os.fspath(project_path)
            ),
            "has_wave": any(
                _is_within(Path(wave["path"]), project_path) for wave in wave_files
            ),
        }
        for project_path in sorted(project_paths, key=os.fspath)
    ]

    explicit_mode = any(
        value is not None for value in (project_dir, formal_log, wave_file)
    )
    if explicit_mode:
        discovery_mode = "explicit"
    elif root in project_paths:
        discovery_mode = "formal_project_dir"
    elif projects:
        discovery_mode = "formal_root"
    elif wave_files:
        discovery_mode = "wave_only"
    else:
        discovery_mode = "unknown"

    truncation_reasons = sorted(set(truncation_reasons))
    degradation_reasons = sorted(set(degradation_reasons))
    if truncation_reasons:
        coverage_status: Literal["complete", "truncated", "degraded"] = "truncated"
    elif degradation_reasons:
        coverage_status = "degraded"
    else:
        coverage_status = "complete"

    hints = list(walk.hints)
    if not projects and not wave_files:
        if coverage_status == "complete":
            hints.append(f"No supported formal artifacts found under {scan_root}")
        else:
            hints.append("No supported formal artifacts found in the scanned prefix")
    if wave_files and not projects:
        hints.append(
            "Exported waveforms can be read without a recognized formal project. "
            "Choose a wave_files path, signals and timestamp; use get_signal_at_time "
            "for one signal or get_signals_around_time(return_mode=\"values_only\", "
            "window_ps=0, extra_transitions=0) for several. Use get_waveform_summary "
            "for units and search_signals for path lookup when needed."
        )
    if any(item["project_layout_state"] == "markers_only" for item in projects):
        hints.append("Some JasperGold projects have markers but no current session log")
    if any(item["project_layout_state"] == "explicit_unverified" for item in projects):
        hints.append("Explicit project directory has no supported provider markers")
    hints = _dedupe_strings(hints)[:_MAX_HINTS]

    return {
        "formal_root": os.fspath(root),
        "requested_formal_tool": requested_formal_tool,
        "detected_formal_tools": sorted(
            {
                item["detected_formal_tool"]
                for item in projects
                if item["detected_formal_tool"] is not None
            }
        ),
        "discovery_mode": discovery_mode,
        "selected_project_dir": selected_project_dir,
        "projects": projects,
        "formal_logs": formal_logs,
        "wave_files": wave_files,
        "available_runs": available_runs,
        "coverage": {
            "status": coverage_status,
            "directories_examined": walk.directories_examined,
            "files_examined": walk.files_examined,
            "entries_examined": walk.entries_examined,
            "max_depth": effective_limits.max_depth,
            "max_entries": effective_limits.max_entries,
            "max_projects": effective_limits.max_projects,
            "max_files_per_role": effective_limits.max_files_per_role,
            "truncation_reasons": truncation_reasons,
            "degradation_reasons": degradation_reasons,
            "skipped_count": walk.skipped_count,
        },
        "hints": hints,
    }


@dataclass(frozen=True)
class _WalkResult:
    entries: tuple[_IndexedEntry, ...]
    directories_examined: int
    files_examined: int
    entries_examined: int
    skipped_count: int
    truncation_reasons: tuple[str, ...]
    degradation_reasons: tuple[str, ...]
    hints: tuple[str, ...]


def _walk_formal_root(
    scan_root: Path,
    containment_root: Path,
    providers: Sequence[FormalArtifactProvider],
    limits: FormalDiscoveryLimits,
) -> _WalkResult:
    queue: deque[tuple[Path, tuple[str, ...], int]] = deque([(scan_root, (), 0)])
    root_stat = scan_root.stat()
    visited_dirs = {(root_stat.st_dev, root_stat.st_ino)}
    entries: list[_IndexedEntry] = []
    truncation_reasons: set[str] = set()
    degradation_reasons: set[str] = set()
    hints: list[str] = []
    directories_examined = 0
    files_examined = 0
    entries_examined = 0
    skipped_count = 0

    while queue and entries_examined < limits.max_entries:
        directory, relative_prefix, depth = queue.popleft()
        directories_examined += 1
        try:
            with os.scandir(directory) as iterator:
                children = []
                for child in iterator:
                    if entries_examined + len(children) >= limits.max_entries:
                        truncation_reasons.add("max_entries")
                        break
                    children.append(child)
        except OSError:
            skipped_count += 1
            degradation_reasons.add("directory_access_error")
            _append_hint(hints, f"Could not read directory: {directory}")
            continue

        children.sort(key=lambda item: item.name)
        for child in children:
            entries_examined += 1
            logical_path = Path(child.path)
            relative_parts = relative_prefix + (child.name,)
            try:
                is_dir = child.is_dir(follow_symlinks=True)
                is_file = child.is_file(follow_symlinks=True)
                canonical_path = logical_path.resolve(strict=True)
            except OSError:
                skipped_count += 1
                degradation_reasons.add("entry_access_error")
                _append_hint(hints, f"Could not inspect entry: {logical_path}")
                continue

            if child.is_symlink() and not _is_within(canonical_path, containment_root):
                skipped_count += 1
                degradation_reasons.add("outside_root_symlink")
                _append_hint(
                    hints, f"Skipped symlink outside formal_root: {logical_path}"
                )
                continue
            if not is_dir and not is_file:
                skipped_count += 1
                continue
            if any(
                provider.excludes(relative_parts, is_dir=is_dir)
                for provider in providers
            ):
                skipped_count += 1
                continue

            try:
                stat_result = canonical_path.stat()
            except OSError:
                skipped_count += 1
                degradation_reasons.add("entry_stat_error")
                _append_hint(hints, f"Could not stat entry: {logical_path}")
                continue

            entry = _IndexedEntry(
                logical_path=logical_path,
                canonical_path=canonical_path,
                relative_parts=relative_parts,
                is_dir=is_dir,
                size=stat_result.st_size,
                mtime=stat_result.st_mtime,
            )
            entries.append(entry)
            if is_file:
                files_examined += 1
                continue

            identity = (stat_result.st_dev, stat_result.st_ino)
            if identity in visited_dirs:
                continue
            visited_dirs.add(identity)
            child_depth = depth + 1
            if child_depth >= limits.max_depth:
                truncation_reasons.add("max_depth")
                continue
            queue.append((canonical_path, relative_parts, child_depth))

    if queue:
        truncation_reasons.add("max_entries")

    return _WalkResult(
        entries=tuple(entries),
        directories_examined=directories_examined,
        files_examined=files_examined,
        entries_examined=entries_examined,
        skipped_count=skipped_count,
        truncation_reasons=tuple(sorted(truncation_reasons)),
        degradation_reasons=tuple(sorted(degradation_reasons)),
        hints=tuple(hints),
    )


def _collect_projects(
    provider_results: Sequence[tuple[FormalArtifactProvider, _ProviderResult]],
    *,
    explicit_project: Path | None,
    max_projects: int,
    truncation_reasons: list[str],
) -> list[dict]:
    merged: dict[Path, dict] = {}
    for provider, result in provider_results:
        for project in result.projects:
            merged[project.path] = {
                "path": os.fspath(project.path),
                "detected_formal_tool": provider.name,
                "project_layout_state": (
                    "session_present" if project.has_session_log else "markers_only"
                ),
                "detection_evidence": list(project.evidence),
            }

    if explicit_project is not None and explicit_project not in merged:
        merged[explicit_project] = {
            "path": os.fspath(explicit_project),
            "detected_formal_tool": None,
            "project_layout_state": "explicit_unverified",
            "detection_evidence": [],
        }

    ordered = [merged[path] for path in sorted(merged, key=os.fspath)]
    if len(ordered) > max_projects:
        truncation_reasons.append("max_projects")
        ordered = ordered[:max_projects]
    return ordered


def _collect_logs(
    provider_results: Sequence[tuple[FormalArtifactProvider, _ProviderResult]],
    *,
    projects: Sequence[dict],
    explicit_log: Path | None,
    scan_root: Path,
    providers: Sequence[FormalArtifactProvider],
    max_files_per_role: int,
    truncation_reasons: list[str],
) -> list[dict]:
    project_paths = {Path(item["path"]) for item in projects}
    candidates: list[tuple[_IndexedEntry, str, Path | None]] = []
    if explicit_log is not None:
        entry = _entry_for_explicit(explicit_log, scan_root)
        role = "unclassified"
        inferred_project: Path | None = None
        for provider in providers:
            candidate_role, candidate_project = provider.classify_explicit_log(entry)
            if candidate_role != "unclassified":
                role = candidate_role
                inferred_project = candidate_project
                break
        associated = _closest_containing_project(explicit_log, project_paths)
        candidates.append((entry, role, associated or inferred_project))
    else:
        for _provider, result in provider_results:
            for item in result.logs:
                if item.project_path in project_paths:
                    candidates.append((item.entry, item.role, item.project_path))

    by_role_count: dict[str, int] = defaultdict(int)
    seen: set[Path] = set()
    result: list[dict] = []
    for entry, role, project_path in sorted(
        candidates, key=lambda item: (item[1], os.fspath(item[0].canonical_path))
    ):
        if entry.canonical_path in seen:
            continue
        seen.add(entry.canonical_path)
        if by_role_count[role] >= max_files_per_role:
            truncation_reasons.append(f"max_files_per_role:{role}")
            continue
        by_role_count[role] += 1
        result.append(
            {
                **_file_metadata(entry),
                "role": role,
                "project_dir": (
                    os.fspath(project_path) if project_path in project_paths else None
                ),
            }
        )
    return result


def _collect_waves(
    entries: Sequence[_IndexedEntry],
    *,
    explicit_wave: Path | None,
    max_files: int,
    truncation_reasons: list[str],
) -> list[dict]:
    if explicit_wave is not None:
        candidates = [_entry_for_explicit(explicit_wave, explicit_wave.parent)]
    else:
        candidates = [
            entry
            for entry in entries
            if not entry.is_dir
            and entry.canonical_path.suffix.lower() in _WAVE_SUFFIXES
        ]
    deduped = {entry.canonical_path: entry for entry in candidates}
    ordered = [deduped[path] for path in sorted(deduped, key=os.fspath)]
    if len(ordered) > max_files:
        truncation_reasons.append("max_files_per_role:wave")
        ordered = ordered[:max_files]
    return [
        {
            **_file_metadata(entry),
            "format": entry.canonical_path.suffix.lower().lstrip("."),
        }
        for entry in ordered
    ]


def _select_providers(formal_tool: str) -> tuple[FormalArtifactProvider, ...]:
    normalized = str(formal_tool).strip().lower()
    if normalized == "auto":
        return tuple(_PROVIDERS[name] for name in sorted(_PROVIDERS))
    provider = _PROVIDERS.get(normalized)
    if provider is None:
        supported = ", ".join(("auto", *sorted(_PROVIDERS)))
        raise ValueError(
            f"unsupported formal_tool '{formal_tool}'; expected one of: {supported}"
        )
    return (provider,)


def _resolve_explicit_path(
    root: Path,
    value: str | None,
    *,
    label: str,
    expect: Literal["file", "directory"],
) -> Path | None:
    if value is None:
        return None
    raw = Path(str(value)).expanduser()
    candidate = raw if raw.is_absolute() else root / raw
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"{label} does not exist: {candidate}") from exc
    except OSError as exc:
        raise ValueError(f"could not resolve {label}: {candidate}") from exc
    if not _is_within(resolved, root):
        raise ValueError(f"{label} resolves outside formal_root")
    if expect == "file" and not resolved.is_file():
        raise ValueError(f"{label} is not a regular file: {resolved}")
    if expect == "directory" and not resolved.is_dir():
        raise ValueError(f"{label} is not a directory: {resolved}")
    return resolved


def _entry_for_explicit(path: Path, relative_root: Path) -> _IndexedEntry:
    stat_result = path.stat()
    try:
        relative_parts = path.relative_to(relative_root).parts
    except ValueError:
        relative_parts = (path.name,)
    return _IndexedEntry(
        logical_path=path,
        canonical_path=path,
        relative_parts=tuple(relative_parts),
        is_dir=path.is_dir(),
        size=stat_result.st_size,
        mtime=stat_result.st_mtime,
    )


def _file_metadata(entry: _IndexedEntry) -> dict:
    mtime = datetime.fromtimestamp(entry.mtime, tz=timezone.utc)
    age_hours = max(
        0.0,
        round((datetime.now(timezone.utc) - mtime).total_seconds() / 3600.0, 1),
    )
    return {
        "path": os.fspath(entry.canonical_path),
        "size": entry.size,
        "mtime": mtime.isoformat(),
        "age_hours": age_hours,
    }


def _current_session_index(parts: tuple[str, ...]) -> int | None:
    for index, part in enumerate(parts[:-2]):
        if part.lower() != "sessionlogs":
            continue
        if parts[index + 1].lower().startswith("session_"):
            return index
    return None


def _canonical_ancestor(path: Path, levels: int) -> Path:
    ancestor = path
    for _ in range(levels):
        ancestor = ancestor.parent
    return ancestor.resolve()


def _closest_containing_project(path: Path, projects: set[Path]) -> Path | None:
    matches = [project for project in projects if _is_within(path, project)]
    if not matches:
        return None
    return max(matches, key=lambda project: len(project.parts))


def _is_proprietary_database(path: Path) -> bool:
    return path.suffix.lower() in {".apdb", ".ddk"}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _relative_display_name(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return path.name
    return path.name if len(relative.parts) == 1 else relative.as_posix()


def _append_hint(hints: list[str], hint: str) -> None:
    if len(hints) < _MAX_HINTS:
        hints.append(hint)


def _dedupe_strings(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))
