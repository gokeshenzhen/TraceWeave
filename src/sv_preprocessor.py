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
_MACRO_INVOCATION_RE = re.compile(
    r"^\s*`(?P<name>[A-Za-z_][A-Za-z0-9_$]*)\b"
)
_HIERARCHY_MACRO_INSTANCE_RE = re.compile(
    r"^\s*[A-Za-z_][A-Za-z0-9_$]*\s*"
    r"(?:#\s*\([^;]*\)\s*)?"
    r"[A-Za-z_][A-Za-z0-9_$]*\s*"
    r"(?:\[[^\]]+\]\s*)?\(",
    re.DOTALL,
)
_MAX_FILELIST_DEPTH = 32
_MAX_FILELIST_TOKENS = 200_000
_DEFAULT_SOURCE_CACHE_BYTES = 16 * 1024 * 1024
_DEFAULT_INCLUDE_RESOLUTION_CACHE_ENTRIES = 4096
_MAX_HIERARCHY_MACRO_EXPANSIONS = 4096
_MAX_HIERARCHY_MACRO_BODY_BYTES = 16 * 1024


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
    trusted_hierarchy_text: str = ""
    hierarchy_evidence_status: str = "complete"


@dataclass
class _ConditionalFrame:
    parent_active: bool
    branch_taken: bool
    active: bool


@dataclass(frozen=True)
class _TextMacro:
    parameters: tuple[str, ...] | None
    body: str


@dataclass(frozen=True)
class _SourceCacheEntry:
    raw: str
    masked: str | None = None

    @property
    def size(self) -> int:
        masked_size = len(self.masked) if self.masked is not None else 0
        return len(self.raw) + masked_size


@dataclass(frozen=True)
class _CommandContextTemplate:
    command: str
    base: str | None


class _CommandContextIndex:
    """Resolve per-source compile contexts without rescanning every unit.

    ``ordered_compilation_units`` can contain the same canonical source more
    than once.  Preserve the historical first-match rule exactly: the first
    record decides ``source_log_index``.  A missing/non-integer index retains
    the old conservative behaviour of replaying every recorded source phase.
    """

    def __init__(
        self,
        compile_result: Mapping[str, Any],
        *,
        canonicalize: Callable[[str | os.PathLike[str]], str] | None = None,
    ) -> None:
        if canonicalize is None:
            canonicalize = _canonical
        self._canonicalize = canonicalize
        self._source_log_indexes: dict[str, int | None] = {}
        self._contexts_by_source: dict[
            str, tuple[tuple[str, str], ...]
        ] = {}

        evidence = compile_result.get("compile_evidence")
        evidence = evidence if isinstance(evidence, Mapping) else {}
        records = evidence.get("ordered_compilation_units")
        if isinstance(records, Sequence) and not isinstance(
            records, (str, bytes)
        ):
            for record in records:
                if not isinstance(record, Mapping) or not record.get("path"):
                    continue
                canonical = canonicalize(str(record["path"]))
                if canonical in self._source_log_indexes:
                    continue
                raw_index = record.get("source_log_index")
                self._source_log_indexes[canonical] = (
                    raw_index if isinstance(raw_index, int) else None
                )

        default_cwd = compile_result.get("compile_cwd")
        default_base = (
            canonicalize(str(default_cwd)) if default_cwd else None
        )
        all_templates: list[_CommandContextTemplate] = []
        templates_by_index: dict[Any, list[_CommandContextTemplate]] = {}
        phases = evidence.get("source_phases")
        if isinstance(phases, Sequence) and not isinstance(phases, (str, bytes)):
            for phase in phases:
                if not isinstance(phase, Mapping):
                    continue
                command = str(
                    phase.get("expanded_replay_command")
                    or phase.get("compile_replay_command")
                    or phase.get("compile_command")
                    or ""
                )
                if not command:
                    continue
                phase_cwd = phase.get("compile_cwd")
                base = (
                    canonicalize(str(phase_cwd))
                    if phase_cwd
                    else default_base
                )
                template = _CommandContextTemplate(command=command, base=base)
                all_templates.append(template)
                raw_index = phase.get("source_log_index")
                try:
                    templates_by_index.setdefault(raw_index, []).append(template)
                except TypeError:
                    # A malformed, unhashable phase index cannot equal the
                    # integer source indices accepted above.
                    continue

        self._all_templates = tuple(all_templates)
        self._templates_by_index = {
            key: tuple(value) for key, value in templates_by_index.items()
        }
        self._fallback_command = str(
            evidence.get("expanded_replay_command")
            or compile_result.get("compile_replay_command")
            or compile_result.get("compile_command")
            or ""
        )
        self._fallback_base = default_base

    def contexts_for(self, source_path: str) -> tuple[tuple[str, str], ...]:
        canonical_source = self._canonicalize(source_path)
        cached = self._contexts_by_source.get(canonical_source)
        if cached is not None:
            return cached

        source_log_index = self._source_log_indexes.get(canonical_source)
        templates = (
            self._all_templates
            if source_log_index is None
            else self._templates_by_index.get(source_log_index, ())
        )
        source_base: str | None = None

        def render_base(template_base: str | None) -> str:
            nonlocal source_base
            if template_base is not None:
                return template_base
            if source_base is None:
                source_base = self._canonicalize(Path(canonical_source).parent)
            return source_base

        contexts = tuple(
            (template.command, render_base(template.base))
            for template in templates
        )
        if not contexts and self._fallback_command:
            contexts = ((
                self._fallback_command,
                render_base(self._fallback_base),
            ),)
        self._contexts_by_source[canonical_source] = contexts
        return contexts


@dataclass
class _ExpansionState:
    macros: dict[str, str]
    text_macros: dict[str, _TextMacro]
    include_tree: dict[str, list[str]]
    ordered_includes: list[dict[str, str]]
    included_paths: list[str]
    root_include_directives: list[str]
    active_include_directives: list[str]
    conditional_macros: set[str]
    issues: list[str]
    visited_paths: set[str]
    trusted_hierarchy_parts: list[str]
    has_conditionals: bool = False
    hierarchy_macro_expansions: int = 0
    hierarchy_tainted: bool = False

    def issue(
        self,
        code: str,
        *,
        taints_hierarchy: bool = True,
        invalidates_all_hierarchy: bool = False,
    ) -> None:
        if code not in self.issues:
            self.issues.append(code)
        if invalidates_all_hierarchy:
            self.trusted_hierarchy_parts.clear()
        if taints_hierarchy:
            self.hierarchy_tainted = True


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
    return list(_CommandContextIndex(compile_result).contexts_for(source_path))


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
    # A comment can only begin with '/'. Most directive-bearing source lines
    # contain no slash at all, so avoid allocating and walking a character
    # list when the current line cannot change block-comment state.
    if not in_block_comment and "/" not in line:
        return line, False
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


def _parse_text_macro(body: str) -> tuple[str, _TextMacro] | None:
    """Parse one single-line object-like or function-like macro definition.

    General SystemVerilog macro expansion is deliberately outside this small
    preprocessor.  The retained definition is used only when a standalone
    invocation expands to one syntactically obvious module/interface instance.
    """

    match = _IDENTIFIER_RE.match(body.lstrip())
    if match is None:
        return None
    rendered = body.lstrip()
    name = match.group(0)
    remainder = rendered[match.end() :].rstrip("\r\n")
    parameters: tuple[str, ...] | None = None
    if remainder.startswith("("):
        close = remainder.find(")")
        if close < 0:
            return None
        raw_parameters = remainder[1:close].strip()
        parsed = tuple(
            item.strip() for item in raw_parameters.split(",") if item.strip()
        )
        if any(_IDENTIFIER_RE.fullmatch(item) is None for item in parsed):
            return None
        parameters = parsed
        remainder = remainder[close + 1 :]
    macro_body = remainder.strip()
    if len(macro_body.encode("utf-8", errors="replace")) > (
        _MAX_HIERARCHY_MACRO_BODY_BYTES
    ):
        return None
    return name, _TextMacro(parameters=parameters, body=macro_body)


def _record_source_macro(state: _ExpansionState, body: str) -> None:
    identifier = _directive_identifier(body)
    if not identifier:
        return
    tail = body[body.find(identifier) + len(identifier) :].strip()
    state.macros[identifier] = tail or "1"
    parsed = _parse_text_macro(body)
    if parsed is not None:
        macro_name, definition = parsed
        state.text_macros[macro_name] = definition


def _strip_line_continuation(value: str) -> tuple[str, bool]:
    rendered = value.rstrip("\r\n")
    trimmed = rendered.rstrip()
    if trimmed.endswith("\\"):
        return trimmed[:-1], True
    return rendered, False


def _split_macro_arguments(value: str) -> tuple[tuple[str, ...], int] | None:
    """Split a parenthesized macro argument list and return its end offset."""

    if not value.startswith("("):
        return None
    depth = 0
    in_string = False
    escaped = False
    argument_start = 1
    arguments: list[str] = []
    for index, char in enumerate(value):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "(":
            depth += 1
            continue
        if char == ")":
            depth -= 1
            if depth == 0:
                tail = value[argument_start:index].strip()
                if tail or arguments:
                    arguments.append(tail)
                return tuple(arguments), index + 1
            continue
        if char == "," and depth == 1:
            arguments.append(value[argument_start:index].strip())
            argument_start = index + 1
    return None


def _substitute_macro_parameters(
    body: str,
    parameters: Sequence[str],
    arguments: Sequence[str],
) -> str:
    rendered = body
    for parameter, argument in zip(parameters, arguments):
        rendered = re.sub(
            rf"(?<![A-Za-z0-9_$]){re.escape(parameter)}(?![A-Za-z0-9_$])",
            argument,
            rendered,
        )
    return rendered


def _expand_hierarchy_macro_line(
    line: str,
    masked: str,
    state: _ExpansionState,
) -> str:
    """Expand a standalone macro only when it renders one HDL instance.

    This avoids pulling UVM report/factory macro bodies into the hierarchy
    scanner while recovering the common RTL pattern
    `` `MAKE_INSTANCE(u_leaf) ``. Unsupported or compound macros remain in the
    text and therefore cannot fabricate hierarchy.
    """

    match = _MACRO_INVOCATION_RE.match(masked)
    if match is None:
        return line
    macro_name = match.group("name")
    if macro_name.lower().startswith(("uvm_", "m_uvm_")):
        return line
    definition = state.text_macros.get(macro_name)
    if definition is None or not definition.body:
        return line

    remainder = masked[match.end() :]
    arguments: tuple[str, ...] = ()
    consumed = 0
    if definition.parameters is not None:
        stripped = remainder.lstrip()
        leading = len(remainder) - len(stripped)
        parsed = _split_macro_arguments(stripped)
        if parsed is None:
            return line
        arguments, consumed = parsed
        consumed += leading
        if len(arguments) != len(definition.parameters):
            return line
    trailing = remainder[consumed:].strip()
    invocation_semicolon = trailing == ";"
    if trailing not in {"", ";"}:
        return line

    rendered = _substitute_macro_parameters(
        definition.body,
        definition.parameters or (),
        arguments,
    ).strip()
    prefix = _HIERARCHY_MACRO_INSTANCE_RE.match(rendered)
    if prefix is None:
        return line
    if not _single_instance_tail(rendered, prefix.end() - 1):
        state.issue(
            "hierarchy_macro_compound_unsupported",
            taints_hierarchy=False,
        )
        return line
    if state.hierarchy_macro_expansions >= _MAX_HIERARCHY_MACRO_EXPANSIONS:
        state.issue(
            "hierarchy_macro_expansion_limit_exceeded",
            taints_hierarchy=False,
        )
        return line
    state.hierarchy_macro_expansions += 1
    if invocation_semicolon and not rendered.endswith(";"):
        rendered += ";"
    indent = line[: len(line) - len(line.lstrip())]
    newline = "\n" if line.endswith("\n") else ""
    return f"{indent}{rendered}{newline}"


def _single_instance_tail(rendered: str, open_paren: int) -> bool:
    """Return whether ``open_paren`` closes the only instance statement.

    The prefix recognizer deliberately accepts arbitrary parameter and port
    expressions. This balanced suffix check prevents a macro containing an
    instance plus a second statement from leaking compound macro content into
    the hierarchy scanner.
    """

    depth = 0
    in_string = False
    escaped = False
    for index in range(open_paren, len(rendered)):
        char = rendered[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "(":
            depth += 1
            continue
        if char != ")":
            continue
        depth -= 1
        if depth < 0:
            return False
        if depth == 0:
            return rendered[index + 1 :].strip() in {"", ";"}
    return False


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
        include_resolution_cache_entries: int = (
            _DEFAULT_INCLUDE_RESOLUTION_CACHE_ENTRIES
        ),
    ) -> None:
        self._compile_result = compile_result
        self._source_loader = source_loader or self._read_source
        self._max_include_depth = max(0, int(max_include_depth))
        # Raw and derived masked text share one hard LRU budget.
        self._cache_limit = max(0, int(source_cache_bytes))
        self._cache: OrderedDict[str, _SourceCacheEntry] = OrderedDict()
        self._cache_bytes = 0
        self._cache_peak_bytes = 0
        self._source_cache_hit_count = 0
        self._source_load_count = 0
        self._canonical_cache: dict[str, str] = {}
        self._context_index = _CommandContextIndex(
            compile_result,
            canonicalize=self._canonical_path,
        )
        self._options_cache: dict[
            tuple[tuple[str, str], ...], PreprocessorOptions
        ] = {}
        self._exact_includes = self._build_exact_include_map(compile_result)
        self._exact_include_by_basename = self._build_exact_basename_index(
            self._exact_includes
        )
        self._include_resolution_cache_limit = max(
            0, int(include_resolution_cache_entries)
        )
        self._include_resolution_cache: OrderedDict[
            tuple[str, str, tuple[str, ...]], str
        ] = OrderedDict()
        self._include_resolution_cache_hit_count = 0
        self._include_resolution_cache_miss_count = 0
        self._include_resolution_cache_eviction_count = 0
        self._exact_include_resolution_count = 0
        self._logical_file_expansion_count = 0
        self._comment_mask_line_count = 0
        self._comment_mask_fast_path_count = 0
        self._masked_text_cache_hit_count = 0

    def _canonical_path(self, path: str | os.PathLike[str]) -> str:
        raw = os.fspath(path)
        cached = self._canonical_cache.get(raw)
        if cached is not None:
            return cached
        canonical = _canonical(raw)
        self._canonical_cache[raw] = canonical
        self._canonical_cache.setdefault(canonical, canonical)
        return canonical

    @staticmethod
    def _read_source(path: str) -> str:
        with open(path, "r", errors="replace") as stream:
            return stream.read()

    def _build_exact_include_map(
        self,
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
                parent = self._canonical_path(str(item["parent"]))
                child = self._canonical_path(str(item["path"]))
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
                destination = result.setdefault(
                    self._canonical_path(str(parent)), []
                )
                for child in children:
                    canonical = self._canonical_path(str(child))
                    if canonical not in destination:
                        destination.append(canonical)
        return result

    @staticmethod
    def _build_exact_basename_index(
        exact_includes: Mapping[str, Sequence[str]],
    ) -> dict[str, dict[str, str]]:
        """Index only unambiguous simulator-recorded include basenames."""

        result: dict[str, dict[str, str]] = {}
        for parent, children in exact_includes.items():
            by_name: dict[str, list[str]] = {}
            for child in children:
                paths = by_name.setdefault(os.path.basename(child), [])
                if child not in paths:
                    paths.append(child)
            unique = {
                name: paths[0] for name, paths in by_name.items() if len(paths) == 1
            }
            if unique:
                result[parent] = unique
        return result

    def _load(self, path: str) -> str:
        canonical = self._canonical_path(path)
        cached = self._cache.get(canonical)
        if cached is not None:
            self._source_cache_hit_count += 1
            self._cache.move_to_end(canonical)
            return cached.raw
        check_cancelled()
        self._source_load_count += 1
        raw = self._source_loader(canonical)
        check_cancelled()
        self._store_cache_entry(canonical, _SourceCacheEntry(raw=raw))
        return raw

    def _store_cache_entry(
        self,
        canonical: str,
        entry: _SourceCacheEntry,
    ) -> bool:
        if not self._cache_limit or entry.size > self._cache_limit:
            return False
        previous = self._cache.pop(canonical, None)
        if previous is not None:
            self._cache_bytes -= previous.size
        while self._cache and self._cache_bytes + entry.size > self._cache_limit:
            _, removed = self._cache.popitem(last=False)
            self._cache_bytes -= removed.size
        self._cache[canonical] = entry
        self._cache_bytes += entry.size
        self._cache_peak_bytes = max(self._cache_peak_bytes, self._cache_bytes)
        return True

    def metrics_snapshot(self) -> dict[str, int]:
        """Return privacy-safe aggregate preprocessing counters."""

        return {
            "preprocessor_logical_file_expansion_count": (
                self._logical_file_expansion_count
            ),
            "preprocessor_comment_mask_line_count": self._comment_mask_line_count,
            "preprocessor_comment_mask_fast_path_count": (
                self._comment_mask_fast_path_count
            ),
            "preprocessor_masked_text_cache_hit_count": (
                self._masked_text_cache_hit_count
            ),
            "preprocessor_source_load_count": self._source_load_count,
            "preprocessor_source_cache_hit_count": self._source_cache_hit_count,
            "preprocessor_source_cache_entry_count": len(self._cache),
            "preprocessor_source_cache_bytes": self._cache_bytes,
            "preprocessor_source_cache_peak_bytes": self._cache_peak_bytes,
            "preprocessor_source_cache_limit_bytes": self._cache_limit,
            "preprocessor_include_resolution_cache_hit_count": (
                self._include_resolution_cache_hit_count
            ),
            "preprocessor_include_resolution_cache_miss_count": (
                self._include_resolution_cache_miss_count
            ),
            "preprocessor_include_resolution_cache_eviction_count": (
                self._include_resolution_cache_eviction_count
            ),
            "preprocessor_exact_include_resolution_count": (
                self._exact_include_resolution_count
            ),
            "preprocessor_include_resolution_cache_entry_count": len(
                self._include_resolution_cache
            ),
            "preprocessor_include_resolution_cache_limit_entries": (
                self._include_resolution_cache_limit
            ),
        }

    def _cached_masked_text(self, canonical: str, raw: str) -> str | None:
        cached = self._cache.get(canonical)
        if (
            cached is None
            or cached.masked is None
            or cached.raw != raw
            or len(cached.masked) != len(raw)
        ):
            return None
        self._cache.move_to_end(canonical)
        return cached.masked

    def _store_masked_text(
        self,
        canonical: str,
        raw: str,
        masked: str,
    ) -> None:
        if len(masked) != len(raw):
            return
        self._store_cache_entry(
            canonical,
            _SourceCacheEntry(raw=raw, masked=masked),
        )

    def _resolve_include(
        self,
        *,
        parent: str,
        raw_name: str,
        include_dirs: Sequence[str],
    ) -> str | None:
        cache_key = (parent, raw_name, tuple(include_dirs))
        cached = self._include_resolution_cache.get(cache_key)
        if cached is not None:
            self._include_resolution_cache_hit_count += 1
            self._include_resolution_cache.move_to_end(cache_key)
            return cached
        self._include_resolution_cache_miss_count += 1
        exact = self._exact_includes.get(parent, [])
        exact_candidate = self._exact_include_by_basename.get(parent, {}).get(
            os.path.basename(raw_name)
        )
        if exact_candidate is not None and os.path.isfile(exact_candidate):
            self._exact_include_resolution_count += 1
            return self._remember_include_resolution(cache_key, exact_candidate)

        parent_dir = os.path.dirname(parent)
        search_candidates: list[str] = []
        seen_candidates: set[str] = set()
        if os.path.isabs(raw_name):
            candidate = self._canonical_path(raw_name)
            search_candidates.append(candidate)
            seen_candidates.add(candidate)
        else:
            for base in (parent_dir, *include_dirs):
                candidate = self._canonical_path(os.path.join(base, raw_name))
                if candidate not in seen_candidates:
                    search_candidates.append(candidate)
                    seen_candidates.add(candidate)

        for candidate in search_candidates:
            if candidate in exact and os.path.isfile(candidate):
                return self._remember_include_resolution(cache_key, candidate)
        basename_matches = [
            candidate
            for candidate in exact
            if os.path.basename(candidate) == os.path.basename(raw_name)
            and os.path.isfile(candidate)
        ]
        if len(dict.fromkeys(basename_matches)) == 1:
            return self._remember_include_resolution(
                cache_key, basename_matches[0]
            )
        for candidate in search_candidates:
            if os.path.isfile(candidate):
                return self._remember_include_resolution(cache_key, candidate)
        return None

    def _remember_include_resolution(
        self,
        key: tuple[str, str, tuple[str, ...]],
        resolved: str,
    ) -> str:
        """Publish one positive resolution into the bounded process-local LRU."""

        if not self._include_resolution_cache_limit:
            return resolved
        previous = self._include_resolution_cache.pop(key, None)
        if previous is None:
            while (
                len(self._include_resolution_cache)
                >= self._include_resolution_cache_limit
            ):
                self._include_resolution_cache.popitem(last=False)
                self._include_resolution_cache_eviction_count += 1
        self._include_resolution_cache[key] = resolved
        return resolved

    def preprocess(self, source_path: str) -> PreprocessedSource:
        root = self._canonical_path(source_path)
        contexts = self._context_index.contexts_for(root)
        options = self._options_cache.get(contexts)
        if options is None:
            options = _extract_options_from_contexts(contexts)
            self._options_cache[contexts] = options
        state = _ExpansionState(
            macros=options.macro_dict(),
            text_macros={
                name: _TextMacro(parameters=None, body=value)
                for name, value in options.macros
            },
            include_tree={},
            ordered_includes=[],
            included_paths=[],
            root_include_directives=[],
            active_include_directives=[],
            conditional_macros=set(),
            issues=[],
            visited_paths=set(),
            trusted_hierarchy_parts=[],
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
        if state.has_conditionals and not options.complete:
            state.issue(
                "compile_options_incomplete",
                invalidates_all_hierarchy=True,
            )
        for parent in state.visited_paths:
            exact_children = set(self._exact_includes.get(parent, ()))
            if not exact_children:
                continue
            resolved_children = set(state.include_tree.get(parent, ()))
            if resolved_children != exact_children:
                state.issue(
                    "include_evidence_mismatch",
                    taints_hierarchy=False,
                )
        trusted_hierarchy_text = "".join(state.trusted_hierarchy_parts)
        return PreprocessedSource(
            text=text,
            root_text=raw,
            included_paths=tuple(state.included_paths),
            include_tree=state.include_tree,
            ordered_includes=tuple(state.ordered_includes),
            root_include_directives=tuple(state.root_include_directives),
            active_include_directives=tuple(state.active_include_directives),
            conditional_macros=tuple(sorted(state.conditional_macros)),
            has_conditional_preprocessing=state.has_conditionals,
            complete=not state.issues,
            issues=tuple(state.issues),
            trusted_hierarchy_text=trusted_hierarchy_text,
            hierarchy_evidence_status=(
                "complete"
                if not state.issues
                else (
                    "positive_local"
                    if trusted_hierarchy_text
                    else "unproved"
                )
            ),
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
        self._logical_file_expansion_count += 1
        canonical = self._canonical_path(path)
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
        if "`" not in raw:
            # Without a backtick this file cannot contain a directive or a
            # hierarchy macro invocation.  Returning the immutable source
            # text is equivalent to the line-wise expansion and avoids a
            # full comment-masking pass for ordinary RTL files.
            check_cancelled()
            if not state.hierarchy_tainted:
                state.trusted_hierarchy_parts.append(raw)
            return raw

        output: list[str] = []

        def emit(piece: str) -> None:
            output.append(piece)
            if not state.hierarchy_tainted:
                state.trusted_hierarchy_parts.append(piece)

        frames: list[_ConditionalFrame] = []
        active = True
        in_block_comment = False
        continued_define: list[str] | None = None
        continued_define_active = False
        cached_masked = self._cached_masked_text(canonical, raw)
        if cached_masked is not None:
            self._masked_text_cache_hit_count += 1
        masked_offset = 0
        collect_masked = (
            cached_masked is None
            and depth > 0
            and self._cache_limit > 0
            and len(raw) * 2 <= self._cache_limit
        )
        masked_parts: list[str] | None = [] if collect_masked else None
        for line in raw.splitlines(keepends=True):
            check_cancelled()
            if cached_masked is None:
                self._comment_mask_line_count += 1
                if not in_block_comment and "/" not in line:
                    self._comment_mask_fast_path_count += 1
                masked, in_block_comment = _mask_comments(
                    line,
                    in_block_comment,
                )
                if masked_parts is not None:
                    masked_parts.append(masked)
            else:
                masked_end = masked_offset + len(line)
                masked = cached_masked[masked_offset:masked_end]
                masked_offset = masked_end
            newline = "\n" if line.endswith("\n") else ""
            match = _DIRECTIVE_RE.match(masked)
            if match and depth == 0 and match.group("name").lower() == "include":
                literal = _INCLUDE_LITERAL_RE.match(match.group("body"))
                if (
                    literal
                    and literal.group(1) not in state.root_include_directives
                ):
                    state.root_include_directives.append(literal.group(1))
            if continued_define is not None:
                part, continues = _strip_line_continuation(masked)
                continued_define.append(part)
                emit(newline)
                if not continues:
                    if continued_define_active:
                        _record_source_macro(state, " ".join(continued_define))
                    continued_define = None
                    continued_define_active = False
                continue
            if not match:
                emit(
                    _expand_hierarchy_macro_line(line, masked, state)
                    if active
                    else ("\n" if line.endswith("\n") else "")
                )
                continue

            directive = match.group("name").lower()
            body = match.group("body")
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
                emit(newline)
                continue
            if directive == "elsif":
                state.has_conditionals = True
                identifier = _directive_identifier(body)
                if identifier:
                    state.conditional_macros.add(identifier)
                if not frames:
                    state.issue("conditional_unbalanced")
                    emit(newline)
                    continue
                frame = frames[-1]
                condition = bool(identifier and identifier in state.macros)
                take = not frame.branch_taken and condition
                frame.active = frame.parent_active and take
                frame.branch_taken = frame.branch_taken or condition
                active = frame.active
                emit(newline)
                continue
            if directive == "else":
                state.has_conditionals = True
                if not frames:
                    state.issue("conditional_unbalanced")
                    emit(newline)
                    continue
                frame = frames[-1]
                take = not frame.branch_taken
                frame.active = frame.parent_active and take
                frame.branch_taken = True
                active = frame.active
                emit(newline)
                continue
            if directive == "endif":
                state.has_conditionals = True
                if not frames:
                    state.issue("conditional_unbalanced")
                else:
                    frame = frames.pop()
                    active = frame.parent_active
                emit(newline)
                continue
            if directive == "define":
                first_part, continues = _strip_line_continuation(body)
                if continues:
                    continued_define = [first_part]
                    continued_define_active = active
                    emit(newline)
                    continue
            if not active:
                emit(newline)
                continue
            if directive == "define":
                _record_source_macro(state, body)
                # A macro definition is not active HDL source. Keeping its
                # replacement tokens here can fabricate an instance before
                # the macro is ever invoked.
                emit(newline)
                continue
            if directive == "undef":
                identifier = _directive_identifier(body)
                if identifier:
                    state.macros.pop(identifier, None)
                    state.text_macros.pop(identifier, None)
                emit(newline)
                continue
            if directive == "undefineall":
                state.macros.clear()
                state.text_macros.clear()
                emit(newline)
                continue
            if directive == "include":
                raw_name = _macro_include_name(body, state.macros)
                if raw_name is None:
                    state.issue("include_expression_unresolved")
                    emit(newline)
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
                    emit(newline)
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
                # The child records its trusted pieces directly in expansion
                # order. Append only to the full local text here so the trusted
                # stream is not duplicated.
                output.append(child_text)
                if child_text and not child_text.endswith("\n") and newline:
                    emit("\n")
                continue
            emit(line)

        if continued_define is not None:
            state.issue("macro_continuation_unterminated")
        if frames:
            state.issue("conditional_unbalanced")
        if masked_parts is not None:
            check_cancelled()
            self._store_masked_text(canonical, raw, "".join(masked_parts))
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
