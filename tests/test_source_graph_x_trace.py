from __future__ import annotations

from pathlib import Path

import pytest

import src.source_graph_adapter as source_graph_adapter
from src.hierarchy_handles import compute_snapshot_fingerprint
from src.source_graph_adapter import (
    AdapterStatus,
    build_source_graph_trace_plan,
)
from src.source_graph_contract import (
    BoundaryMode,
    CoverageBoundary,
    SourceGraphArtifactScope,
    compute_source_graph_build_key,
)
from src.source_graph_x_trace import (
    SourceGraphTraceArtifactGuard,
    SourceGraphTraceConnectivityBackend,
    SourceGraphTraceFallbackRequired,
    SourceGraphTraceScopeExpansion,
)
from src.x_trace import trace_x_source


@pytest.fixture(autouse=True)
def _clean_adapter_cache():
    source_graph_adapter._reset_source_graph_adapter_cache_for_tests()
    yield
    source_graph_adapter._reset_source_graph_adapter_cache_for_tests()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _compile_fixture(tmp_path: Path, *, tops=("tb",)) -> tuple[str, dict]:
    source = tmp_path / "top.sv"
    compile_log = tmp_path / "compile.log"
    _write(source, "module tb; endmodule\n")
    command = "xrun top.sv " + " ".join(f"-top {top}" for top in tops)
    _write(compile_log, command + "\n")
    return str(compile_log), {
        "simulator": "xcelium",
        "compile_cwd": str(tmp_path),
        "compile_command": command,
        "top_modules": list(tops),
        "files": {
            "user": [
                {
                    "path": str(source.resolve()),
                    "type": "module",
                    "category": "rtl",
                }
            ],
            "filtered_count": 0,
        },
        "include_tree": {},
        "filelist_tree": {},
        "interfaces": [],
        "parse_warnings": [],
    }


def _branch_hierarchy(children: dict | None = None) -> dict:
    return {
        "component_tree": {
            "tb": {
                "dut": {
                    "module": "dut",
                    "children": children
                    or {
                        "left": {"module": "leaf", "children": {}},
                        "right": {"module": "leaf", "children": {}},
                    },
                }
            }
        }
    }


def _trace_plan(
    tmp_path: Path,
    signal_paths: tuple[str, ...],
    *,
    hierarchy: dict | None = None,
    tops=("tb",),
):
    compile_log, compile_result = _compile_fixture(tmp_path, tops=tops)
    return build_source_graph_trace_plan(
        compile_log=compile_log,
        compile_result=compile_result,
        hierarchy_result=hierarchy or _branch_hierarchy(),
        hierarchy_snapshot_sha256=compute_snapshot_fingerprint(compile_log, "xcelium"),
        signal_paths=signal_paths,
        top_hint=None,
        max_hops=8,
        frontend_version="11.0.0",
    )


def test_trace_plan_projects_only_exact_ancestor_union_without_sibling_iteration(
    tmp_path,
):
    class NoIterationDict(dict):
        def __iter__(self):  # pragma: no cover - failure is the assertion
            raise AssertionError("trace scope enumerated siblings")

        def items(self):  # pragma: no cover - failure is the assertion
            raise AssertionError("trace scope enumerated siblings")

        def values(self):  # pragma: no cover - failure is the assertion
            raise AssertionError("trace scope enumerated siblings")

    children = NoIterationDict(
        {
            "left": {"module": "leaf", "children": {}},
            "right": {"module": "leaf", "children": {}},
            "unrelated": {"module": "other", "children": {}},
        }
    )
    plan = _trace_plan(
        tmp_path,
        ("tb.dut.left.q", "tb.dut.right.d"),
        hierarchy=_branch_hierarchy(children),
    )

    assert plan.status is AdapterStatus.READY
    assert plan.request is not None
    scope = plan.request.artifact_identity.scope
    assert scope.proved_ancestor_chains == (
        ("tb", "tb.dut", "tb.dut.left"),
        ("tb", "tb.dut", "tb.dut.right"),
    )
    assert scope.proved_lcas == ("tb.dut",)
    assert scope.projection_instance_paths == (
        "tb",
        "tb.dut",
        "tb.dut.left",
        "tb.dut.right",
    )
    assert "tb.dut.unrelated" not in scope.projection_instance_paths
    receipt = plan.receipt.to_dict()["scope"]
    assert receipt["kind"] == "multi_endpoint_trace"
    assert receipt["endpoint_count"] == 2
    assert receipt["lca_depth"] == 1


def test_trace_plan_keeps_artifact_and_query_identity_separate(tmp_path):
    compile_log, compile_result = _compile_fixture(tmp_path)
    common = {
        "compile_log": compile_log,
        "compile_result": compile_result,
        "hierarchy_result": _branch_hierarchy(),
        "hierarchy_snapshot_sha256": compute_snapshot_fingerprint(
            compile_log, "xcelium"
        ),
        "top_hint": None,
        "max_hops": 8,
        "frontend_version": "11.0.0",
    }
    root_only = build_source_graph_trace_plan(
        **common,
        signal_paths=("tb.dut.left.q",),
    )
    expanded = build_source_graph_trace_plan(
        **common,
        signal_paths=("tb.dut.left.q", "tb.dut.left.d"),
    )

    assert root_only.request is not None and expanded.request is not None
    assert (
        compute_source_graph_build_key(root_only.request).digest
        == compute_source_graph_build_key(expanded.request).digest
    )
    assert root_only.request.query_identity == expanded.request.query_identity
    assert expanded.receipt.endpoint_count == 2


def test_trace_plan_blocks_different_top_and_defers_dotted_suffix(tmp_path):
    different_top = _trace_plan(
        tmp_path / "different",
        ("tb.q", "other.q"),
        hierarchy={"component_tree": {"tb": {}, "other": {}}},
        tops=("tb", "other"),
    )
    missing = _trace_plan(
        tmp_path / "missing",
        ("tb.dut.left.q", "tb.dut.missing.d"),
    )

    assert different_top.receipt.blocker is not None
    assert different_top.receipt.blocker.code == "trace_target_top_mismatch"
    assert missing.status is AdapterStatus.READY
    assert missing.request is not None
    assert missing.receipt.endpoint_count == 2
    assert missing.request.artifact_identity.scope.projection_instance_paths == (
        "tb",
        "tb.dut",
        "tb.dut.left",
    )


def _artifact_scope(*chains: tuple[str, ...]) -> SourceGraphArtifactScope:
    union = tuple(
        sorted(
            set().union(*(set(chain) for chain in chains)),
            key=lambda path: (path.count("."), path),
        )
    )
    common = chains[0]
    for chain in chains[1:]:
        length = 0
        for left, right in zip(common, chain):
            if left != right:
                break
            length += 1
        common = common[:length]
    return SourceGraphArtifactScope(
        design="fixture",
        top="tb",
        hierarchy_snapshot_sha256="a" * 64,
        proved_ancestor_chains=chains,
        proved_lcas=(common[-1],),
        projection_instance_paths=union,
        coverage_boundary=CoverageBoundary(
            mode=BoundaryMode.EXPLICIT,
            instance_paths=union,
        ),
    )


def test_artifact_guard_requires_expansion_but_larger_scope_covers_target():
    hierarchy = _branch_hierarchy()
    smaller = SourceGraphTraceArtifactGuard(
        artifact_scope=_artifact_scope(("tb", "tb.dut", "tb.dut.left")),
        hierarchy_result=hierarchy,
    )
    larger = SourceGraphTraceArtifactGuard(
        artifact_scope=_artifact_scope(
            ("tb", "tb.dut", "tb.dut.left"),
            ("tb", "tb.dut", "tb.dut.right"),
        ),
        hierarchy_result=hierarchy,
    )

    with pytest.raises(SourceGraphTraceScopeExpansion) as caught:
        smaller.require(("tb.dut.right.q",))
    assert caught.value.signal_paths == ("tb.dut.right.q",)
    larger.require(("tb.dut.right.q",))


def test_artifact_guard_defers_dotted_suffix_but_blocks_different_top():
    guard = SourceGraphTraceArtifactGuard(
        artifact_scope=_artifact_scope(("tb", "tb.dut", "tb.dut.left")),
        hierarchy_result=_branch_hierarchy(),
    )

    guard.require(("tb.dut.missing.q",))
    with pytest.raises(SourceGraphTraceFallbackRequired) as other_top:
        guard.require(("other.dut.q",))
    assert other_top.value.code == "source_graph_trace_target_top_mismatch"


class _PreparedBackend:
    def __init__(self, response: dict):
        self.response = response

    def find_driver(self, **kwargs):
        del kwargs
        return dict(self.response)


def _driver_response(*, status: str, coverage: str, backend="source_graph") -> dict:
    if status == "found":
        driver_status = "resolved"
    elif status == "not_connected":
        driver_status = "not_connected"
    else:
        driver_status = "partial"
    return {
        "signal_path": "tb.dut.left.q",
        "wave_path": "fixture.vcd",
        "driver_status": driver_status,
        "driver_kind": "assign" if status == "found" else None,
        "upstream_signals": ["tb.dut.left.d"] if status == "found" else [],
        "backend": backend,
        "_source_graph_query_receipt": {
            "status": status,
            "coverage_status": coverage,
            "confidence": "partial" if coverage != "complete" else "exact",
            "match_count": int(status == "found"),
            "unresolved_boundary_codes": [],
            "traversed_binding_edges": 0,
            "max_depth": 8,
        },
    }


def _guarded_backend(response: dict) -> SourceGraphTraceConnectivityBackend:
    return SourceGraphTraceConnectivityBackend(
        backend=_PreparedBackend(response),
        artifact_scope=_artifact_scope(("tb", "tb.dut", "tb.dut.left")),
        hierarchy_result=_branch_hierarchy(),
        artifact_fingerprint_sha256="b" * 64,
    )


def _query(backend):
    return backend.find_driver(
        "tb.dut.left.q",
        "fixture.vcd",
        "compile.log",
        recursive=True,
        max_depth=8,
    )


def test_trace_backend_preserves_partial_positive_and_query_identity():
    backend = _guarded_backend(
        _driver_response(status="found", coverage="inconclusive")
    )

    result = _query(backend)
    receipt = backend.ledger.to_dict()

    assert result["driver_status"] == "resolved"
    assert "_source_graph_query_receipt" not in result
    assert receipt["single_artifact_provenance"] is True
    assert receipt["query_count"] == 1
    assert receipt["positive_query_count"] == 1
    assert len(receipt["query_fingerprints_sha256"][0]) == 64
    assert "tb.dut.left.q" not in str(receipt)


def test_trace_backend_accepts_namespaced_frontend_diagnostic_gap():
    response = _driver_response(status="found", coverage="inconclusive")
    response["_source_graph_query_receipt"]["unresolved_boundary_codes"] = [
        "bind_semantics",
        "frontend_diagnostic:ConcatWithStringInt",
        "frontend_diagnostic:UnknownPackage",
    ]
    backend = _guarded_backend(response)

    result = _query(backend)
    receipt = backend.ledger.to_dict()

    assert result["driver_status"] == "resolved"
    assert receipt["query_count"] == 1
    assert receipt["positive_query_count"] == 1
    assert receipt["query_gap_codes"] == [
        "bind_semantics",
        "frontend_diagnostic",
    ]


def test_trace_backend_invalid_gap_receipt_does_not_partially_mutate_ledger():
    response = _driver_response(status="found", coverage="inconclusive")
    response["_source_graph_query_receipt"]["unresolved_boundary_codes"] = [
        "unexpected:project_path"
    ]
    backend = _guarded_backend(response)

    with pytest.raises(ValueError, match="fixed query gap label"):
        _query(backend)

    assert backend.ledger.to_dict()["query_count"] == 0
    assert backend.ledger.to_dict()["query_statuses"] == []


def test_trace_backend_separates_complete_and_inconclusive_negatives():
    complete = _guarded_backend(
        _driver_response(status="not_connected", coverage="complete")
    )
    incomplete = _guarded_backend(
        _driver_response(status="not_connected", coverage="partial")
    )

    assert _query(complete)["driver_status"] == "not_connected"
    assert complete.ledger.complete_negative_query_count == 1
    with pytest.raises(SourceGraphTraceFallbackRequired) as caught:
        _query(incomplete)
    assert caught.value.code == "source_graph_trace_incomplete_negative"
    assert incomplete.ledger.inconclusive_negative_count == 1


def test_trace_backend_rejects_mixed_provenance():
    backend = _guarded_backend(
        _driver_response(status="found", coverage="complete", backend="static")
    )

    with pytest.raises(SourceGraphTraceFallbackRequired) as caught:
        _query(backend)
    assert caught.value.code == "source_graph_trace_mixed_provenance"


@pytest.mark.anyio
async def test_x_trace_scope_guard_checks_all_x_upstreams_before_child_query():
    values = {
        "tb.dut.out": {"value": {"raw": "x"}},
        "tb.dut.left": {"value": {"raw": "x"}},
        "tb.dut.right": {"value": {"raw": "x"}},
    }
    driver_calls: list[str] = []
    guarded: list[list[str]] = []

    def driver_lookup(signal_path):
        driver_calls.append(signal_path)
        return {
            "driver_status": "resolved",
            "driver_kind": "assign",
            "upstream_signals": ["left", "right"],
        }

    async def upstream_lookup(names, current, time_ps):
        del current, time_ps
        return [
            {"name": name, "path": f"tb.dut.{name}", "value": values[f"tb.dut.{name}"]}
            for name in names
        ]

    def scope_guard(paths):
        guarded.append(list(paths))
        raise SourceGraphTraceScopeExpansion(paths)

    with pytest.raises(SourceGraphTraceScopeExpansion):
        await trace_x_source(
            wave_path="fixture.vcd",
            signal_path="tb.dut.out",
            time_ps=0,
            compile_log="compile.log",
            parser=None,
            driver_lookup=driver_lookup,
            value_lookup=lambda path, time_ps: values[path],
            upstream_lookup=upstream_lookup,
            upstream_scope_guard=scope_guard,
        )

    assert driver_calls == ["tb.dut.out"]
    assert guarded == [["tb.dut.left", "tb.dut.right"]]
