from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.connectivity_ir import BindingSourceKind, CoverageStatus, SignalSelection
from src.slang_connectivity_projector import (
    ProjectionDiagnostic,
    ProjectionExclusion,
    ProjectionOptions,
    SlangConnectivityProjector,
    _BindingOperand,
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


@pytest.mark.parametrize(
    "symbol_kind",
    ["EnumValue", "Genvar", "Parameter", "Specparam", "TypeParameter"],
)
def test_elaboration_constants_are_not_runtime_signal_dependencies(symbol_kind):
    packed_range = SimpleNamespace(width=32, left=31, right=0)
    symbol = SimpleNamespace(
        kind=SimpleNamespace(name=symbol_kind),
        hierarchicalPath="tb.u_leaf.Width",
        type=SimpleNamespace(getBitVectorRange=lambda: packed_range),
    )
    expression = SimpleNamespace(
        kind=SimpleNamespace(name="NamedValue"),
        symbol=symbol,
    )
    record = SimpleNamespace(path="tb.u_leaf")
    projector = SlangConnectivityProjector(source_manager=object())

    assert projector._template_selection(expression, record, {}) is None


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


def test_concat_mapping_partitions_constant_signal_and_unresolved_segments():
    target = SignalSelection("data_i", tuple(range(31, -1, -1)), "top.u_leaf")
    payload = SignalSelection("payload", tuple(range(23, -1, -1)), "top")

    mappings = _map_concat_to_target(
        (
            _BindingOperand.constant(("0",) * 8),
            _BindingOperand.signal(payload),
        ),
        target,
    )

    assert mappings is not None
    assert [item.source_kind for item in mappings] == [
        BindingSourceKind.CONSTANT,
        BindingSourceKind.SIGNAL,
    ]
    assert mappings[0].target.bits == tuple(range(31, 23, -1))
    assert mappings[0].constant_bits == ("0",) * 8
    assert mappings[1].source == payload
    assert mappings[1].target.bits == tuple(range(23, -1, -1))


def test_real_frontend_projects_common_segmented_port_actuals():
    pyslang = pytest.importorskip("pyslang")
    source = """
module leaf32(input logic [31:0] data_i); endmodule
module leaf8(input logic [7:0] data_i); endmodule
module top;
  logic [23:0] payload;
  logic [1:0] pair;
  leaf32 u_concat(.data_i({8'h0, payload}));
  leaf32 u_extend(.data_i(payload));
  leaf8  u_trunc(.data_i(payload));
  leaf8  u_repeat(.data_i({4{pair}}));
endmodule
"""
    tree = pyslang.syntax.SyntaxTree.fromText(source)
    compilation = pyslang.ast.Compilation()
    compilation.addSyntaxTree(tree)
    projection = SlangConnectivityProjector(
        source_manager=tree.sourceManager,
    ).project(compilation.getRoot())
    by_instance = {binding.instance_path: binding for binding in projection.ir.bindings}

    concat = by_instance["top.u_concat"].mappings
    assert [mapping.source_kind for mapping in concat] == [
        BindingSourceKind.CONSTANT,
        BindingSourceKind.SIGNAL,
    ]
    assert concat[0].target.bits == tuple(range(31, 23, -1))
    assert concat[1].source.path(include_bits=True) == "top.payload[23:0]"

    extended = by_instance["top.u_extend"].mappings
    assert [mapping.source_kind for mapping in extended] == [
        BindingSourceKind.CONSTANT,
        BindingSourceKind.SIGNAL,
    ]
    assert extended[0].constant_bits == ("0",) * 8
    assert extended[1].source.bits == tuple(range(23, -1, -1))

    truncated = by_instance["top.u_trunc"].mappings
    assert len(truncated) == 1
    assert truncated[0].source.bits == tuple(range(7, -1, -1))

    repeated = by_instance["top.u_repeat"].mappings
    assert len(repeated) == 4
    assert all(mapping.source.bits == (1, 0) for mapping in repeated)
    assert [mapping.target.bits for mapping in repeated] == [
        (7, 6),
        (5, 4),
        (3, 2),
        (1, 0),
    ]


def test_real_frontend_binding_evidence_points_to_actual_expression():
    pyslang = pytest.importorskip("pyslang")
    source = """\
module leaf(input logic [31:0] data_i); endmodule
module top;
  logic [23:0] payload;
  leaf u_leaf (
    .data_i(
      {8'h0, payload}
    )
  );
endmodule
"""
    tree = pyslang.syntax.SyntaxTree.fromText(source)
    compilation = pyslang.ast.Compilation()
    compilation.addSyntaxTree(tree)

    projection = SlangConnectivityProjector(
        source_manager=tree.sourceManager,
    ).project(compilation.getRoot())

    binding = projection.ir.bindings[0]
    assert binding.instance_path == "top.u_leaf"
    assert binding.evidence.location.line == 6


def test_source_paths_normalize_against_projection_root(tmp_path: Path, monkeypatch):
    source = tmp_path / "rtl" / "core.sv"

    assert normalize_source_path(str(source), tmp_path) == "rtl/core.sv"
    assert normalize_source_path(str(source), None) == source.as_posix()

    worker_root = tmp_path / "traceweave" / "worker"
    external_source = tmp_path / "soc" / "rtl" / "core.sv"
    worker_root.mkdir(parents=True)
    monkeypatch.chdir(worker_root)
    frontend_name = "../../soc/rtl/core.sv"

    assert normalize_source_path(frontend_name, worker_root) == (
        external_source.resolve().as_posix()
    )
