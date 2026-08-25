import shlex

import pytest

from src.hdl_suffixes import (
    FRONTEND_HDL_SUFFIXES,
    HDL_SOURCE_SUFFIXES,
    PROTECTED_SYSTEMVERILOG_SOURCE_SUFFIXES,
    SYSTEMVERILOG_SOURCE_SUFFIXES,
    TEXT_SCAN_HDL_SUFFIXES,
    VHDL_SOURCE_SUFFIXES,
    is_frontend_hdl_path,
    is_protected_systemverilog_path,
    is_text_scan_hdl_path,
)


def test_hdl_suffix_capabilities_are_explicit_and_non_overlapping():
    assert SYSTEMVERILOG_SOURCE_SUFFIXES == frozenset(
        {".sv", ".svh", ".svi", ".sva", ".svl"}
    )
    assert PROTECTED_SYSTEMVERILOG_SOURCE_SUFFIXES == frozenset({".svp"})
    assert VHDL_SOURCE_SUFFIXES == frozenset({".vhd", ".vhdl"})
    assert PROTECTED_SYSTEMVERILOG_SOURCE_SUFFIXES <= FRONTEND_HDL_SUFFIXES
    assert PROTECTED_SYSTEMVERILOG_SOURCE_SUFFIXES.isdisjoint(
        TEXT_SCAN_HDL_SUFFIXES
    )
    assert HDL_SOURCE_SUFFIXES == FRONTEND_HDL_SUFFIXES | VHDL_SOURCE_SUFFIXES


def test_hdl_suffix_helpers_are_case_insensitive_and_fail_closed():
    assert is_frontend_hdl_path("rtl/checker.SVA")
    assert is_frontend_hdl_path("rtl/protected.SVP")
    assert is_text_scan_hdl_path("rtl/library.SVL")
    assert not is_text_scan_hdl_path("rtl/protected.SVP")
    assert is_protected_systemverilog_path("rtl/protected.SVP")
    assert not is_frontend_hdl_path("rtl/unknown.source")


def test_pinned_pyslang_accepts_supported_suffixes_as_explicit_inputs(tmp_path):
    pytest.importorskip("pyslang")
    from pyslang import driver as driver_module

    source_definitions = {
        "legacy.v": "module legacy_v; endmodule\n",
        "legacy_header.vh": "module legacy_vh; endmodule\n",
        "top.sv": "module top; endmodule\n",
        "defs.svh": "package defs_svh; endpackage\n",
        "include.svi": "package include_svi; endpackage\n",
        "assertions.sva": "module assertions_sva; endmodule\n",
        "library.svl": "package library_svl; endpackage\n",
        # Plaintext fixture verifies suffix dispatch only. Production still
        # treats every .svp input as protected and limits coverage.
        "protected.svp": "module protected_svp; endmodule\n",
    }
    paths = []
    for name, source_text in source_definitions.items():
        path = tmp_path / name
        path.write_text(source_text, encoding="utf-8")
        paths.append(str(path))

    driver = driver_module.Driver()
    driver.addStandardArgs()
    options = driver_module.CommandLineOptions()
    options.ignoreProgramName = True
    options.expandEnvVars = True
    options.supportsComments = True
    command = shlex.join(
        [
            "--compat",
            "vcs",
            "--enable-legacy-protect",
            "--single-unit",
            *paths,
        ]
    )

    assert driver.parseCommandLine(command, options)
    assert driver.processOptions()
    assert list(driver.sourceLoader.errors) == []
    assert driver.parseAllSources()
    root = driver.createCompilation().getRoot()
    assert {str(instance.name) for instance in root.topInstances} >= {
        "legacy_v",
        "legacy_vh",
        "top",
        "assertions_sva",
        "protected_svp",
    }
