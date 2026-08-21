"""
compile_log_parser.py
Extract user files, filelist relationships, include relationships, and top information
from compile and elaborate logs.
"""

import os
import re
import shlex
from copy import deepcopy
from pathlib import Path

from .filelist_tokenizer import tokenize_filelist


EDA_LIB_PREFIXES = [
    "/tools/synopsys/",
    "/tools/cadence/",
    "/tools/mentor/",
    "$VCS_HOME",
    "$XCELIUM_HOME",
    "$XLM_ROOT",
    "$UVM_HOME",
]


_VCS_FILE_RE = re.compile(r"Parsing design file '([^']+)'")
_VCS_INC_RE = re.compile(r"Parsing included file '([^']+)'")
_VCS_BACK_RE = re.compile(r"Back to file '([^']+)'")
_VCS_TOP_RE = re.compile(r"^\s+([A-Za-z_]\w*)\s*$")
_VCS_IF_RE = re.compile(r"recompiling interface (\w+)", re.IGNORECASE)
_VCS_SHELL_COMMAND_RE = re.compile(
    r"^\s*cd\s+(?P<cwd>.+?)\s+&&\s+"
    r"(?P<command>(?:\S*/)?vcs(?:\s+.*)?)\s*$"
)

_XCE_FILE_RE = re.compile(r"^file:\s+(.+)$")
_XCE_ENTITY_RE = re.compile(
    r"^\s*(module|interface|package)\s+worklib\.(\w+):", re.IGNORECASE
)
_XCE_SHELL_COMMAND_RE = re.compile(
    r"^\s*cd\s+(?P<cwd>.+?)\s+&&\s+"
    r"(?P<command>(?:\S*/)?xrun(?:\s+.*)?)\s*$"
)
_TOP_RE = re.compile(r"(?:^|\s)-top\s+(\w+)")
_SNAPSHOT_RE = re.compile(r"(?:^|\s)-snapshot\s+(\w+)")
_SOURCE_SUFFIXES = (".v", ".sv", ".vh", ".svh", ".vhd", ".vhdl")
_ENV_REF_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[^}]+\})")
_VCS_FILELIST_MAX_DEPTH = 16
_VCS_FILELIST_MAX_TOKENS = 100_000
_SIMULATOR_DETECT_MAX_LINES = 1_000
_COMPILE_EVIDENCE_SCHEMA_VERSION = 1
_LOG_CANCEL_CHECK_STRIDE = 4096
_VCS_FLAGS_WITH_VALUE = frozenset(
    {
        "-assert",
        "-cm_dir",
        "-l",
        "-Mdir",
        "-ntb_opts",
        "-o",
        "-P",
        "-timescale",
        "-work",
        "-y",
    }
)
_VCS_MARKERS = (
    "chronologic vcs",
    "parsing design file",
    "parsing included file",
    "back to file '",
    "synopsys vcs",
    "vcs-mx",
    "vlogan",
    "vhdlan",
    "/vcs_mx/",
    "/vcs/",
    "vcs_home",
    "simv.daidir",
    "script_home",
    "&& vcs ",
)
_XCE_MARKERS = (
    "xrun",
    "xmvlog",
    "xmelab",
    "xmsim",
    "xcelium",
    "incisive",
    "cadence design systems",
    "xlm_",
    "xcelium_home",
)


def _normalize_path(path: str, parent: str | None = None) -> str:
    path = path.strip().strip("'\"").rstrip(".")
    path = os.path.expandvars(path)
    if parent and not os.path.isabs(path):
        path = os.path.join(parent, path)
    return os.path.normpath(os.path.realpath(path))


def _normalize_reported_path(path: str, parent: str | None = None) -> str:
    """Normalize a concrete log spelling without erasing symlink evidence."""

    path = os.path.expandvars(path.strip().strip("'\"").rstrip("."))
    if parent and not os.path.isabs(path):
        path = os.path.join(parent, path)
    return os.path.normpath(os.path.abspath(path))


def _is_eda_lib(path: str) -> bool:
    normalized = path.replace("\\", "/")
    for prefix in EDA_LIB_PREFIXES:
        expanded = os.path.expandvars(prefix).replace("\\", "/")
        if normalized.startswith(expanded):
            return True
    return False


def _compilation_unit_role(path: str) -> str:
    """Classify a simulator-reported unit without hiding it from evidence.

    ``files.user`` intentionally filters installed EDA libraries for hierarchy
    browsing.  Source replay needs a different distinction: language-visible
    packages such as ``uvm_pkg.sv`` can be required by a frontend, while VCS /
    Verdi recorder installers are simulator instrumentation and must not be
    treated as project HDL merely because the compile log reports them.
    """

    name = os.path.basename(path).lower()
    if "uvm_custom_install" in name and "record" in name:
        return "simulator_instrumentation"
    if not _is_eda_lib(path):
        return "project"
    return "simulator_library"


def _compilation_unit_record(
    path: str,
    file_info: dict[str, dict],
    *,
    reported_path: str | None = None,
) -> dict:
    result = {
        "path": path,
        "type": file_info.get(path, {}).get("type", "unknown"),
        "role": _compilation_unit_role(path),
    }
    if reported_path and reported_path != path:
        result["reported_path"] = reported_path
    return result


def _categorize(path: str) -> str:
    lower = path.lower()
    if "tb" in lower or "testbench" in lower or "verif" in lower:
        return "tb"
    if "rtl" in lower or "dut" in lower or "design" in lower or "des_" in lower:
        return "rtl"
    if "assert" in lower or "sva" in lower:
        return "assertion"
    return "other"


def detect_simulator(log_path: str) -> str:
    try:
        with open(log_path, "r", errors="replace") as f:
            for _, line in zip(range(_SIMULATOR_DETECT_MAX_LINES), f):
                lower = line.lower()
                if any(marker in lower for marker in _VCS_MARKERS):
                    return "vcs"
                if any(marker in lower for marker in _XCE_MARKERS):
                    return "xcelium"
    except OSError:
        return "unknown"
    return "unknown"


def _collect_user_files(
    file_info: dict[str, dict], *, preserve_order: bool = False
) -> tuple[list[dict], int]:
    user = []
    filtered_count = 0
    paths = file_info if preserve_order else sorted(file_info)
    for path in paths:
        if _is_eda_lib(path):
            filtered_count += 1
            continue
        info = file_info[path]
        user.append(
            {
                "path": path,
                "type": info.get("type", "unknown"),
                "category": _categorize(path),
            }
        )
    return user, filtered_count


def _record_definition(
    definitions: dict[str, dict[str, list[str]]],
    kind: str,
    name: str,
    path: str | None,
) -> None:
    if not path:
        return
    paths = definitions[kind].setdefault(name, [])
    if path not in paths:
        paths.append(path)


def parse_vcs_compile_log(log_path: str) -> dict:
    log_dir = os.path.dirname(log_path)
    # Only the invocation itself needs limited look-ahead for continuation
    # lines.  Keep that small prefix in memory and stream the (potentially
    # multi-hundred-thousand-line) compile evidence below.
    invocation_lines = _read_vcs_invocation_lines(log_path)
    compile_command, command_dir = _extract_vcs_invocation(
        invocation_lines, log_dir
    )
    parse_warnings: list[str] = []
    command_tokens = _tokenize_vcs_text(
        compile_command or "", "VCS Command", parse_warnings
    )
    incdirs = _extract_vcs_incdirs(command_tokens, command_dir)
    filelist_tree = _direct_vcs_filelist_tree(command_tokens, command_dir)
    recovered_files: dict[str, dict] = {}
    recovered_filelists: list[dict] = []
    if command_tokens:
        recovered_files, recovered_tree, recovered_incdirs, recovered_filelists = (
            _recover_vcs_command_files(
                command_tokens,
                command_dir,
                parse_warnings,
            )
        )
        if recovered_tree:
            filelist_tree = recovered_tree
        incdirs = list(dict.fromkeys((*incdirs, *recovered_incdirs)))

    include_tree: dict[str, list[str]] = {}
    file_info: dict[str, dict] = {}
    interfaces: set[str] = set()
    definitions: dict[str, dict[str, list[str]]] = {
        "modules": {},
        "interfaces": {},
        "packages": {},
    }
    reported_top_modules: list[str] = []
    ordered_design_paths: list[str] = []
    ordered_reported_paths: list[str] = []
    ordered_includes: list[dict[str, str]] = []
    stack: list[str] = []
    in_top_section = False

    with open(log_path, "r", errors="replace") as stream:
        lines_seen = 0
        for line in stream:
            lines_seen += 1
            if lines_seen % _LOG_CANCEL_CHECK_STRIDE == 0:
                _check_cancelled()
            if line.startswith("Top Level Modules:"):
                in_top_section = True
                continue
            if in_top_section:
                match = _VCS_TOP_RE.match(line)
                if match:
                    reported_top_modules.append(match.group(1))
                    continue
                in_top_section = False

            match = _VCS_FILE_RE.search(line)
            if match:
                raw_path = match.group(1)
                path = _normalize_path(raw_path, command_dir)
                stack = [path]
                ordered_design_paths.append(path)
                ordered_reported_paths.append(
                    _normalize_reported_path(raw_path, command_dir)
                )
                file_info.setdefault(path, {"type": "module"})
                continue

            match = _VCS_INC_RE.search(line)
            if match and stack:
                parent = stack[-1]
                raw_child = match.group(1)
                child = _normalize_path(raw_child, os.path.dirname(parent))
                if not os.path.isabs(raw_child):
                    # VCS may echo an include either as a bare basename (resolved
                    # through +incdir) or as a cwd-relative path such as
                    # ``src/core/defs.svh``. Trying the compile cwd first avoids
                    # incorrectly nesting the latter beneath the including file.
                    for base in (command_dir, *incdirs):
                        candidate = _normalize_path(raw_child, base)
                        if os.path.exists(candidate):
                            child = candidate
                            break
                include_tree.setdefault(parent, [])
                if child not in include_tree[parent]:
                    include_tree[parent].append(child)
                ordered_includes.append({"parent": parent, "path": child})
                file_info.setdefault(child, {"type": "unknown"})
                stack.append(child)
                continue

            match = _VCS_BACK_RE.search(line)
            if match:
                target = _normalize_path(match.group(1), command_dir)
                while stack and stack[-1] != target:
                    stack.pop()
                continue

            # VCS emits ``recompiling module/package/interface`` as a detached
            # inline-pass summary after every design file has been parsed.  The
            # names are useful as inventory hints but carry no source path;
            # associating them with the current include stack would silently
            # map every summary entry to the last parsed file.  Preserve the
            # historical interface-name list, but leave definition-to-file
            # proof to bounded bootstrap's capped source inventory.
            match = _VCS_IF_RE.search(line)
            if match:
                interfaces.add(match.group(1))
                continue

    used_command_fallback = not file_info
    if used_command_fallback and command_tokens:
        file_info.update(recovered_files)

    command_tops = _extract_vcs_tops(command_tokens)
    top_modules = command_tops or list(dict.fromkeys(reported_top_modules))
    primary_top = _select_primary_top(top_modules)
    if primary_top:
        top_modules = [primary_top, *(top for top in top_modules if top != primary_top)]

    user, filtered_count = _collect_user_files(
        file_info, preserve_order=used_command_fallback
    )
    if ordered_design_paths:
        evidence_paths = ordered_design_paths
        unit_order_source = "simulator_log"
    elif used_command_fallback and recovered_files:
        evidence_paths = list(recovered_files)
        unit_order_source = "command_recovery"
    else:
        evidence_paths = []
        unit_order_source = "unavailable"
    return {
        "simulator": "vcs",
        # Adapter-facing provenance: relative command/filelist and parser-output
        # paths are interpreted from the directory in which VCS actually ran.
        # DVSim/FuseSoC wrapper logs commonly live one level above that cwd.
        "compile_cwd": command_dir,
        "primary_top": primary_top,
        "top_modules": top_modules,
        "reported_top_modules": list(dict.fromkeys(reported_top_modules)),
        "files": {
            "user": user,
            "filtered_count": filtered_count,
        },
        "include_tree": include_tree,
        "filelist_tree": filelist_tree,
        "interfaces": sorted(interfaces),
        "definitions": definitions,
        "compile_command": compile_command,
        "parse_warnings": parse_warnings,
        "compile_evidence": {
            "schema_version": _COMPILE_EVIDENCE_SCHEMA_VERSION,
            "unit_order_source": unit_order_source,
            "ordered_compilation_units": [
                _compilation_unit_record(
                    path,
                    file_info,
                    reported_path=(
                        ordered_reported_paths[index]
                        if unit_order_source == "simulator_log"
                        else None
                    ),
                )
                for index, path in enumerate(evidence_paths)
            ],
            "ordered_includes": ordered_includes,
            "filelists": recovered_filelists,
            "expanded_replay_command": None,
        },
    }


def _check_cancelled() -> None:
    """Keep the parser lightweight when cancellation support is unavailable.

    compile_log_parser is also used by small standalone scripts.  Importing
    lazily avoids adding server/runtime initialization to those callers while
    still making long MCP parses cooperatively cancellable.
    """

    try:
        from .cancellation import check_cancelled  # noqa: PLC0415
    except ImportError:
        return
    check_cancelled()


def _read_vcs_invocation_lines(log_path: str) -> list[str]:
    """Return only the VCS invocation evidence, never the whole log."""

    wrapper: list[str] = []
    with open(log_path, "r", errors="replace") as stream:
        iterator = iter(stream)
        for line_number, line in enumerate(iterator, 1):
            if line_number % _LOG_CANCEL_CHECK_STRIDE == 0:
                _check_cancelled()
            if not wrapper and _VCS_SHELL_COMMAND_RE.match(line.rstrip("\n")):
                wrapper = [line]
            stripped = line.lstrip()
            if not stripped.startswith("Command:"):
                continue
            result = [line]
            body = stripped[len("Command:") :].strip()
            while body.endswith("\\"):
                try:
                    continuation = next(iterator)
                except StopIteration:
                    break
                result.append(continuation)
                body = continuation.rstrip("\n")
            return result
    return wrapper


def _extract_vcs_command(lines: list[str]) -> str | None:
    """Return the verbatim `vcs ...` invocation line(s) from a compile log.

    VCS prepends `Command: <invocation>` to the log, possibly with shell
    line continuations (trailing backslash). Returns the joined command
    with newlines collapsed, or None if not found.
    """
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith("Command:"):
            continue
        body = stripped[len("Command:") :].strip()
        parts = [body.rstrip(" \\")]
        i = idx
        while body.endswith("\\"):
            i += 1
            if i >= len(lines):
                break
            cont = lines[i].rstrip("\n")
            parts.append(cont.rstrip(" \\").lstrip())
            body = cont
        return " ".join(p for p in parts if p)
    return None


def _extract_vcs_invocation(lines: list[str], log_dir: str) -> tuple[str | None, str]:
    """Recover a VCS command and its execution directory without a shell.

    Native VCS logs use ``Command: vcs ...``. Orchestrators such as DVSim emit
    the bounded wrapper shape ``cd <workdir> && vcs ...`` before the VCS banner.
    Only those two textual forms are recognized; neither is evaluated.
    """

    command = _extract_vcs_command(lines)
    if command:
        return command, log_dir

    for line in lines:
        match = _VCS_SHELL_COMMAND_RE.match(line.rstrip("\n"))
        if not match:
            continue
        command_dir = _normalize_path(match.group("cwd"), log_dir)
        command = match.group("command")
        executable, separator, rest = command.partition(" ")
        if Path(executable).name == "vcs":
            command = f"vcs{separator}{rest}"
        return command, command_dir
    return None, log_dir


def _tokenize_vcs_text(
    text: str,
    context: str,
    warnings: list[str],
) -> list[str]:
    """Tokenize a recorded command/filelist without invoking a shell."""
    if not text.strip():
        return []
    try:
        return shlex.split(text, comments=True, posix=True)
    except ValueError as exc:
        warnings.append(f"{context} tokenization failed: {exc}")
        return []


def _extract_vcs_tops(tokens: list[str]) -> list[str]:
    tops: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        value = None
        if token == "-top" and index + 1 < len(tokens):
            value = tokens[index + 1]
            index += 2
        elif token.startswith("-top="):
            value = token.split("=", 1)[1]
            index += 1
        else:
            index += 1
        if value and re.fullmatch(r"[A-Za-z_]\w*", value) and value not in tops:
            tops.append(value)
    return tops


def _select_primary_top(tops: list[str]) -> str | None:
    """Choose the user-facing simulation root while retaining every top.

    Multi-top DV builds place bind modules before the actual testbench. Prefer
    the conventional exact ``tb`` root, then a single unambiguous ``*_tb`` or
    ``tb_*`` name; otherwise preserve the tool's first top.
    """

    if not tops:
        return None
    if "tb" in tops:
        return "tb"
    tb_like = [
        top
        for top in tops
        if top.lower().startswith("tb_") or top.lower().endswith("_tb")
    ]
    return tb_like[0] if len(tb_like) == 1 else tops[0]


def _extract_vcs_incdirs(tokens: list[str], command_dir: str) -> list[str]:
    incdirs: list[str] = []
    for token in tokens:
        if not token.startswith("+incdir+"):
            continue
        for raw_path in token[len("+incdir+") :].split("+"):
            if not raw_path:
                continue
            path = _normalize_path(raw_path, command_dir)
            if path not in incdirs:
                incdirs.append(path)
    return incdirs


def _direct_vcs_filelist_tree(
    tokens: list[str], command_dir: str
) -> dict[str, list[str]]:
    tree: dict[str, list[str]] = {}
    index = 0
    while index < len(tokens):
        if tokens[index] in {"-f", "-F"} and index + 1 < len(tokens):
            path = _normalize_path(tokens[index + 1], command_dir)
            tree.setdefault(os.path.basename(path), [])
            index += 2
        else:
            index += 1
    return tree


def _recover_vcs_command_files(
    tokens: list[str],
    command_dir: str,
    warnings: list[str],
) -> tuple[dict[str, dict], dict[str, list[str]], list[str], list[dict]]:
    """Recover sources from a no-op VCS command and its filelists.

    This is deliberately a tokenizer plus bounded local-file reader. It never
    performs globbing, command substitution, variable execution, or any other
    shell evaluation.
    """
    file_info: dict[str, dict] = {}
    filelist_tree: dict[str, list[str]] = {}
    state = {
        "active": set(),
        "visited": set(),
        "token_count": 0,
        "token_limit_reported": False,
        "incdirs": [],
        "filelists": [],
    }
    _scan_vcs_tokens(
        tokens,
        source_base=command_dir,
        command_dir=command_dir,
        file_info=file_info,
        filelist_tree=filelist_tree,
        warnings=warnings,
        state=state,
        parent_filelist=None,
        depth=0,
    )
    return file_info, filelist_tree, state["incdirs"], state["filelists"]


def _scan_vcs_tokens(
    tokens: list[str],
    *,
    source_base: str,
    command_dir: str,
    file_info: dict[str, dict],
    filelist_tree: dict[str, list[str]],
    warnings: list[str],
    state: dict,
    parent_filelist: str | None,
    depth: int,
) -> None:
    state["token_count"] += len(tokens)
    if state["token_count"] > _VCS_FILELIST_MAX_TOKENS:
        if not state["token_limit_reported"]:
            warnings.append(
                "VCS filelist token limit exceeded; remaining entries were ignored"
            )
            state["token_limit_reported"] = True
        return

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if index == 0 and Path(token).name in {"vcs", "vlogan", "vhdlan"}:
            index += 1
            continue
        if token.startswith("+incdir+"):
            for raw_path in token[len("+incdir+") :].split("+"):
                if not raw_path:
                    continue
                path = _normalize_path(raw_path, source_base)
                if path not in state["incdirs"]:
                    state["incdirs"].append(path)
            index += 1
            continue
        if token in {"-f", "-F"} and index + 1 < len(tokens):
            raw_filelist = tokens[index + 1]
            filelist_base = command_dir if token == "-f" else source_base
            filelist_path = _normalize_path(raw_filelist, filelist_base)
            entries_base = (
                command_dir if token == "-f" else os.path.dirname(filelist_path)
            )
            _expand_vcs_filelist(
                filelist_path,
                raw_path=raw_filelist,
                mode=token,
                entries_base=entries_base,
                command_dir=command_dir,
                file_info=file_info,
                filelist_tree=filelist_tree,
                warnings=warnings,
                state=state,
                parent_filelist=parent_filelist,
                depth=depth + 1,
            )
            index += 2
            continue
        if token == "-v" and index + 1 < len(tokens):
            _add_vcs_source(tokens[index + 1], source_base, file_info, warnings)
            index += 2
            continue
        if token in _VCS_FLAGS_WITH_VALUE and index + 1 < len(tokens):
            index += 2
            continue
        if token == "-top" and index + 1 < len(tokens):
            index += 2
            continue
        if token.startswith(("-", "+")):
            index += 1
            continue
        _add_vcs_source(token, source_base, file_info, warnings)
        index += 1


def _expand_vcs_filelist(
    filelist_path: str,
    *,
    raw_path: str,
    mode: str,
    entries_base: str,
    command_dir: str,
    file_info: dict[str, dict],
    filelist_tree: dict[str, list[str]],
    warnings: list[str],
    state: dict,
    parent_filelist: str | None,
    depth: int,
) -> None:
    name = os.path.basename(filelist_path)
    filelist_tree.setdefault(name, [])
    if parent_filelist is not None:
        children = filelist_tree.setdefault(os.path.basename(parent_filelist), [])
        if name not in children:
            children.append(name)

    state["filelists"].append(
        {
            "path": filelist_path,
            "raw_path": raw_path,
            "parent": parent_filelist,
            "depth": depth,
            "mode": mode,
        }
    )

    if filelist_path in state["active"]:
        warnings.append(f"VCS filelist cycle ignored: {filelist_path}")
        return
    if filelist_path in state["visited"]:
        return
    if depth > _VCS_FILELIST_MAX_DEPTH:
        warnings.append(f"VCS filelist depth limit exceeded: {filelist_path}")
        return
    if not os.path.isfile(filelist_path):
        if _ENV_REF_RE.search(raw_path):
            warnings.append(
                "VCS environment-dependent filelist unavailable: "
                f"{raw_path} -> {filelist_path}"
            )
        else:
            warnings.append(f"VCS filelist missing: {filelist_path}")
        return

    try:
        text = Path(filelist_path).read_text(errors="replace")
    except OSError as exc:
        warnings.append(f"VCS filelist unreadable: {filelist_path}: {exc}")
        return

    try:
        tokens = tokenize_filelist(text)
    except ValueError as exc:
        warnings.append(f"VCS filelist {filelist_path} tokenization failed: {exc}")
        return
    state["active"].add(filelist_path)
    state["visited"].add(filelist_path)
    try:
        _scan_vcs_tokens(
            tokens,
            source_base=entries_base,
            command_dir=command_dir,
            file_info=file_info,
            filelist_tree=filelist_tree,
            warnings=warnings,
            state=state,
            parent_filelist=filelist_path,
            depth=depth,
        )
    finally:
        state["active"].remove(filelist_path)


def _add_vcs_source(
    raw_path: str,
    source_base: str,
    file_info: dict[str, dict],
    warnings: list[str],
) -> None:
    if not raw_path.lower().endswith(_SOURCE_SUFFIXES):
        return
    path = _normalize_path(raw_path, source_base)
    if not os.path.isfile(path):
        if _ENV_REF_RE.search(raw_path):
            warnings.append(
                f"VCS environment-dependent source unavailable: {raw_path} -> {path}"
            )
        else:
            warnings.append(f"VCS source missing: {path}")
        return
    file_info.setdefault(path, {"type": "unknown"})


def parse_xcelium_compile_log(log_path: str) -> dict:
    log_dir = os.path.dirname(log_path)
    invocation_lines = _read_xcelium_invocation_lines(log_path)
    compile_command, replay_command, command_dir = _extract_xcelium_invocation(
        invocation_lines, log_dir
    )
    expanded_replay_command, evidence_filelists = _extract_xcelium_expanded_evidence(
        invocation_lines, command_dir
    )
    file_info: dict[str, dict] = {}
    include_tree: dict[str, list[str]] = {}
    filelist_tree: dict[str, list[str]] = {}
    interfaces: set[str] = set()
    definitions: dict[str, dict[str, list[str]]] = {
        "modules": {},
        "interfaces": {},
        "packages": {},
    }
    top_modules: list[str] = []
    command_started = False
    current_file: str | None = None
    filelist_stack: list[tuple[int, str]] = []
    expanded_source_paths: list[str] = []
    ordered_unit_paths: list[str] = []
    ordered_reported_paths: list[str] = []

    if compile_command:
        for top_match in _TOP_RE.finditer(compile_command):
            top = top_match.group(1)
            if top not in top_modules:
                top_modules.append(top)
        snapshot_match = _SNAPSHOT_RE.search(compile_command)
        if snapshot_match and snapshot_match.group(1) in top_modules:
            snapshot_top = snapshot_match.group(1)
            top_modules.remove(snapshot_top)
            top_modules.insert(0, snapshot_top)
        for filelist_match in re.finditer(r"(?:^|\s)-f\s+(\S+)", compile_command):
            path = _normalize_path(filelist_match.group(1), command_dir)
            filelist_tree.setdefault(os.path.basename(path), [])

    for line in invocation_lines:
        stripped = line.strip()
        if stripped == "xrun":
            command_started = True
        if not command_started:
            continue
        if _XCE_FILE_RE.match(line):
            break

        top_match = _TOP_RE.search(line)
        if top_match and top_match.group(1) not in top_modules:
            top_modules.append(top_match.group(1))

        if not stripped or stripped.startswith(("+define", "-incdir", "+incdir")):
            continue

        indent = len(line) - len(line.lstrip(" \t"))
        filelist_match = re.search(r"-f\s+(\S+)", stripped)
        if filelist_match:
            path = _normalize_path(filelist_match.group(1), command_dir)
            name = os.path.basename(path)
            filelist_tree.setdefault(name, [])
            while filelist_stack and filelist_stack[-1][0] >= indent:
                filelist_stack.pop()
            if filelist_stack:
                parent_name = os.path.basename(filelist_stack[-1][1])
                filelist_tree.setdefault(parent_name, [])
                if name not in filelist_tree[parent_name]:
                    filelist_tree[parent_name].append(name)
            filelist_stack.append((indent, path))
            continue

        if stripped.startswith("-") or stripped.startswith("+"):
            continue

        if stripped.lower().endswith(_SOURCE_SUFFIXES):
            path = _normalize_path(stripped, command_dir)
            file_info.setdefault(path, {"type": "unknown"})
            expanded_source_paths.append(path)

    with open(log_path, "r", errors="replace") as stream:
        lines_seen = 0
        for line in stream:
            lines_seen += 1
            if lines_seen % _LOG_CANCEL_CHECK_STRIDE == 0:
                _check_cancelled()
            match = _XCE_FILE_RE.match(line)
            if match:
                raw_path = match.group(1)
                current_file = _normalize_path(raw_path, command_dir)
                ordered_unit_paths.append(current_file)
                ordered_reported_paths.append(
                    _normalize_reported_path(raw_path, command_dir)
                )
                file_info.setdefault(current_file, {"type": "unknown"})
                continue
            match = _XCE_ENTITY_RE.match(line)
            if match and current_file:
                entity_type, entity_name = match.group(1).lower(), match.group(2)
                file_info[current_file]["type"] = entity_type
                _record_definition(
                    definitions,
                    f"{entity_type}s",
                    entity_name,
                    current_file,
                )
                if entity_type == "interface":
                    interfaces.add(entity_name)

    user, filtered_count = _collect_user_files(file_info)
    if ordered_unit_paths:
        evidence_paths = ordered_unit_paths
        unit_order_source = "simulator_log"
    elif expanded_source_paths:
        evidence_paths = expanded_source_paths
        unit_order_source = "expanded_invocation"
    else:
        evidence_paths = []
        unit_order_source = "unavailable"
    return {
        "simulator": "xcelium",
        # Preserve the cwd recovered by _extract_xcelium_invocation so an
        # on-demand source frontend can replay relative inputs without guessing.
        "compile_cwd": command_dir,
        "top_modules": top_modules,
        "files": {
            "user": user,
            "filtered_count": filtered_count,
        },
        "include_tree": include_tree,
        "filelist_tree": filelist_tree,
        "interfaces": sorted(interfaces),
        "definitions": definitions,
        "compile_command": compile_command,
        # Native xrun logs indent expanded command-file contents beneath the
        # top-level ``-f`` argument.  ``compile_command`` intentionally keeps
        # that historical flattened view; the replay form removes only those
        # deeper, echoed lines so an on-demand frontend does not expand the
        # same command file twice.
        "compile_replay_command": replay_command,
        "compile_evidence": {
            "schema_version": _COMPILE_EVIDENCE_SCHEMA_VERSION,
            "unit_order_source": unit_order_source,
            "ordered_compilation_units": [
                _compilation_unit_record(
                    path,
                    file_info,
                    reported_path=(
                        ordered_reported_paths[index]
                        if unit_order_source == "simulator_log"
                        else None
                    ),
                )
                for index, path in enumerate(evidence_paths)
            ],
            "ordered_includes": [],
            "filelists": evidence_filelists,
            "expanded_replay_command": expanded_replay_command,
        },
    }


def _read_xcelium_invocation_lines(log_path: str) -> list[str]:
    """Read only the indentation-bounded native xrun invocation block.

    Wrapper logs that contain ``cd ... && xrun ...`` need just that one line.
    In either form the large compile/entity transcript remains streamed by the
    parser instead of being retained as a Python list of lines.
    """

    wrapper: list[str] = []
    invocation: list[str] = []
    collecting = False
    with open(log_path, "r", errors="replace") as stream:
        for line_number, line in enumerate(stream, 1):
            if line_number % _LOG_CANCEL_CHECK_STRIDE == 0:
                _check_cancelled()
            if collecting:
                rendered = line.rstrip("\n")
                if not rendered.strip() or rendered == rendered.lstrip(" \t"):
                    break
                invocation.append(line)
                continue
            if not wrapper and _XCE_SHELL_COMMAND_RE.match(line.rstrip("\n")):
                wrapper = [line]
            if line.strip() == "xrun":
                collecting = True
                invocation = [line]
    return invocation or wrapper


def _extract_xcelium_invocation(
    lines: list[str], log_dir: str
) -> tuple[str | None, str | None, str]:
    """Recover an xrun command and the directory in which it executed.

    Native xrun logs use an indented argument block. Build orchestrators such
    as OpenTitan dvsim instead record ``cd <workdir> && xrun ...`` before the
    tool output. Relative ``file:`` paths in that output are relative to the
    recorded work directory, not to the directory containing the wrapper log.
    The match is deliberately limited to that simple, emitted command shape;
    no shell text is evaluated.
    """
    command = _extract_xcelium_command(lines)
    if command:
        return command, _extract_xcelium_replay_command(lines), log_dir

    for line in lines:
        match = _XCE_SHELL_COMMAND_RE.match(line.rstrip("\n"))
        if not match:
            continue
        command_dir = _normalize_path(match.group("cwd"), log_dir)
        command = match.group("command")
        if command.startswith("/"):
            command = f"xrun{command[command.find('xrun') + len('xrun') :]}"
        return command, command, command_dir
    return None, None, log_dir


def _extract_xcelium_replay_command(lines: list[str]) -> str | None:
    """Return the top-level xrun invocation without echoed ``-f`` contents.

    Xcelium prints command-file contents one indentation level below the
    top-level arguments.  Retaining the historical flattened command is useful
    for source discovery, but replaying it together with ``-f`` compiles every
    command-file source twice.  Indentation is the simulator-provided boundary;
    no token de-duplication is performed, so intentional repeated compilation
    units in the original command remain intact.
    """

    for idx, line in enumerate(lines):
        if line.strip() != "xrun":
            continue
        parts = ["xrun"]
        base_indent: int | None = None
        i = idx + 1
        while i < len(lines):
            rendered = lines[i].rstrip("\n")
            stripped = rendered.strip()
            if not stripped:
                break
            if rendered == rendered.lstrip(" \t"):
                break
            indent = len(rendered) - len(rendered.lstrip(" \t"))
            if base_indent is None:
                base_indent = indent
            if indent == base_indent:
                parts.append(stripped.rstrip(" \\"))
            i += 1
        return " ".join(parts) if len(parts) > 1 else "xrun"
    return None


def _extract_xcelium_expanded_evidence(
    lines: list[str], command_dir: str
) -> tuple[str | None, list[dict]]:
    """Flatten a native xrun argument tree after removing ``-f`` markers.

    The nested contents are already the simulator's concrete expansion of each
    command file.  Keeping those contents recovers options even when the MCP
    process lacks the original environment, while dropping every ``-f`` pair
    prevents a second expansion.  Filelist paths remain separate support-file
    evidence with full parent/depth provenance.
    """

    for idx, line in enumerate(lines):
        if line.strip() != "xrun":
            continue
        flattened = ["xrun"]
        filelists: list[dict] = []
        stack: list[tuple[int, str]] = []
        i = idx + 1
        while i < len(lines):
            rendered = lines[i].rstrip("\n")
            stripped = rendered.strip()
            if not stripped or rendered == rendered.lstrip(" \t"):
                break
            indent = len(rendered) - len(rendered.lstrip(" \t"))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            try:
                tokens = shlex.split(stripped.rstrip(" \\"), posix=True)
            except ValueError:
                return None, []
            retained: list[str] = []
            token_index = 0
            while token_index < len(tokens):
                token = tokens[token_index]
                if token in {"-f", "-F"} and token_index + 1 < len(tokens):
                    raw_path = tokens[token_index + 1]
                    if os.path.isabs(raw_path):
                        path = _normalize_path(raw_path)
                    else:
                        path_base = (
                            os.path.dirname(stack[-1][1])
                            if token == "-F" and stack
                            else command_dir
                        )
                        path = _normalize_path(raw_path, path_base)
                    parent = stack[-1][1] if stack else None
                    filelists.append(
                        {
                            "path": path,
                            "raw_path": raw_path,
                            "parent": parent,
                            "depth": len(stack) + 1,
                            "mode": token,
                        }
                    )
                    stack.append((indent, path))
                    token_index += 2
                    continue
                retained.append(token)
                token_index += 1
            flattened.extend(retained)
            i += 1
        if len(flattened) == 1:
            return "xrun", filelists
        return shlex.join(flattened), filelists
    return None, []


def _extract_xcelium_command(lines: list[str]) -> str | None:
    """Return the verbatim `xrun ...` invocation, joining continuation lines.

    xrun logs emit each argument on its own indented line. Sources and
    flags interleave freely — ``-incdir`` paths and additional source
    files commonly appear *after* the first source file.  The emitted
    invocation block is indentation-bounded: arguments (including nested
    filelist expansions) are indented, while library definitions,
    diagnostics, and compile output resume in column zero.  Stop at that
    boundary so later log text cannot become command-line input.
    """
    for idx, line in enumerate(lines):
        if line.strip() == "xrun":
            parts = ["xrun"]
            i = idx + 1
            while i < len(lines):
                cont = lines[i].rstrip("\n")
                stripped = cont.strip()
                if not stripped:
                    break
                if cont == cont.lstrip(" \t"):
                    break
                parts.append(stripped.rstrip(" \\"))
                i += 1
            return " ".join(parts) if len(parts) > 1 else "xrun"
    return None


def _merge_phase_role(result: dict) -> str:
    evidence = result.get("compile_evidence")
    units = (
        evidence.get("ordered_compilation_units", [])
        if isinstance(evidence, dict)
        else []
    )
    files = result.get("files")
    user_files = files.get("user", []) if isinstance(files, dict) else []
    has_sources = bool(units or user_files)
    has_tops = bool(result.get("primary_top") or result.get("top_modules"))
    if has_sources and has_tops:
        return "compile_elaborate"
    if has_sources:
        return "compile"
    if has_tops:
        return "elaborate"
    return "unknown"


def _merge_phase_language(result: dict) -> str:
    """Classify a source phase for frontend replay without guessing semantics."""

    command = str(
        result.get("compile_replay_command")
        or result.get("compile_command")
        or ""
    )
    try:
        tokens = shlex.split(command, comments=True, posix=True)
    except ValueError:
        tokens = []
    executable = Path(tokens[0]).name.lower() if tokens else ""
    if executable == "vhdlan":
        return "vhdl"
    if executable == "vlogan":
        return "verilog_systemverilog"

    evidence = result.get("compile_evidence")
    units = (
        evidence.get("ordered_compilation_units", [])
        if isinstance(evidence, dict)
        else []
    )
    suffixes = {
        Path(str(item.get("path") or "")).suffix.lower()
        for item in units
        if isinstance(item, dict) and item.get("path")
    }
    has_vhdl = bool(suffixes & {".vhd", ".vhdl"})
    has_verilog = bool(suffixes & {".v", ".sv", ".vh", ".svh"})
    if has_vhdl and has_verilog:
        return "mixed"
    if has_vhdl:
        return "vhdl"
    if has_verilog:
        return "verilog_systemverilog"
    return "unknown"


def _merge_log_record(path: str, *, role: str, simulator: str) -> dict:
    canonical = str(Path(path).resolve(strict=False))
    try:
        stat_result = Path(canonical).stat()
    except OSError:
        size = None
        mtime_ns = None
    else:
        size = stat_result.st_size
        mtime_ns = stat_result.st_mtime_ns
    return {
        "path": canonical,
        "role": role,
        "simulator": simulator,
        "size": size,
        "mtime_ns": mtime_ns,
    }


def _stable_merge_file_entries(results: list[dict]) -> tuple[list[dict], int]:
    merged: list[dict] = []
    seen: set[str] = set()
    filtered_count = 0
    for result in results:
        files = result.get("files")
        if not isinstance(files, dict):
            continue
        try:
            filtered_count += int(files.get("filtered_count") or 0)
        except (TypeError, ValueError):
            pass
        user = files.get("user")
        if not isinstance(user, list):
            continue
        for item in user:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            key = str(Path(str(item["path"])).resolve(strict=False))
            if key in seen:
                continue
            seen.add(key)
            copied = deepcopy(item)
            copied["path"] = key
            merged.append(copied)
    return merged, filtered_count


def _stable_merge_mapping_lists(results: list[dict], field: str) -> dict:
    merged: dict[str, list] = {}
    for result in results:
        raw = result.get(field)
        if not isinstance(raw, dict):
            continue
        for key, value in raw.items():
            destination = merged.setdefault(str(key), [])
            if not isinstance(value, list):
                continue
            for item in value:
                if item not in destination:
                    destination.append(deepcopy(item))
    return merged


def _stable_merge_definitions(results: list[dict]) -> dict[str, dict[str, list[str]]]:
    merged: dict[str, dict[str, list[str]]] = {
        "modules": {},
        "interfaces": {},
        "packages": {},
    }
    for result in results:
        definitions = result.get("definitions")
        if not isinstance(definitions, dict):
            continue
        for kind in merged:
            by_name = definitions.get(kind)
            if not isinstance(by_name, dict):
                continue
            for name, raw_paths in by_name.items():
                if not isinstance(raw_paths, list):
                    continue
                destination = merged[kind].setdefault(str(name), [])
                for path in raw_paths:
                    rendered = str(path)
                    if rendered not in destination:
                        destination.append(rendered)
    return merged


def merge_compile_results(
    primary: dict,
    supplements: list[dict],
    *,
    primary_log: str,
    supplementary_logs: list[str],
) -> dict:
    """Merge complementary compile/elaboration parse results conservatively.

    Compilation-unit order is never deduplicated: repeated units and repeated
    filelist expansion can have language semantics. Browsing-oriented
    ``files.user`` entries are deduplicated independently by canonical path.
    Semantic conflicts remain in a fixed-label merge receipt and deliberately
    remove the elaboration top instead of guessing one.
    """

    if len(supplements) != len(supplementary_logs):
        raise ValueError("supplementary compile result/path count mismatch")

    results = [deepcopy(primary), *(deepcopy(item) for item in supplements)]
    paths = [primary_log, *supplementary_logs]
    canonical_paths = [str(Path(path).resolve(strict=False)) for path in paths]
    conflicts: set[str] = set()
    duplicate_indexes = {
        index
        for index, path in enumerate(canonical_paths)
        if path in canonical_paths[:index]
    }
    if duplicate_indexes:
        conflicts.add("duplicate_compile_log")

    primary_simulator = str(primary.get("simulator") or "unknown").lower()
    eligible: list[tuple[int, dict]] = []
    roles: list[str] = []
    for index, result in enumerate(results):
        role = _merge_phase_role(result)
        roles.append(role)
        simulator = str(result.get("simulator") or "unknown").lower()
        if index in duplicate_indexes:
            continue
        if simulator != primary_simulator:
            conflicts.add("simulator_mismatch")
            continue
        eligible.append((index, result))

    top_candidates: list[tuple[str, list[str], int]] = []
    for index, result in eligible:
        tops = [str(item) for item in result.get("top_modules", []) if item]
        primary_top = str(result.get("primary_top") or "")
        selected = primary_top or (tops[0] if tops else "")
        if selected:
            ordered = [selected, *(item for item in tops if item != selected)]
            top_candidates.append((selected, ordered, index))
    selected_top_names = {item[0] for item in top_candidates}
    if len(selected_top_names) > 1:
        conflicts.add("conflicting_elaboration_tops")
        primary_top = None
        top_modules: list[str] = []
    elif top_candidates:
        primary_top = top_candidates[0][0]
        top_modules = list(
            dict.fromkeys(
                item
                for _, ordered_tops, _ in top_candidates
                for item in ordered_tops
            )
        )
    else:
        primary_top = None
        top_modules = []

    eligible_results = [item for _, item in eligible]
    user_files, filtered_count = _stable_merge_file_entries(eligible_results)
    ordered_units: list[dict] = []
    ordered_includes: list[dict] = []
    filelists: list[dict] = []
    source_phases: list[dict] = []
    source_order_complete = True
    for index, result in eligible:
        evidence = result.get("compile_evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        units = evidence.get("ordered_compilation_units")
        units = units if isinstance(units, list) else []
        has_sources = bool(units) or _merge_phase_role(result) in {
            "compile",
            "compile_elaborate",
        }
        if has_sources:
            unit_order_source = str(
                evidence.get("unit_order_source") or "unavailable"
            )
            source_order_complete &= unit_order_source == "simulator_log"
            source_phases.append(
                {
                    "source_log_index": index,
                    "role": roles[index],
                    "language": _merge_phase_language(result),
                    "compile_cwd": str(result.get("compile_cwd") or ""),
                    "compile_command": result.get("compile_command"),
                    "compile_replay_command": result.get("compile_replay_command"),
                    "expanded_replay_command": evidence.get(
                        "expanded_replay_command"
                    ),
                    "unit_order_source": unit_order_source,
                }
            )
        for record in units:
            if not isinstance(record, dict):
                continue
            copied = deepcopy(record)
            copied["source_log_index"] = index
            ordered_units.append(copied)
        for field, destination in (
            ("ordered_includes", ordered_includes),
            ("filelists", filelists),
        ):
            raw = evidence.get(field)
            if not isinstance(raw, list):
                continue
            for record in raw:
                if not isinstance(record, dict):
                    continue
                copied = deepcopy(record)
                copied["source_log_index"] = index
                destination.append(copied)

    command_source = next(
        (
            result
            for _, result in eligible
            if _merge_phase_role(result) in {"compile", "compile_elaborate"}
            and (
                result.get("compile_replay_command")
                or result.get("compile_command")
            )
        ),
        eligible[0][1] if eligible else primary,
    )
    warnings: list[str] = []
    warning_records: list[dict] = []
    for source_log_index, result in eligible:
        raw_warnings = result.get("parse_warnings")
        if not isinstance(raw_warnings, list):
            continue
        for warning in raw_warnings:
            rendered = str(warning)
            if rendered not in warnings:
                warnings.append(rendered)
            record = {
                "source_log_index": source_log_index,
                "message": rendered,
            }
            if record not in warning_records:
                warning_records.append(record)
    if not source_order_complete and source_phases:
        warnings.append("compile_result_merge_source_order_incomplete")
    if conflicts:
        warnings.append("compile_result_merge_conflict")

    merged = deepcopy(primary)
    merged.update(
        {
            "simulator": primary_simulator,
            "compile_cwd": command_source.get("compile_cwd")
            or primary.get("compile_cwd"),
            "primary_top": primary_top,
            "top_modules": top_modules,
            "reported_top_modules": list(top_modules),
            "files": {"user": user_files, "filtered_count": filtered_count},
            "include_tree": _stable_merge_mapping_lists(
                eligible_results, "include_tree"
            ),
            "filelist_tree": _stable_merge_mapping_lists(
                eligible_results, "filelist_tree"
            ),
            "interfaces": list(
                dict.fromkeys(
                    str(item)
                    for result in eligible_results
                    for item in result.get("interfaces", [])
                    if item
                )
            ),
            "definitions": _stable_merge_definitions(eligible_results),
            "compile_command": command_source.get("compile_command"),
            "compile_replay_command": command_source.get(
                "compile_replay_command"
            ),
            "parse_warnings": warnings,
            "compile_evidence": {
                "schema_version": _COMPILE_EVIDENCE_SCHEMA_VERSION,
                "unit_order_source": (
                    "simulator_log"
                    if source_phases and source_order_complete
                    else "merged_incomplete"
                ),
                "ordered_compilation_units": ordered_units,
                "ordered_includes": ordered_includes,
                "filelists": filelists,
                "expanded_replay_command": (
                    source_phases[0].get("expanded_replay_command")
                    if len(source_phases) == 1
                    else None
                ),
                "source_logs": [
                    _merge_log_record(
                        path,
                        role=roles[index],
                        simulator=str(results[index].get("simulator") or "unknown"),
                    )
                    for index, path in enumerate(paths)
                ],
                "source_phases": source_phases,
                "parse_warning_records": warning_records,
                "merge_status": "conflict" if conflicts else "complete",
                "merge_conflicts": sorted(conflicts),
            },
        }
    )
    return merged


def parse_compile_log(log_path: str, simulator: str = "auto") -> dict:
    sim_type = simulator.lower()
    if sim_type == "auto":
        sim_type = detect_simulator(log_path)
    if sim_type == "vcs":
        return parse_vcs_compile_log(log_path)
    if sim_type == "xcelium":
        return parse_xcelium_compile_log(log_path)
    raise ValueError(f"Unable to determine simulator type from compile log: {log_path}")
