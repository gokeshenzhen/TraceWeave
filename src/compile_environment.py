"""Bounded, log-anchored compile path replay shared by hierarchy and backends.

Only uniquely constrained local path variables are inferred. No shell, process
environment mutation or design/frontend execution is involved.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import os
import re
import shlex

from .cancellation import check_cancelled
from .filelist_tokenizer import tokenize_filelist

_ENV_REF_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[^}]+\})")
_MAX_ENV_INFERENCE_FILELISTS = 256
_MAX_ENV_INFERENCE_ROUNDS = 64
_MAX_FILELIST_TOKENS = 1_000_000


@dataclass(frozen=True)
class CompileEnvironment:
    bindings: dict[str, str]
    complete: bool
    filelists: tuple[str, ...] = ()


def resolve_compile_environment(compile_result: Mapping[str, Any]) -> CompileEnvironment:
    """Validate inferred paths against the simulator's ordered project units.

    Phase-local commands keep the existing conservative path; bindings from
    another phase must never alter a source's defines or include search order.
    Legacy caller-built contexts without unit evidence retain their behavior.
    """
    evidence = _compile_evidence(compile_result) or {}
    if (
        compile_result.get("simulator") != "vcs"
        or evidence.get("unit_order_source") != "simulator_log"
        or evidence.get("source_phases")
    ):
        return CompileEnvironment({}, True)
    records = _compilation_unit_records(compile_result)
    project = [
        str(Path(str(r["path"])).resolve()) for r in records
        if r.get("role", "project") == "project"
    ]
    if not project:
        return CompileEnvironment({}, True)
    command = str(
        compile_result.get("compile_replay_command")
        or compile_result.get("compile_command") or ""
    )
    base = Path(str(compile_result.get("compile_cwd") or Path(project[0]).parent)).resolve()
    bindings, _ = _infer_compile_environment(
        command, command_dir=base, compile_result=compile_result,
    )
    try:
        tokens = shlex.split(command, comments=True, posix=True)
    except ValueError:
        return CompileEnvironment({}, False)
    # Lazy import: the parser also uses local expansion during this replay.
    from .compile_log_parser import _is_eda_lib, _recover_vcs_command_files

    warnings: list[str] = []
    files, _, _, filelists = _recover_vcs_command_files(
        tokens, str(base), warnings, environment=bindings,
    )
    nonproject = {
        str(Path(str(r["path"])).resolve()) for r in records
        if r.get("role", "project") != "project"
    }
    project_set = set(project)
    replay = [
        p for p in files
        if p not in nonproject and (p in project_set or not _is_eda_lib(p))
    ]
    complete = bool(command) and not warnings and replay == project
    return CompileEnvironment(
        bindings if complete else {}, complete,
        tuple(dict.fromkeys(str(f["path"]) for f in filelists)),
    )


def _env_ref_name(match: re.Match[str]) -> str:
    rendered = match.group(0)
    return rendered[2:-1] if rendered.startswith("${") else rendered[1:]


def _expand_with_environment(value: str, overrides: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = _env_ref_name(match)
        if name in overrides:
            return overrides[name]
        return os.environ.get(name, match.group(0))

    return _ENV_REF_RE.sub(replace, os.path.expanduser(value))


def _expand_with_overrides_only(value: str, overrides: Mapping[str, str]) -> str:
    return _ENV_REF_RE.sub(
        lambda match: overrides.get(_env_ref_name(match), match.group(0)),
        os.path.expanduser(value),
    )


def _compile_evidence(
    compile_result: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    evidence = compile_result.get("compile_evidence")
    if not isinstance(evidence, Mapping):
        return None
    if evidence.get("schema_version") != 1:
        return None
    return evidence


def _compilation_unit_records(
    compile_result: Mapping[str, Any],
    *,
    require_simulator_log: bool = False,
) -> list[Mapping[str, Any]]:
    evidence = _compile_evidence(compile_result)
    if evidence is None:
        return []
    if require_simulator_log:
        order_source = evidence.get("unit_order_source")
        bootstrap = compile_result.get("bootstrap_context")
        bounded_subset = (
            order_source == "bootstrap_subset"
            and isinstance(bootstrap, Mapping)
            and bootstrap.get("used") is True
        )
        if order_source != "simulator_log" and not bounded_subset:
            return []
    raw = evidence.get("ordered_compilation_units")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [item for item in raw if isinstance(item, Mapping) and item.get("path")]


def _evidence_anchor_paths(compile_result: Mapping[str, Any]) -> tuple[Path, ...]:
    anchors: list[Path] = []
    for item in _compilation_unit_records(compile_result):
        anchors.append(Path(str(item["path"])).resolve(strict=False))
        if item.get("reported_path"):
            anchors.append(Path(str(item["reported_path"])).absolute())

    evidence = _compile_evidence(compile_result)
    if evidence is not None:
        includes = evidence.get("ordered_includes")
        if isinstance(includes, Sequence) and not isinstance(includes, (str, bytes)):
            for item in includes:
                if not isinstance(item, Mapping):
                    continue
                for key in ("parent", "path"):
                    if item.get(key):
                        path = Path(str(item[key])).resolve(strict=False)
                        anchors.extend((path, path.parent))
        filelists = evidence.get("filelists")
        if isinstance(filelists, Sequence) and not isinstance(filelists, (str, bytes)):
            for item in filelists:
                if not isinstance(item, Mapping) or not item.get("path"):
                    continue
                rendered = str(item["path"])
                raw_path = str(item.get("raw_path") or "")
                if not _ENV_REF_RE.search(rendered) and not _ENV_REF_RE.search(
                    raw_path
                ):
                    anchors.append(Path(rendered).resolve(strict=False))
    return tuple(dict.fromkeys(anchors))


def _iter_path_expressions(tokens: Sequence[str]):
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"-f", "-F"}:
            index += 2
            continue
        if token in {"-incdir", "-I", "-v", "-y"}:
            if index + 1 < len(tokens):
                yield tokens[index + 1]
            index += 2
            continue
        if token.startswith("+incdir+"):
            yield from (item for item in token[len("+incdir+") :].split("+") if item)
            index += 1
            continue
        if token.startswith(("-I", "-v", "-y")) and len(token) > 2:
            yield token[2:]
            index += 1
            continue
        if not token.startswith(("+", "-")) and _ENV_REF_RE.search(token):
            yield token
        index += 1


def _iter_filelist_references(tokens: Sequence[str]):
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"-f", "-F"} and index + 1 < len(tokens):
            yield token, tokens[index + 1]
            index += 2
            continue
        index += 1


def _candidate_environment_values(
    expression: str,
    *,
    base: Path,
    anchors: Sequence[Path],
) -> tuple[str, set[str]] | None:
    matches = list(_ENV_REF_RE.finditer(expression))
    if len(matches) != 1:
        return None
    match = matches[0]
    name = _env_ref_name(match)
    prefix = expression[: match.start()]
    suffix = expression[match.end() :]
    if _ENV_REF_RE.search(prefix) or _ENV_REF_RE.search(suffix):
        return None
    if not prefix and not suffix:
        return None

    candidates: set[str] = set()
    for anchor in anchors:
        rendered = str(anchor)
        if prefix and not rendered.startswith(prefix):
            continue
        if suffix and not rendered.endswith(suffix):
            continue
        end = len(rendered) - len(suffix) if suffix else len(rendered)
        candidate = rendered[len(prefix) : end]
        if not candidate or _ENV_REF_RE.search(candidate):
            continue
        expanded = prefix + candidate + suffix
        path = Path(expanded)
        if not path.is_absolute():
            path = base / path
        if path.absolute() == anchor.absolute() or path.resolve(
            strict=False
        ) == anchor.resolve(strict=False):
            candidates.add(candidate)
    return (name, candidates) if candidates else None


def _candidate_filelist_root_values(
    expression: str,
    *,
    base: Path,
    anchors: Sequence[Path],
) -> tuple[str, set[str]] | None:
    """Find a unique root candidate for ``$ROOT/path/to/file.f``.

    A VCS log does not normally print the expanded ``-f`` path.  It does print
    the files compiled from that command file, so their bounded ancestor set is
    the only local search space needed for the common project-root convention.
    Candidates are accepted only when the exact substituted command file
    exists; the later compilation-unit order check still validates its
    contents before the binding can make a manifest reusable.
    """

    matches = list(_ENV_REF_RE.finditer(expression))
    if len(matches) != 1 or matches[0].start() != 0:
        return None
    match = matches[0]
    suffix = expression[match.end() :]
    if not suffix or _ENV_REF_RE.search(suffix):
        return None

    roots: set[Path] = set()
    for index, anchor in enumerate(anchors):
        if index % 256 == 0:
            check_cancelled()
        absolute = anchor.absolute()
        start = absolute if absolute.is_dir() else absolute.parent
        roots.add(start)
        roots.update(start.parents)

    candidates: set[str] = set()
    for index, root in enumerate(sorted(roots, key=lambda item: str(item))):
        if index % 256 == 0:
            check_cancelled()
        value = str(root)
        rendered = value + suffix
        path = Path(rendered)
        if not path.is_absolute():
            path = base / path
        if path.is_file():
            candidates.add(value)
    return (_env_ref_name(match), candidates) if candidates else None


def _infer_compile_environment(
    command: str,
    *,
    command_dir: Path,
    compile_result: Mapping[str, Any],
) -> tuple[dict[str, str], set[str]]:
    """Infer only uniquely constrained path variables from local log facts.

    No filesystem search, glob, subprocess, or environment mutation is used.
    Starting from the recorded command, newly resolved command files are read
    iteratively; every accepted binding is the sole value consistent with all
    path expressions that matched simulator-reported absolute paths.
    """

    anchors = _evidence_anchor_paths(compile_result)
    if not anchors:
        return {}, set()
    try:
        command_tokens = shlex.split(command, comments=True, posix=True)
    except ValueError:
        return {}, set()

    documents: list[tuple[list[str], Path]] = [(command_tokens, command_dir)]
    visited_filelists: set[Path] = set()
    bindings: dict[str, str] = {}
    inferred_names: set[str] = set()
    token_count = len(command_tokens)

    for _round in range(_MAX_ENV_INFERENCE_ROUNDS):
        constraints: dict[str, list[set[str]]] = {}
        pending_filelists: list[tuple[str, str, Path]] = []
        for tokens, base in documents:
            check_cancelled()
            for mode, raw_path in _iter_filelist_references(tokens):
                rendered = _expand_with_overrides_only(raw_path, bindings)
                candidate = _candidate_environment_values(
                    rendered,
                    base=base,
                    anchors=anchors,
                ) or _candidate_filelist_root_values(
                    rendered,
                    base=base,
                    anchors=anchors,
                )
                if candidate is not None:
                    name, values = candidate
                    if name not in bindings:
                        constraints.setdefault(name, []).append(values)
                pending_filelists.append((mode, raw_path, base))
            for expression in _iter_path_expressions(tokens):
                # Keep a process-environment value visible as a constraint
                # until the log proves it. An MCP server can inherit a stale
                # variable from another project; a unique simulator-recorded
                # binding must override that value locally rather than trust
                # ambient state by accident.
                rendered = _expand_with_overrides_only(expression, bindings)
                candidate = _candidate_environment_values(
                    rendered,
                    base=base,
                    anchors=anchors,
                )
                if candidate is None:
                    continue
                name, values = candidate
                if name not in bindings:
                    constraints.setdefault(name, []).append(values)

        changed = False
        for name, candidate_sets in constraints.items():
            common = set.intersection(*candidate_sets)
            if len(common) != 1:
                continue
            value = next(iter(common))
            if bindings.get(name) == value:
                continue
            bindings[name] = value
            inferred_names.add(name)
            changed = True

        for mode, raw_path, base in pending_filelists:
            rendered = _expand_with_environment(raw_path, bindings)
            if _ENV_REF_RE.search(rendered):
                continue
            path = Path(rendered)
            if not path.is_absolute():
                path = base / path
            path = path.resolve(strict=False)
            if path in visited_filelists or not path.is_file():
                continue
            if len(visited_filelists) >= _MAX_ENV_INFERENCE_FILELISTS:
                break
            try:
                tokens = tokenize_filelist(
                    path.read_text(encoding="utf-8", errors="replace")
                )
            except (OSError, ValueError):
                continue
            if token_count + len(tokens) > _MAX_FILELIST_TOKENS:
                break
            token_count += len(tokens)
            visited_filelists.add(path)
            token_base = command_dir if mode == "-f" else path.parent
            documents.append((tokens, token_base))
            changed = True

        if not changed:
            break
    return bindings, inferred_names
