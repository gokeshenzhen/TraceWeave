"""Shared HDL suffix capabilities used by compile and Source Graph flows.

Suffixes are only a dispatch hint for files that the compile command,
filelists, or simulator log already named.  Callers must not use these sets to
enumerate directories or guess additional project inputs.
"""

from __future__ import annotations

from pathlib import Path


VERILOG_SOURCE_SUFFIXES = frozenset({".v", ".vh"})
SYSTEMVERILOG_SOURCE_SUFFIXES = frozenset(
    {".sv", ".svh", ".svi", ".sva", ".svl"}
)
PROTECTED_SYSTEMVERILOG_SOURCE_SUFFIXES = frozenset({".svp"})
FRONTEND_HDL_SUFFIXES = frozenset(
    VERILOG_SOURCE_SUFFIXES
    | SYSTEMVERILOG_SOURCE_SUFFIXES
    | PROTECTED_SYSTEMVERILOG_SOURCE_SUFFIXES
)
TEXT_SCAN_HDL_SUFFIXES = frozenset(
    VERILOG_SOURCE_SUFFIXES | SYSTEMVERILOG_SOURCE_SUFFIXES
)
VHDL_SOURCE_SUFFIXES = frozenset({".vhd", ".vhdl"})
HDL_SOURCE_SUFFIXES = frozenset(FRONTEND_HDL_SUFFIXES | VHDL_SOURCE_SUFFIXES)


def suffix_of(path: str | Path) -> str:
    return Path(path).suffix.lower()


def is_frontend_hdl_path(path: str | Path) -> bool:
    return suffix_of(path) in FRONTEND_HDL_SUFFIXES


def is_text_scan_hdl_path(path: str | Path) -> bool:
    return suffix_of(path) in TEXT_SCAN_HDL_SUFFIXES


def is_protected_systemverilog_path(path: str | Path) -> bool:
    return suffix_of(path) in PROTECTED_SYSTEMVERILOG_SOURCE_SUFFIXES


def is_vhdl_path(path: str | Path) -> bool:
    return suffix_of(path) in VHDL_SOURCE_SUFFIXES
