from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.connectivity_ir import CoverageStatus, SignalSelection
from src.slang_connectivity_projector import (
    ProjectionDiagnostic,
    ProjectionExclusion,
    ProjectionOptions,
    _map_concat_to_target,
    _parameterization,
    normalize_source_path,
)


ROOT = Path(__file__).resolve().parents[1]


def test_projector_import_does_not_import_optional_pyslang():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import src.slang_connectivity_projector; "
                "assert 'pyslang' not in sys.modules"
            ),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_projection_options_validate_diagnostic_receipt_and_focused_scope():
    blocking = ProjectionDiagnostic(
        code="ForceHierarchicalName",
        severity="Error",
        message="runtime force target is unresolved",
    )

    options = ProjectionOptions(
        diagnostics=(blocking,),
        diagnostic_total=29_416,
        blocking_diagnostic_total=65,
        focus_instance_paths=("tb.dut.u_core",),
        assignment_instance_paths=("tb.dut.u_core",),
    )

    assert options.diagnostic_total == 29_416
    assert options.blocking_diagnostic_total == 65

    with pytest.raises(ValueError, match="smaller than supplied"):
        ProjectionOptions(diagnostics=(blocking,), diagnostic_total=0)
    with pytest.raises(ValueError, match="require an explicit focused"):
        ProjectionOptions(assignment_instance_paths=("tb.dut.u_core",))


def test_projection_exclusion_rejects_complete_impact():
    with pytest.raises(ValueError, match="cannot be complete"):
        ProjectionExclusion(
            code="not_a_gap",
            message="invalid complete exclusion",
            impact=CoverageStatus.COMPLETE,
        )


def test_parameterization_handles_value_and_type_parameters_without_pyslang():
    value_parameter = SimpleNamespace(name="WIDTH", isValue=True, value=16)
    type_parameter = SimpleNamespace(
        name="T",
        isValue=False,
        targetType=SimpleNamespace(type="bit[1:0]"),
    )
    instance = SimpleNamespace(
        body=SimpleNamespace(parameters=(value_parameter, type_parameter))
    )

    assert _parameterization(instance) == (("WIDTH", "16"), ("T", "bit[1:0]"))


def test_concat_mapping_preserves_ordered_slice_bits():
    sources = (
        SignalSelection("upper", (7, 6, 5, 4), "top"),
        SignalSelection("lower", (3, 2, 1, 0), "top"),
    )
    target = SignalSelection("data_i", tuple(range(7, -1, -1)), "top.u_leaf")

    mappings = _map_concat_to_target(sources, target)

    assert mappings is not None
    assert mappings[0].target.bits == (7, 6, 5, 4)
    assert mappings[1].target.bits == (3, 2, 1, 0)
    assert _map_concat_to_target(sources, SignalSelection("short", (3, 2), "x")) is None


def test_source_paths_normalize_against_projection_root(tmp_path: Path):
    source = tmp_path / "rtl" / "core.sv"

    assert normalize_source_path(str(source), tmp_path) == "rtl/core.sv"
    assert normalize_source_path(str(source), None) == source.as_posix()
