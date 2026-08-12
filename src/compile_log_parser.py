"""
compile_log_parser.py
Extract user files, filelist relationships, include relationships, and top information
from compile and elaborate logs.
"""

import os
import re
import shlex
from pathlib import Path


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
_VCS_MODULE_RE = re.compile(r"recompiling module (\w+)", re.IGNORECASE)
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
_SOURCE_SUFFIXES = (".v", ".sv", ".vh", ".svh")
_VCS_FILELIST_MAX_DEPTH = 16
_VCS_FILELIST_MAX_TOKENS = 100_000
_SIMULATOR_DETECT_MAX_LINES = 1_000
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


def _is_eda_lib(path: str) -> bool:
    normalized = path.replace("\\", "/")
    for prefix in EDA_LIB_PREFIXES:
        expanded = os.path.expandvars(prefix).replace("\\", "/")
        if normalized.startswith(expanded):
            return True
    return False


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


def parse_vcs_compile_log(log_path: str) -> dict:
    with open(log_path, "r", errors="replace") as f:
        lines = f.readlines()

    log_dir = os.path.dirname(log_path)
    compile_command, command_dir = _extract_vcs_invocation(lines, log_dir)
    parse_warnings: list[str] = []
    command_tokens = _tokenize_vcs_text(
        compile_command or "", "VCS Command", parse_warnings
    )
    incdirs = _extract_vcs_incdirs(command_tokens, command_dir)
    filelist_tree = _direct_vcs_filelist_tree(command_tokens, command_dir)
    recovered_files: dict[str, dict] = {}
    if command_tokens:
        recovered_files, recovered_tree, recovered_incdirs = (
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
    reported_top_modules: list[str] = []
    stack: list[str] = []
    in_top_section = False

    for line in lines:
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
            path = _normalize_path(match.group(1), command_dir)
            stack = [path]
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
            file_info.setdefault(child, {"type": "unknown"})
            stack.append(child)
            continue

        match = _VCS_BACK_RE.search(line)
        if match:
            target = _normalize_path(match.group(1), command_dir)
            while stack and stack[-1] != target:
                stack.pop()
            continue

        match = _VCS_IF_RE.search(line)
        if match:
            interfaces.add(match.group(1))
            continue

        _VCS_MODULE_RE.search(line)

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
        "compile_command": compile_command,
        "parse_warnings": parse_warnings,
    }


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
) -> tuple[dict[str, dict], dict[str, list[str]], list[str]]:
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
    return file_info, filelist_tree, state["incdirs"]


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
        if index == 0 and Path(token).name in {"vcs", "vlogan"}:
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
        children = filelist_tree.setdefault(parent_filelist, [])
        if name not in children:
            children.append(name)

    if filelist_path in state["active"]:
        warnings.append(f"VCS filelist cycle ignored: {filelist_path}")
        return
    if filelist_path in state["visited"]:
        return
    if depth > _VCS_FILELIST_MAX_DEPTH:
        warnings.append(f"VCS filelist depth limit exceeded: {filelist_path}")
        return
    if not os.path.isfile(filelist_path):
        warnings.append(f"VCS filelist missing: {filelist_path}")
        return

    try:
        text = Path(filelist_path).read_text(errors="replace")
    except OSError as exc:
        warnings.append(f"VCS filelist unreadable: {filelist_path}: {exc}")
        return

    # Backslash continuation is syntax, not shell execution. Ignore full-line
    # // comments before shlex handles # comments and quoted paths.
    logical_text = text.replace("\\\r\n", " ").replace("\\\n", " ")
    logical_text = "\n".join(
        line for line in logical_text.splitlines() if not line.lstrip().startswith("//")
    )
    tokens = _tokenize_vcs_text(logical_text, f"VCS filelist {filelist_path}", warnings)
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
            parent_filelist=name,
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
        warnings.append(f"VCS source missing: {path}")
        return
    file_info.setdefault(path, {"type": "unknown"})


def parse_xcelium_compile_log(log_path: str) -> dict:
    with open(log_path, "r", errors="replace") as f:
        lines = f.readlines()

    log_dir = os.path.dirname(log_path)
    compile_command, replay_command, command_dir = _extract_xcelium_invocation(
        lines, log_dir
    )
    file_info: dict[str, dict] = {}
    include_tree: dict[str, list[str]] = {}
    filelist_tree: dict[str, list[str]] = {}
    interfaces: set[str] = set()
    top_modules: list[str] = []
    command_started = False
    current_file: str | None = None
    filelist_stack: list[tuple[int, str]] = []

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

    for line in lines:
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

        if any(stripped.endswith(ext) for ext in (".sv", ".svh", ".v", ".vh")):
            path = _normalize_path(stripped, command_dir)
            file_info.setdefault(path, {"type": "unknown"})

    for line in lines:
        match = _XCE_FILE_RE.match(line)
        if match:
            current_file = _normalize_path(match.group(1), command_dir)
            file_info.setdefault(current_file, {"type": "unknown"})
            continue
        match = _XCE_ENTITY_RE.match(line)
        if match and current_file:
            entity_type, entity_name = match.group(1).lower(), match.group(2)
            file_info[current_file]["type"] = entity_type
            if entity_type == "interface":
                interfaces.add(entity_name)

    user, filtered_count = _collect_user_files(file_info)
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
        "compile_command": compile_command,
        # Native xrun logs indent expanded command-file contents beneath the
        # top-level ``-f`` argument.  ``compile_command`` intentionally keeps
        # that historical flattened view; the replay form removes only those
        # deeper, echoed lines so an on-demand frontend does not expand the
        # same command file twice.
        "compile_replay_command": replay_command,
    }


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


def parse_compile_log(log_path: str, simulator: str = "auto") -> dict:
    sim_type = simulator.lower()
    if sim_type == "auto":
        sim_type = detect_simulator(log_path)
    if sim_type == "vcs":
        return parse_vcs_compile_log(log_path)
    if sim_type == "xcelium":
        return parse_xcelium_compile_log(log_path)
    raise ValueError(f"Unable to determine simulator type from compile log: {log_path}")
