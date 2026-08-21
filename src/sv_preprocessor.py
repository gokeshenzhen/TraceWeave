"""Small, conservative SystemVerilog preprocessing for hierarchy discovery.

This is intentionally not a replacement for the Source Graph frontend.  It
implements only the preprocessor semantics needed to reconstruct the source
text seen by the module-instance scanner: command-line defines/include paths,
conditional directives, and literal (or simple macro-backed) ``include``
directives.  Simulator-recorded VCS include edges are preferred when present;
Xcelium can use the same code from its recorded command and source files even
though a normal ``elab.log`` does not report parent/child include edges.
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
from typing import Any, Callable, Mapping, Sequence

from .cancellation import check_cancelled
from .filelist_tokenizer import tokenize_filelist


_DIRECTIVE_RE = re.compile(
    r"^\s*`(?P<name>ifdef|ifndef|elsif|else|endif|define|undef|"
    r"undefineall|include)\b(?P<body>.*)$",
    re.IGNORECASE,
)
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_INCLUDE_LITERAL_RE = re.compile(r'^\s*["<]([^">]+)[">]')
_INCLUDE_MACRO_RE = re.compile(r"^\s*`([A-Za-z_][A-Za-z0-9_$]*)\b")
_MAX_FILELIST_DEPTH = 32
_MAX_FILELIST_TOKENS = 200_000
_DEFAULT_SOURCE_CACHE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class PreprocessorOptions:
    macros: tuple[tuple[str, str], ...]
    include_dirs: tuple[str, ...]
    complete: bool = True

    def macro_dict(self) -> dict[str, str]:
        return dict(self.macros)


@dataclass(frozen=True)
class PreprocessedSource:
    text: str
    root_text: str
    included_paths: tuple[str, ...]
    include_tree: dict[str, list[str]]
    ordered_includes: tuple[dict[str, str], ...]
    root_include_directives: tuple[str, ...]
    active_include_directives: tuple[str, ...]
    conditional_macros: tuple[str, ...]
    has_conditional_preprocessing: bool
    complete: bool
    issues: tuple[str, ...]


@dataclass
class _ConditionalFrame:
    parent_active: bool
    branch_taken: bool
    active: bool


@dataclass
class _ExpansionState:
    macros: dict[str, str]
    include_tree: dict[str, list[str]]
    ordered_includes: list[dict[str, str]]
    included_paths: list[str]
    active_include_directives: list[str]
    conditional_macros: set[str]
    issues: list[str]
    visited_paths: set[str]
    has_conditionals: bool = False
    has_conditional_include: bool = False

    def issue(self, code: str) -> None:
        if code not in self.issues:
            self.issues.append(code)


def _canonical(path: str | os.PathLike[str]) -> str:
    return os.path.realpath(os.fspath(path))


def _render_path(raw: str, base: str) -> str | None:
    expanded = os.path.expandvars(os.path.expanduser(raw))
    if "$" in expanded:
        return None
    path = Path(expanded)
    if not path.is_absolute():
        path = Path(base) / path
    return _canonical(path)


def _command_contexts(
    compile_result: Mapping[str, Any], source_path: str
) -> list[tuple[str, str]]:
    evidence = compile_result.get("compile_evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    source_log_index: int | None = None
    records = evidence.get("ordered_compilation_units")
    if isinstance(records, Sequence) and not isinstance(records, (str, bytes)):
        canonical_source = _canonical(source_path)
        for record in records:
            if not isinstance(record, Mapping) or not record.get("path"):
                continue
            if _canonical(str(record["path"])) != canonical_source:
                continue
            raw_index = record.get("source_log_index")
            if isinstance(raw_index, int):
                source_log_index = raw_index
            break

    phases = evidence.get("source_phases")
    if isinstance(phases, Sequence) and not isinstance(phases, (str, bytes)):
        selected: list[tuple[str, str]] = []
        for phase in phases:
            if not isinstance(phase, Mapping):
                continue
            raw_index = phase.get("source_log_index")
            if source_log_index is not None and raw_index != source_log_index:
                continue
            command = str(
                phase.get("expanded_replay_command")
                or phase.get("compile_replay_command")
                or phase.get("compile_command")
                or ""
            )
            if not command:
                continue
            base = str(
                phase.get("compile_cwd")
                or compile_result.get("compile_cwd")
                or Path(source_path).parent
            )
            selected.append((command, _canonical(base)))
        if selected:
            return selected

    command = str(
        evidence.get("expanded_replay_command")
        or compile_result.get("compile_replay_command")
        or compile_result.get("compile_command")
        or ""
    )
    base = str(compile_result.get("compile_cwd") or Path(source_path).parent)
    return [(command, _canonical(base))] if command else []


def _split_plus_defines(token: str) -> list[str]:
    values: list[str] = []
    for item in token[len("+define+") :].split("+"):
        if not item or item.lower() == "define":
            continue
        values.append(item)
    return values


def _record_macro(macros: dict[str, str], rendered: str) -> None:
    name, separator, value = rendered.partition("=")
    if _IDENTIFIER_RE.fullmatch(name):
        macros[name] = value if separator else "1"


def _record_incdir(include_dirs: list[str], raw: str, base: str) -> bool:
    path = _render_path(raw, base)
    if path is None:
        return False
    if path not in include_dirs:
        include_dirs.append(path)
    return True


def _extract_tokens_options(
    tokens: Sequence[str],
    *,
    base: str,
    command_dir: str,
    macros: dict[str, str],
    include_dirs: list[str],
    visited_filelists: set[str],
    token_budget: list[int],
    depth: int,
) -> bool:
    if depth > _MAX_FILELIST_DEPTH:
        return False
    token_budget[0] += len(tokens)
    if token_budget[0] > _MAX_FILELIST_TOKENS:
        return False

    complete = True
    index = 0
    while index < len(tokens):
        check_cancelled()
        token = str(tokens[index])
        lower = token.lower()
        if token.startswith("+define+"):
            for item in _split_plus_defines(token):
                _record_macro(macros, item)
            index += 1
            continue
        if (lower == "-define" or token == "-D") and index + 1 < len(tokens):
            _record_macro(macros, str(tokens[index + 1]))
            index += 2
            continue
        if token.startswith("-D") and len(token) > 2:
            _record_macro(macros, token[2:])
            index += 1
            continue
        if (lower == "-undefine" or token == "-U") and index + 1 < len(tokens):
            macros.pop(str(tokens[index + 1]).split("=", 1)[0], None)
            index += 2
            continue
        if token.startswith("+incdir+"):
            for raw in token[len("+incdir+") :].split("+"):
                if raw and not _record_incdir(include_dirs, raw, base):
                    complete = False
            index += 1
            continue
        if (lower == "-incdir" or token == "-I") and index + 1 < len(tokens):
            if not _record_incdir(include_dirs, str(tokens[index + 1]), base):
                complete = False
            index += 2
            continue
        if token.startswith("-I") and len(token) > 2:
            if not _record_incdir(include_dirs, token[2:], base):
                complete = False
            index += 1
            continue
        if token in {"-f", "-F"} and index + 1 < len(tokens):
            raw_filelist = str(tokens[index + 1])
            filelist_base = command_dir if token == "-f" else base
            filelist_path = _render_path(raw_filelist, filelist_base)
            if filelist_path is None or filelist_path in visited_filelists:
                complete &= filelist_path is not None
                index += 2
                continue
            visited_filelists.add(filelist_path)
            try:
                with open(filelist_path, "r", errors="replace") as stream:
                    nested = tokenize_filelist(stream.read())
            except (OSError, ValueError):
                complete = False
                index += 2
                continue
            nested_base = (
                command_dir
                if token == "-f"
                else str(Path(filelist_path).parent)
            )
            complete &= _extract_tokens_options(
                nested,
                base=nested_base,
                command_dir=command_dir,
                macros=macros,
                include_dirs=include_dirs,
                visited_filelists=visited_filelists,
                token_budget=token_budget,
                depth=depth + 1,
            )
            index += 2
            continue
        index += 1
    return complete


def _extract_options_from_contexts(
    contexts: Sequence[tuple[str, str]],
) -> PreprocessorOptions:
    macros: dict[str, str] = {}
    include_dirs: list[str] = []
    complete = True
    if not contexts:
        return PreprocessorOptions((), (), False)
    for command, base in contexts:
        try:
            tokens = shlex.split(command, comments=True, posix=True)
        except ValueError:
            complete = False
            continue
        complete &= _extract_tokens_options(
            tokens,
            base=base,
            command_dir=base,
            macros=macros,
            include_dirs=include_dirs,
            visited_filelists=set(),
            token_budget=[0],
            depth=0,
        )
    return PreprocessorOptions(
        tuple(macros.items()), tuple(include_dirs), complete
    )


def extract_preprocessor_options(
    compile_result: Mapping[str, Any], source_path: str
) -> PreprocessorOptions:
    return _extract_options_from_contexts(
        _command_contexts(compile_result, source_path)
    )


def _mask_comments(line: str, in_block_comment: bool) -> tuple[str, bool]:
    output = list(line)
    index = 0
    in_string = False
    escaped = False
    while index < len(line):
        if in_block_comment:
            end = line.find("*/", index)
            if end < 0:
                for cursor in range(index, len(output)):
                    if output[cursor] != "\n":
                        output[cursor] = " "
                return "".join(output), True
            for cursor in range(index, end + 2):
                if output[cursor] != "\n":
                    output[cursor] = " "
            in_block_comment = False
            index = end + 2
            continue
        char = line[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            index += 1
            continue
        if line.startswith("//", index):
            for cursor in range(index, len(output)):
                if output[cursor] != "\n":
                    output[cursor] = " "
            break
        if line.startswith("/*", index):
            output[index] = " "
            if index + 1 < len(output):
                output[index + 1] = " "
            in_block_comment = True
            index += 2
            continue
        index += 1
    return "".join(output), in_block_comment


def _directive_identifier(body: str) -> str | None:
    match = _IDENTIFIER_RE.search(body)
    return match.group(0) if match else None


def _macro_include_name(body: str, macros: Mapping[str, str]) -> str | None:
    literal = _INCLUDE_LITERAL_RE.match(body)
    if literal:
        return literal.group(1)
    macro = _INCLUDE_MACRO_RE.match(body)
    if not macro:
        return None
    value = str(macros.get(macro.group(1), "")).strip()
    literal = _INCLUDE_LITERAL_RE.match(value)
    if literal:
        return literal.group(1)
    if value and not any(char.isspace() for char in value):
        return value.strip('"<>') or None
    return None


def _root_include_directives(raw: str) -> tuple[str, ...]:
    result: list[str] = []
    in_block_comment = False
    for line in raw.splitlines(keepends=True):
        masked, in_block_comment = _mask_comments(line, in_block_comment)
        match = _DIRECTIVE_RE.match(masked)
        if not match or match.group("name").lower() != "include":
            continue
        literal = _INCLUDE_LITERAL_RE.match(match.group("body"))
        if literal and literal.group(1) not in result:
            result.append(literal.group(1))
    return tuple(result)


def _common_include_guard(raw: str) -> str | None:
    directives: list[tuple[str, str]] = []
    in_block_comment = False
    for line in raw.splitlines(keepends=True):
        masked, in_block_comment = _mask_comments(line, in_block_comment)
        if not masked.strip():
            continue
        match = _DIRECTIVE_RE.match(masked)
        if not match:
            return None
        name = match.group("name").lower()
        identifier = _directive_identifier(match.group("body")) or ""
        directives.append((name, identifier))
        if len(directives) == 2:
            break
    if (
        len(directives) == 2
        and directives[0][0] == "ifndef"
        and directives[1] == ("define", directives[0][1])
    ):
        return directives[0][1]
    return None


class SystemVerilogPreprocessor:
    """Expand source includes with one fresh macro state per source unit."""

    def __init__(
        self,
        compile_result: Mapping[str, Any],
        *,
        source_loader: Callable[[str], str] | None = None,
        max_include_depth: int = 64,
        source_cache_bytes: int = _DEFAULT_SOURCE_CACHE_BYTES,
    ) -> None:
        self._compile_result = compile_result
        self._source_loader = source_loader or self._read_source
        self._max_include_depth = max(0, int(max_include_depth))
        self._cache_limit = max(0, int(source_cache_bytes))
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._cache_bytes = 0
        self._options_cache: dict[
            tuple[tuple[str, str], ...], PreprocessorOptions
        ] = {}
        self._exact_includes = self._build_exact_include_map(compile_result)

    @staticmethod
    def _read_source(path: str) -> str:
        with open(path, "r", errors="replace") as stream:
            return stream.read()

    @staticmethod
    def _build_exact_include_map(
        compile_result: Mapping[str, Any]
    ) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        evidence = compile_result.get("compile_evidence")
        ordered = (
            evidence.get("ordered_includes")
            if isinstance(evidence, Mapping)
            else None
        )
        if isinstance(ordered, Sequence) and not isinstance(ordered, (str, bytes)):
            for item in ordered:
                if (
                    not isinstance(item, Mapping)
                    or not item.get("parent")
                    or not item.get("path")
                ):
                    continue
                parent = _canonical(str(item["parent"]))
                child = _canonical(str(item["path"]))
                result.setdefault(parent, []).append(child)
        tree = compile_result.get("include_tree")
        if isinstance(tree, Mapping):
            for parent, children in tree.items():
                if (
                    not parent
                    or not isinstance(children, Sequence)
                    or isinstance(children, (str, bytes))
                ):
                    continue
                destination = result.setdefault(_canonical(str(parent)), [])
                for child in children:
                    canonical = _canonical(str(child))
                    if canonical not in destination:
                        destination.append(canonical)
        return result

    def _load(self, path: str) -> str:
        canonical = _canonical(path)
        cached = self._cache.get(canonical)
        if cached is not None:
            self._cache.move_to_end(canonical)
            return cached
        check_cancelled()
        raw = self._source_loader(canonical)
        check_cancelled()
        size = len(raw)
        if self._cache_limit and size <= self._cache_limit:
            while self._cache and self._cache_bytes + size > self._cache_limit:
                _, removed = self._cache.popitem(last=False)
                self._cache_bytes -= len(removed)
            self._cache[canonical] = raw
            self._cache_bytes += size
        return raw

    def _resolve_include(
        self,
        *,
        parent: str,
        raw_name: str,
        include_dirs: Sequence[str],
    ) -> str | None:
        parent_dir = str(Path(parent).parent)
        search_candidates: list[str] = []
        if os.path.isabs(raw_name):
            search_candidates.append(_canonical(raw_name))
        else:
            for base in (parent_dir, *include_dirs):
                candidate = _canonical(Path(base) / raw_name)
                if candidate not in search_candidates:
                    search_candidates.append(candidate)

        exact = self._exact_includes.get(parent, [])
        for candidate in search_candidates:
            if candidate in exact and os.path.isfile(candidate):
                return candidate
        basename_matches = [
            candidate
            for candidate in exact
            if Path(candidate).name == Path(raw_name).name
            and os.path.isfile(candidate)
        ]
        if len(dict.fromkeys(basename_matches)) == 1:
            return basename_matches[0]
        for candidate in search_candidates:
            if os.path.isfile(candidate):
                return candidate
        return None

    def preprocess(self, source_path: str) -> PreprocessedSource:
        root = _canonical(source_path)
        contexts = tuple(_command_contexts(self._compile_result, root))
        options = self._options_cache.get(contexts)
        if options is None:
            options = _extract_options_from_contexts(contexts)
            self._options_cache[contexts] = options
        state = _ExpansionState(
            macros=options.macro_dict(),
            include_tree={},
            ordered_includes=[],
            included_paths=[],
            active_include_directives=[],
            conditional_macros=set(),
            issues=[],
            visited_paths=set(),
        )
        raw = self._load(root)
        text = self._expand_file(
            root,
            raw=raw,
            include_dirs=options.include_dirs,
            state=state,
            depth=0,
            stack=(),
        )
        if state.has_conditional_include and not options.complete:
            state.issue("compile_options_incomplete")
        for parent in state.visited_paths:
            exact_children = set(self._exact_includes.get(parent, ()))
            if not exact_children:
                continue
            resolved_children = set(state.include_tree.get(parent, ()))
            if resolved_children != exact_children:
                state.issue("include_evidence_mismatch")
        return PreprocessedSource(
            text=text,
            root_text=raw,
            included_paths=tuple(state.included_paths),
            include_tree=state.include_tree,
            ordered_includes=tuple(state.ordered_includes),
            root_include_directives=_root_include_directives(raw),
            active_include_directives=tuple(state.active_include_directives),
            conditional_macros=tuple(sorted(state.conditional_macros)),
            has_conditional_preprocessing=state.has_conditionals,
            complete=not state.issues,
            issues=tuple(state.issues),
        )

    def _expand_file(
        self,
        path: str,
        *,
        raw: str | None,
        include_dirs: Sequence[str],
        state: _ExpansionState,
        depth: int,
        stack: tuple[str, ...],
    ) -> str:
        canonical = _canonical(path)
        state.visited_paths.add(canonical)
        if depth > self._max_include_depth:
            state.issue("include_depth_exceeded")
            return ""
        if raw is None:
            try:
                raw = self._load(canonical)
            except OSError:
                state.issue("include_unreadable")
                return ""
        if canonical in stack:
            guard = _common_include_guard(raw)
            if guard and guard in state.macros:
                return ""
            state.issue("include_cycle")
            return ""

        output: list[str] = []
        frames: list[_ConditionalFrame] = []
        active = True
        in_block_comment = False
        for line in raw.splitlines(keepends=True):
            check_cancelled()
            masked, in_block_comment = _mask_comments(line, in_block_comment)
            match = _DIRECTIVE_RE.match(masked)
            if not match:
                output.append(line if active else ("\n" if line.endswith("\n") else ""))
                continue

            directive = match.group("name").lower()
            body = match.group("body")
            newline = "\n" if line.endswith("\n") else ""
            if directive in {"ifdef", "ifndef"}:
                state.has_conditionals = True
                identifier = _directive_identifier(body)
                if identifier:
                    state.conditional_macros.add(identifier)
                condition = bool(identifier and identifier in state.macros)
                if directive == "ifndef":
                    condition = not condition
                frame = _ConditionalFrame(
                    parent_active=active,
                    branch_taken=condition,
                    active=active and condition,
                )
                frames.append(frame)
                active = frame.active
                output.append(newline)
                continue
            if directive == "elsif":
                state.has_conditionals = True
                identifier = _directive_identifier(body)
                if identifier:
                    state.conditional_macros.add(identifier)
                if not frames:
                    state.issue("conditional_unbalanced")
                    output.append(newline)
                    continue
                frame = frames[-1]
                condition = bool(identifier and identifier in state.macros)
                take = not frame.branch_taken and condition
                frame.active = frame.parent_active and take
                frame.branch_taken = frame.branch_taken or condition
                active = frame.active
                output.append(newline)
                continue
            if directive == "else":
                state.has_conditionals = True
                if not frames:
                    state.issue("conditional_unbalanced")
                    output.append(newline)
                    continue
                frame = frames[-1]
                take = not frame.branch_taken
                frame.active = frame.parent_active and take
                frame.branch_taken = True
                active = frame.active
                output.append(newline)
                continue
            if directive == "endif":
                state.has_conditionals = True
                if not frames:
                    state.issue("conditional_unbalanced")
                else:
                    frame = frames.pop()
                    active = frame.parent_active
                output.append(newline)
                continue
            if directive == "include" and frames:
                state.has_conditional_include = True
            if not active:
                output.append(newline)
                continue
            if directive == "define":
                identifier = _directive_identifier(body)
                if identifier:
                    tail = body[body.find(identifier) + len(identifier) :].strip()
                    state.macros[identifier] = tail or "1"
                output.append(line)
                continue
            if directive == "undef":
                identifier = _directive_identifier(body)
                if identifier:
                    state.macros.pop(identifier, None)
                output.append(newline)
                continue
            if directive == "undefineall":
                state.macros.clear()
                output.append(newline)
                continue
            if directive == "include":
                raw_name = _macro_include_name(body, state.macros)
                if raw_name is None:
                    state.issue("include_expression_unresolved")
                    output.append(newline)
                    continue
                if raw_name not in state.active_include_directives:
                    state.active_include_directives.append(raw_name)
                child = self._resolve_include(
                    parent=canonical,
                    raw_name=raw_name,
                    include_dirs=include_dirs,
                )
                if child is None:
                    state.issue("include_path_unresolved")
                    output.append(newline)
                    continue
                children = state.include_tree.setdefault(canonical, [])
                if child not in children:
                    children.append(child)
                state.ordered_includes.append({"parent": canonical, "path": child})
                if child not in state.included_paths:
                    state.included_paths.append(child)
                child_text = self._expand_file(
                    child,
                    raw=None,
                    include_dirs=include_dirs,
                    state=state,
                    depth=depth + 1,
                    stack=(*stack, canonical),
                )
                output.append(child_text)
                if child_text and not child_text.endswith("\n") and newline:
                    output.append("\n")
                continue
            output.append(line)

        if frames:
            state.issue("conditional_unbalanced")
        return "".join(output)


def merge_resolved_include_evidence(
    compile_result: Mapping[str, Any],
    *,
    include_tree: Mapping[str, Sequence[str]],
    ordered_includes: Sequence[Mapping[str, str]],
) -> dict:
    """Return a copy enriched with active include edges proved from source."""

    result = deepcopy(dict(compile_result))
    merged_tree: dict[str, list[str]] = {}
    existing_tree = result.get("include_tree")
    if isinstance(existing_tree, Mapping):
        for parent, children in existing_tree.items():
            if not isinstance(children, Sequence) or isinstance(
                children, (str, bytes)
            ):
                continue
            merged_tree[_canonical(str(parent))] = list(
                dict.fromkeys(_canonical(str(child)) for child in children)
            )
    for parent, children in include_tree.items():
        destination = merged_tree.setdefault(_canonical(str(parent)), [])
        for child in children:
            canonical = _canonical(str(child))
            if canonical not in destination:
                destination.append(canonical)
    result["include_tree"] = merged_tree

    evidence = result.get("compile_evidence")
    evidence = deepcopy(evidence) if isinstance(evidence, Mapping) else {}
    existing_ordered = evidence.get("ordered_includes")
    merged_ordered: list[dict[str, Any]] = []
    if isinstance(existing_ordered, Sequence) and not isinstance(
        existing_ordered, (str, bytes)
    ):
        merged_ordered = [
            deepcopy(dict(item))
            for item in existing_ordered
            if isinstance(item, Mapping)
            and item.get("parent")
            and item.get("path")
        ]
    seen = {
        (_canonical(str(item["parent"])), _canonical(str(item["path"])))
        for item in merged_ordered
    }
    for item in ordered_includes:
        if not item.get("parent") or not item.get("path"):
            continue
        parent = _canonical(str(item["parent"]))
        child = _canonical(str(item["path"]))
        if (parent, child) in seen:
            continue
        merged_ordered.append({"parent": parent, "path": child})
        seen.add((parent, child))
    evidence["ordered_includes"] = merged_ordered
    result["compile_evidence"] = evidence
    return result
