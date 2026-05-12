"""
kdb_builder.py
Auto-build a Verdi KDB from the compile_result of an Xcelium / VCS log.

For Xcelium flows there is no KDB by default, so the NPI backend cannot
answer driver/load queries. TraceWeave can run::

    vericom -sv -kdb [+define+...] [+incdir+...] [-ntb_opts uvm] <files> -top T
    elabcom -lib work.lib++ -elab kdb -top T

itself, using the file list and defines parsed from the user's compile
log, and cache the resulting KDB under a project-agnostic cache directory
(``$XDG_CACHE_HOME/traceweave/kdb/<hash>/`` by default).

Everything here is derived from the *generic* compile_result shape; no
project-specific filenames, paths, or defines are baked in.

Output layout under ``<cache_root>/kdb/<hash>/``:

    kdb.elab++/        — what NPI consumes (-simflow -dbdir)
    work.lib++/        — vericom output
    build.sh           — runnable reproducer (regenerated every build)
    vericom.log        — captured stdout/stderr
    elabcom.log        — captured stdout/stderr
    state.json         — inputs, status, timestamps

On rebuild the directory is replaced atomically (build in tmp dir, rename
on success) so a stale cache never coexists with a partial new build.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

from config import (
    KDB_BUILD_TIMEOUT_SEC,
    KDB_CACHE_SUBDIR,
    TRACEWEAVE_CACHE_ROOT,
)


_KDB_ELAB_DIRNAME = "kdb.elab++"
_WORK_LIB_DIRNAME = "work.lib++"


class KdbBuildError(RuntimeError):
    """Raised when vericom or elabcom fail; carries phase + log tail."""

    def __init__(self, phase: str, returncode: int, log_tail: str):
        super().__init__(f"{phase} failed (exit {returncode}): {log_tail}")
        self.phase = phase
        self.returncode = returncode
        self.log_tail = log_tail


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_kdb(
    compile_result: dict[str, Any],
    *,
    cache_root: Path | str | None = None,
    verdi_home: str | None = None,
    top_hint: str | None = None,
    timeout_sec: int = KDB_BUILD_TIMEOUT_SEC,
    force_rebuild: bool = False,
) -> dict[str, Any]:
    """Build (or reuse cached) Verdi KDB from a parsed compile_result.

    Returns a dict with status, kdb_path, cache_dir, build_script_path,
    and log paths. Never raises for "command failed"; failures are
    surfaced in the dict so MCP callers can degrade gracefully.
    """
    verdi_home = verdi_home or os.environ.get("VERDI_HOME")
    if not verdi_home:
        return _result_failed(
            cache_dir=None,
            reason="VERDI_HOME is not set; cannot locate vericom / elabcom.",
            phase="precheck",
        )
    vericom_bin = os.path.join(verdi_home, "bin", "vericom")
    elabcom_bin = os.path.join(verdi_home, "bin", "elabcom")
    for tool in (vericom_bin, elabcom_bin):
        if not os.path.isfile(tool):
            return _result_failed(
                cache_dir=None,
                reason=f"Required Verdi tool not found: {tool}",
                phase="precheck",
            )

    inputs = _extract_build_inputs(compile_result, top_hint=top_hint)
    if "error" in inputs:
        return _result_failed(cache_dir=None, reason=inputs["error"], phase="precheck")

    root = Path(cache_root) if cache_root else TRACEWEAVE_CACHE_ROOT
    cache_dir = root / KDB_CACHE_SUBDIR / inputs["hash"]
    kdb_path = cache_dir / _KDB_ELAB_DIRNAME

    if not force_rebuild and _cache_valid(cache_dir, kdb_path):
        return {
            "status": "cached",
            "kdb_path": str(kdb_path),
            "cache_dir": str(cache_dir),
            "build_script_path": str(cache_dir / "build.sh"),
            "vericom_log": str(cache_dir / "vericom.log"),
            "elabcom_log": str(cache_dir / "elabcom.log"),
            "top": inputs["top"],
            "hash": inputs["hash"],
            "rebuilt": False,
        }

    # Build under a tmp dir so a failure leaves the existing cache (if
    # any) intact. Use the same parent so the final move is rename(2).
    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = cache_dir.parent / f".tmp-{inputs['hash']}-{os.getpid()}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=False)

    try:
        build_script = _write_build_script(
            tmp_dir,
            verdi_home=verdi_home,
            inputs=inputs,
        )
        try:
            _run_phase(
                phase="vericom",
                cmd=_vericom_cmd(vericom_bin, inputs),
                cwd=tmp_dir,
                log_path=tmp_dir / "vericom.log",
                timeout_sec=timeout_sec,
            )
            _run_phase(
                phase="elabcom",
                cmd=_elabcom_cmd(elabcom_bin, inputs),
                cwd=tmp_dir,
                log_path=tmp_dir / "elabcom.log",
                timeout_sec=timeout_sec,
            )
        except KdbBuildError as exc:
            state = {
                "status": "failed",
                "phase": exc.phase,
                "returncode": exc.returncode,
                "log_tail": exc.log_tail,
                "timestamp": _now_iso(),
                "inputs": _serialisable_inputs(inputs),
            }
            (tmp_dir / "state.json").write_text(json.dumps(state, indent=2))
            # Keep the failing tmp dir for inspection — rename to a
            # ``.failed-<hash>`` sibling so the next build can still
            # start clean.
            failed_dir = cache_dir.parent / f".failed-{inputs['hash']}"
            if failed_dir.exists():
                shutil.rmtree(failed_dir, ignore_errors=True)
            tmp_dir.rename(failed_dir)
            return _result_failed(
                cache_dir=str(failed_dir),
                reason=f"{exc.phase} failed: {exc.log_tail}",
                phase=exc.phase,
                build_script_path=str(failed_dir / "build.sh"),
                returncode=exc.returncode,
            )

        # Success: validate KDB landed where we expect.
        produced = tmp_dir / _KDB_ELAB_DIRNAME
        if not produced.is_dir():
            return _result_failed(
                cache_dir=str(tmp_dir),
                reason=(
                    f"elabcom succeeded but {_KDB_ELAB_DIRNAME} not found in "
                    f"{tmp_dir}; check elabcom.log."
                ),
                phase="postcheck",
            )

        (tmp_dir / "state.json").write_text(
            json.dumps(
                {
                    "status": "ok",
                    "timestamp": _now_iso(),
                    "inputs": _serialisable_inputs(inputs),
                },
                indent=2,
            )
        )

        if cache_dir.exists():
            shutil.rmtree(cache_dir)
        tmp_dir.rename(cache_dir)
        return {
            "status": "rebuilt",
            "kdb_path": str(cache_dir / _KDB_ELAB_DIRNAME),
            "cache_dir": str(cache_dir),
            "build_script_path": str(cache_dir / "build.sh"),
            "vericom_log": str(cache_dir / "vericom.log"),
            "elabcom_log": str(cache_dir / "elabcom.log"),
            "top": inputs["top"],
            "hash": inputs["hash"],
            "rebuilt": True,
        }
    except Exception:
        # Defensive: never leak a half-built tmp dir.
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------
# Input extraction (generic, no project hardcoding)
# ---------------------------------------------------------------------------


def _extract_build_inputs(
    compile_result: dict[str, Any],
    *,
    top_hint: str | None,
) -> dict[str, Any]:
    files = [
        f.get("path")
        for f in (compile_result.get("files") or {}).get("user") or []
        if f.get("path")
    ]
    files = [f for f in files if _is_source_file(f)]
    # De-dup while preserving order — duplicates trip elabcom.
    seen: set[str] = set()
    ordered_files: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            ordered_files.append(f)
    if not ordered_files:
        return {"error": "No source files extracted from compile_result.files.user."}

    top = top_hint or _pick_top(compile_result)
    if not top:
        return {"error": "No top module known (compile_result.top_modules empty)."}

    cmd = compile_result.get("compile_command") or ""
    defines = _extract_plus_args(cmd, "+define+")
    # Two incdir syntaxes coexist:
    #   VCS / xrun ``+incdir+<path>``  (one token)
    #   xrun       ``-incdir <path>``  (two tokens)
    incdirs = _extract_plus_args(cmd, "+incdir+") + _extract_dash_pair(cmd, "-incdir")
    # Preserve order, drop duplicates.
    seen_inc: set[str] = set()
    deduped_inc: list[str] = []
    for path in incdirs:
        if path not in seen_inc:
            seen_inc.add(path)
            deduped_inc.append(path)
    incdirs = deduped_inc
    needs_uvm = _needs_uvm(cmd, ordered_files)

    h = hashlib.sha256()
    h.update(top.encode())
    h.update(b"\0")
    for f in ordered_files:
        h.update(f.encode())
        h.update(b"\0")
        try:
            h.update(str(int(os.path.getmtime(f))).encode())
        except OSError:
            h.update(b"missing")
        h.update(b"\0")
    for d in sorted(defines):
        h.update(b"D")
        h.update(d.encode())
        h.update(b"\0")
    for i in sorted(incdirs):
        h.update(b"I")
        h.update(i.encode())
        h.update(b"\0")
    h.update(b"U1" if needs_uvm else b"U0")

    return {
        "top": top,
        "files": ordered_files,
        "defines": defines,
        "incdirs": incdirs,
        "needs_uvm": needs_uvm,
        "hash": h.hexdigest()[:16],
    }


def _is_source_file(path: str) -> bool:
    return path.lower().endswith((".v", ".sv", ".vh", ".svh"))


def _pick_top(compile_result: dict[str, Any]) -> str | None:
    tops = compile_result.get("top_modules") or []
    if not tops:
        return None
    # Prefer a top that is *not* a UVM recorder shim. UVM recorder shims
    # commonly appear as additional "top" modules in VCS logs but are
    # never the user's testbench root. The heuristic looks for "uvm"
    # in the name and prefers others when available — generic across
    # projects.
    non_recording = [t for t in tops if "uvm_custom_install" not in t.lower()]
    return non_recording[0] if non_recording else tops[0]


def _extract_plus_args(cmd: str, prefix: str) -> list[str]:
    """Pull values out of repeated ``+define+`` / ``+incdir+`` tokens.

    Compile commands sometimes pack multiple values into one token,
    e.g. ``+define+A+define+B=1+define+C``; both VCS and xrun accept
    that. Split each occurrence.
    """
    out: list[str] = []
    for tok in cmd.split():
        if not tok.startswith(prefix):
            continue
        payload = tok[len(prefix):]
        # Split on the prefix sans leading '+', then rejoin properly.
        for piece in payload.split(prefix):
            piece = piece.strip()
            if piece:
                out.append(piece)
    # Preserve first-seen order, drop dupes.
    seen: set[str] = set()
    deduped: list[str] = []
    for v in out:
        if v not in seen:
            seen.add(v)
            deduped.append(v)
    return deduped


def _extract_dash_pair(cmd: str, flag: str) -> list[str]:
    """Pull repeated ``-flag <value>`` pairs out of a command string.

    xrun uses ``-incdir <path>`` (two tokens) for include search paths,
    in contrast to the ``+incdir+<path>`` single-token form. We accept
    both so the same extractor works for VCS and Xcelium logs.
    """
    out: list[str] = []
    tokens = cmd.split()
    for i, tok in enumerate(tokens):
        if tok == flag and i + 1 < len(tokens):
            value = tokens[i + 1]
            if value and not value.startswith(("-", "+")):
                out.append(value)
    return out


def _needs_uvm(compile_command: str, files: list[str]) -> bool:
    cmd = (compile_command or "").lower()
    if "ntb_opts uvm" in cmd or "-uvm" in cmd:
        return True
    for tok in cmd.split():
        if tok.startswith("+define+") and "uvm" in tok.lower():
            return True
    # Last-resort heuristic: any source file path mentions uvm.
    return any("uvm" in f.lower() for f in files)


# ---------------------------------------------------------------------------
# Command assembly
# ---------------------------------------------------------------------------


def _vericom_cmd(vericom_bin: str, inputs: dict[str, Any]) -> list[str]:
    cmd = [vericom_bin, "-sv", "-kdb"]
    if inputs["needs_uvm"]:
        cmd += ["-ntb_opts", "uvm"]
    for d in inputs["defines"]:
        cmd.append(f"+define+{d}")
    for i in inputs["incdirs"]:
        cmd.append(f"+incdir+{i}")
    cmd.extend(inputs["files"])
    cmd += ["-top", inputs["top"]]
    return cmd


def _elabcom_cmd(elabcom_bin: str, inputs: dict[str, Any]) -> list[str]:
    # Note: elabcom warns on -ntb_opts; UVM linkage is established at
    # vericom time. Keep elabcom args minimal.
    return [elabcom_bin, "-lib", "work.lib++", "-elab", "kdb", "-top", inputs["top"]]


# ---------------------------------------------------------------------------
# Subprocess + log capture
# ---------------------------------------------------------------------------


def _run_phase(
    *,
    phase: str,
    cmd: list[str],
    cwd: Path,
    log_path: Path,
    timeout_sec: int,
) -> None:
    with open(log_path, "w") as logf:
        logf.write(f"# TraceWeave {phase} invocation\n")
        logf.write(f"# CWD: {cwd}\n")
        logf.write(f"# CMD: {' '.join(_shquote(p) for p in cmd)}\n")
        logf.write("# ----------------------------------------\n")
        logf.flush()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                stdout=logf,
                stderr=subprocess.STDOUT,
                timeout=timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            logf.write(f"\n# TIMEOUT after {timeout_sec}s\n")
            raise KdbBuildError(phase, -1, f"timeout after {timeout_sec}s") from exc
    if proc.returncode != 0:
        raise KdbBuildError(phase, proc.returncode, _tail(log_path))


def _tail(log_path: Path, lines: int = 25) -> str:
    try:
        with open(log_path) as f:
            buf = f.readlines()
    except OSError:
        return ""
    return "".join(buf[-lines:]).strip()


def _shquote(s: str) -> str:
    if not s or any(c in s for c in " \t\n\"'\\$`"):
        return "'" + s.replace("'", "'\"'\"'") + "'"
    return s


# ---------------------------------------------------------------------------
# build.sh generation
# ---------------------------------------------------------------------------


def _write_build_script(
    cache_dir: Path,
    *,
    verdi_home: str,
    inputs: dict[str, Any],
) -> Path:
    vericom_args = ["-sv", "-kdb"]
    if inputs["needs_uvm"]:
        vericom_args += ["-ntb_opts", "uvm"]
    for d in inputs["defines"]:
        vericom_args.append(f"+define+{d}")
    for i in inputs["incdirs"]:
        vericom_args.append(f"+incdir+{i}")

    lines: list[str] = []
    lines.append("#!/usr/bin/env bash")
    lines.append("# TraceWeave auto-generated KDB build script.")
    lines.append("# Regenerated on every rebuild — do not edit; changes are lost.")
    lines.append(f"# Hash:      {inputs['hash']}")
    lines.append(f"# Top:       {inputs['top']}")
    lines.append(f"# UVM:       {inputs['needs_uvm']}")
    lines.append(f"# Sources:   {len(inputs['files'])} file(s)")
    lines.append(f"# Generated: {_now_iso()}")
    lines.append("")
    lines.append("set -euo pipefail")
    lines.append(f"export VERDI_HOME={_shquote(verdi_home)}")
    lines.append('SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"')
    lines.append('cd "$SCRIPT_DIR"')
    lines.append("")
    lines.append("# Step 1: vericom — source analysis + per-file KDB")
    lines.append('"$VERDI_HOME/bin/vericom" \\')
    for a in vericom_args:
        lines.append(f"  {_shquote(a)} \\")
    for f in inputs["files"]:
        lines.append(f"  {_shquote(f)} \\")
    lines.append(f"  -top {_shquote(inputs['top'])} 2>&1 | tee vericom.log")
    lines.append("")
    lines.append("# Step 2: elabcom — elaborate to kdb.elab++")
    lines.append('"$VERDI_HOME/bin/elabcom" \\')
    lines.append("  -lib work.lib++ \\")
    lines.append("  -elab kdb \\")
    lines.append(f"  -top {_shquote(inputs['top'])} 2>&1 | tee elabcom.log")
    lines.append("")

    path = cache_dir / "build.sh"
    path.write_text("\n".join(lines))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


# ---------------------------------------------------------------------------
# Cache validation + helpers
# ---------------------------------------------------------------------------


def _cache_valid(cache_dir: Path, kdb_path: Path) -> bool:
    if not cache_dir.is_dir() or not kdb_path.is_dir():
        return False
    state_path = cache_dir / "state.json"
    if not state_path.is_file():
        return False
    try:
        state = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return state.get("status") == "ok"


def _serialisable_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    return {
        "top": inputs["top"],
        "needs_uvm": inputs["needs_uvm"],
        "defines": inputs["defines"],
        "incdirs": inputs["incdirs"],
        "files": inputs["files"],
        "hash": inputs["hash"],
    }


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _result_failed(
    *,
    cache_dir: str | None,
    reason: str,
    phase: str,
    build_script_path: str | None = None,
    returncode: int | None = None,
) -> dict[str, Any]:
    return {
        "status": "failed",
        "phase": phase,
        "reason": reason,
        "returncode": returncode,
        "cache_dir": cache_dir,
        "build_script_path": build_script_path,
        "kdb_path": None,
        "rebuilt": False,
    }
