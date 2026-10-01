"""Recover a real Verilator HDL invocation and its bounded ordered filelists.

No shell execution, C++-to-HDL inference, vendor directory walk, or unrelated
build sidecar discovery. Unsupported/missing replay evidence stays explicit.
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from .cancellation import check_cancelled
from .filelist_tokenizer import tokenize_filelist
from .hdl_suffixes import HDL_SOURCE_SUFFIXES

MAX_BYTES = 1024 * 1024
MAX_TOKENS = 100_000
MAX_DEPTH = 16
VALUE_FLAGS = frozenset({
    '-CFLAGS', '-LDFLAGS', '-Mdir', '--Mdir', '-o', '--prefix', '--exe',
    '--trace-depth', '--trace-max-array', '--trace-max-width', '--unroll-count',
    '--unroll-stmts', '--threads', '--output-split', '--output-split-cfuncs',
    '--x-assign', '--x-initial', '--timescale', '--timescale-override', '--language',
    '--protect-key', '--mod-prefix', '--output-groups', '-j', '--build-jobs',
})
# --exe consumes following C++ operands through suffix exclusion, not one value.
VALUE_FLAGS = VALUE_FLAGS - {'--exe'}
_MAKE_CWD = re.compile(r"^make(?:\[\d+\])?: Entering directory ['`](.+)'$")
_COMMAND = re.compile(r'^(?:(?:Command|INFO: Running command):?\s+)?(?:(?:perl|python\d*)\s+)?((?:\S*/)?verilator(?:_bin)?\s+.+)$')
_SHELL = re.compile(r'^cd\s+(.+?)\s+&&\s+(.+)$')
_HDLMODE = re.compile(r'(?:^|\s)(?:--?(?:cc|sc|lint-only|xml-only|binary|E))(?:\s|$)')
_BANNER = re.compile(r'(?:V e r i l a t i o n\s+R e p o r t: Verilator|Running on Verilator version)\s+(\d+)\.(\d+)')


def bounded_sample(path: str) -> str:
    with open(path, 'rb') as handle:
        size = os.fstat(handle.fileno()).st_size
        if size <= MAX_BYTES:
            return handle.read(MAX_BYTES).decode('utf-8', errors='replace')
        head = handle.read(MAX_BYTES // 2)
        handle.seek(size - MAX_BYTES // 2)
        tail = handle.read(MAX_BYTES // 2)
    return (head + b'\n' + tail).decode('utf-8', errors='replace')


def invocation(text: str, log_dir: str) -> tuple[str | None, str]:
    cwd_stack = [log_dir]
    continuation = ''
    for raw in text.splitlines():
        check_cancelled()
        line = re.sub(r'^(?:INFO|ERROR|WARNING):cocotb:\s?', '', raw).strip()
        if (entered := _MAKE_CWD.match(line)):
            cwd_stack.append(entered[1]); continue
        if re.match(r'^make(?:\[\d+\])?: Leaving directory ', line):
            if len(cwd_stack) > 1: cwd_stack.pop()
            continue
        if continuation:
            line = continuation + ' ' + line
            continuation = ''
        if line.endswith('\\'):
            continuation = line[:-1]; continue
        cwd = cwd_stack[-1]
        shell = _SHELL.match(line)
        if shell:
            try:
                parts = shlex.split(shell[1])
            except ValueError:
                continue
            if len(parts) != 1: continue
            cwd = str((Path(log_dir) / parts[0]).resolve())
            line = shell[2]
        # cocotb runner announces an explicit execution directory after command.
        if line.startswith('INFO: Running command '):
            line = line[len('INFO: Running command '):]
            if ' in directory ' in line:
                line, cwd = line.rsplit(' in directory ', 1)
        matched = _COMMAND.match(line)
        if matched and _HDLMODE.search(matched[1]):
            return matched[1], str(Path(cwd).resolve())
    return None, str(Path(log_dir).resolve())


def is_verilator_sample(text: str) -> bool:
    command, _ = invocation(text, '.')
    return bool(command or _BANNER.search(text) or
                re.search(r'^\[TRACEWEAVE_(?:XHEEP|OPENTITAN)\]', text, re.M))


def parse(log_path: str) -> dict:
    from .compile_log_parser import _categorize, _compilation_unit_record, _collect_user_files
    text = bounded_sample(log_path)
    command, cwd = invocation(text, str(Path(log_path).resolve().parent))
    warnings: list[str] = []
    files: dict[str, dict] = {}
    configuration_files: list[str] = []
    ordered_sources: list[str] = []
    filelists: list[dict] = []
    filelist_tree: dict[str, list[str]] = {}
    tops: list[str] = []
    incdirs: list[str] = []
    defines: list[str] = ['VERILATOR=1']
    active: set[str] = set()
    token_count = 0
    def concrete(raw, base):
        if '$' in raw:
            warnings.append('Verilator unresolved environment-dependent input: ' + raw)
        return str((Path(base) / os.path.expandvars(raw)).resolve())
    def scan(tokens, base, depth=0, parent=None):
        nonlocal token_count
        token_count += len(tokens)
        if token_count > MAX_TOKENS:
            warnings.append('Verilator filelist token limit exceeded'); return
        i = 0
        while i < len(tokens):
            check_cancelled()
            token = tokens[i]
            if token in {'-f', '-F'}:
                if i + 1 >= len(tokens):
                    warnings.append('Verilator filelist argument missing'); return
                # Both filelist operands are relative to the current operand base.
                path = concrete(tokens[i + 1], base)
                filelists.append({'path':path,'raw_path':tokens[i+1], 'parent':parent,
                                  'depth':depth+1,'mode':token})
                name = Path(path).name; filelist_tree.setdefault(name, [])
                if parent: filelist_tree.setdefault(Path(parent).name, []).append(name)
                if depth >= MAX_DEPTH or path in active:
                    warnings.append('Verilator filelist depth/cycle limit: ' + path)
                else:
                    try:
                        if Path(path).stat().st_size > MAX_BYTES:
                            raise ValueError('filelist byte limit')
                        nested = tokenize_filelist(Path(path).read_text(errors='replace'))
                    except (OSError, ValueError) as exc:
                        warnings.append('Verilator filelist unavailable: ' + path + ': ' + str(exc))
                    else:
                        active.add(path)
                        try: scan(nested, cwd if token == '-f' else str(Path(path).parent), depth+1, path)
                        finally: active.remove(path)
                i += 2; continue
            if token in {'--top-module', '-top', '--top'}:
                if i+1 < len(tokens): tops.append(tokens[i+1])
                else: warnings.append('Verilator top argument missing')
                i += 2; continue
            if token.startswith('--top-module='):
                tops.append(token.split('=',1)[1]); i += 1; continue
            if token in VALUE_FLAGS:
                if i+1 >= len(tokens): warnings.append('Verilator option argument missing: ' + token)
                i += 2; continue
            if token in {'-I', '-D', '-U', '-v', '-y'}:
                if i+1 >= len(tokens):
                    warnings.append('Verilator option argument missing: ' + token); break
                value = tokens[i+1]
                if token == '-I': incdirs.append(concrete(value, base))
                elif token == '-D': defines.append(value)
                elif token == '-v': add_source(value, base)
                elif token == '-y': warnings.append('Verilator library search requires semantic frontend: ' + value)
                i += 2; continue
            if token.startswith('+incdir+'):
                incdirs.extend(concrete(p, base) for p in token[8:].split('+') if p)
            elif token.startswith('+define+'):
                defines.extend(p for p in token[8:].split('+') if p)
            elif token.startswith('-I') and len(token)>2: incdirs.append(concrete(token[2:], base))
            elif token.startswith('-D') and len(token)>2: defines.append(token[2:])
            elif not token.startswith(('-', '+')): add_source(token, base)
            i += 1
    def add_source(raw, base):
        if Path(raw).suffix.lower() == ".vlt":
            configuration_files.append(concrete(raw, base)); return
        if Path(raw).suffix.lower() not in HDL_SOURCE_SUFFIXES: return
        path = concrete(raw, base)
        if not Path(path).is_file():
            warnings.append('Verilator source missing: ' + path); return
        ordered_sources.append(path)
        files.setdefault(path, {'type':'unknown', 'category':_categorize(path)})
    if command:
        try: tokens = shlex.split(command, comments=True)
        except ValueError as exc:
            warnings.append('Verilator command tokenization failed: ' + str(exc))
        else: scan(tokens[1:], cwd)
    else:
        warnings.append('Verilator HDL command missing; C++ build output is not HDL compile context')
    if not tops:
        warnings.append('Verilator explicit top missing; no top guessed from generated C++ files')
    tops = list(dict.fromkeys(tops))
    user, filtered = _collect_user_files(files, preserve_order=True)
    return {'simulator':'verilator','compile_cwd':cwd,'primary_top':tops[-1] if tops else None,
            'top_modules':tops[-1:] if tops else [], 'reported_top_modules':[],
            'files':{'user':user,'filtered_count':filtered}, 'include_tree':{},
            'filelist_tree':filelist_tree,'interfaces':[],
            'definitions':{'modules':{},'interfaces':{},'packages':{}},
            'compile_command':command,'parse_warnings':warnings,
            'include_dirs':list(dict.fromkeys(incdirs)), 'defines':defines,
            'compile_evidence':{'schema_version':1,'unit_order_source':'command_recovery' if files else 'unavailable',
                'ordered_compilation_units':[_compilation_unit_record(p,files) for p in ordered_sources],
                'ordered_includes':[],'filelists':filelists,'configuration_files':configuration_files,'expanded_replay_command':None}}
