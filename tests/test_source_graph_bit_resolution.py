from dataclasses import replace

from src.connectivity_ir import CoverageGap, CoverageStatus, SignalSelection
from src.connectivity_query import ConnectivityQueryEngine
from src.source_graph_backend import SourceGraphConnectivityBackend
from tests.test_dynamic_evidence import project
from tests.test_source_graph_backend import _entry
from tests.test_source_graph_driver_processes import bank_backend


def mapped(backend, path, **limits):
    from src.source_graph_backend import _query_claim_semantics
    q = backend._entry.query_engine.query_driver(path, **limits)
    return backend._map_driver(q, wave_path="", recursive=True,
        requested_signal_path=path, claim_semantics=_query_claim_semantics(q))


def test_bank_hole_is_unknown_with_explicit_coverage_reasons():
    backend = bank_backend()
    ir = backend._entry.ir
    # Model the real run's objective exclusions explicitly. The small standalone
    # bank has no UVM context and must not inherit invented global exclusions.
    gap = CoverageGap(code="compile_context_incomplete", message="external context excluded",
                      impact=CoverageStatus.INCONCLUSIVE, scopes=("*",))
    ir = replace(ir, coverage=replace(ir.coverage, status=CoverageStatus.INCONCLUSIVE,
                                     gaps=(*ir.coverage.gaps, gap)))
    r = mapped(SourceGraphConnectivityBackend(_entry(ir)), "bank.S[5]")
    assert r["driver_status"] != "not_connected"
    p = r["bit_provenance"][0]
    assert p["resolution"] == "unknown"
    assert not p["driver_set_complete"] and p["reason_codes"]
    assert not r["claim_semantics"]["negative_claim_allowed"]


def test_supported_undriven_net_proves_no_driver():
    backend = SourceGraphConnectivityBackend(_entry(project("module t(output wire [3:0] q); endmodule")._entry.ir))
    r = mapped(backend, "t.q")
    assert r["driver_status"] == "not_connected"
    assert r["bit_provenance"][0]["resolution"] == "proved_no_driver"
    assert r["bit_provenance"][0]["driver_set_complete"]


def test_local_assignment_and_constant_are_found_without_source_path():
    backend = SourceGraphConnectivityBackend(_entry(project("module t(output wire q); assign q=1'b0; endmodule")._entry.ir))
    r = mapped(backend, "t.q")
    p = r["bit_provenance"][0]
    assert p["source_path"] is None
    assert p["resolution"] == "found" and p["driver_set_complete"]
    assert p["terminal_path"] == "t.q[0]"


def test_work_limit_cannot_prove_unvisited_bits_or_complete_writer_sets():
    r = mapped(bank_backend(), "bank.S", assignment_limit=80)
    unknown = [p for p in r["bit_provenance"] if p["resolution"] == "unknown"]
    found = [p for p in r["bit_provenance"] if p["resolution"] == "found"]
    assert unknown and found
    assert all("query_assignment_limit" in p["reason_codes"] for p in unknown)
    assert all(not p["driver_set_complete"] for p in found + unknown)


def test_scoped_unmodeled_write_and_sparse_bits_keep_exact_boundaries():
    ir = project("module t(output wire [4:0] q); assign q[3]=0; assign q[1]=0; endmodule")._entry.ir
    gap = CoverageGap(code="runtime_force_not_modeled", message="test force", impact=CoverageStatus.INCONCLUSIVE, scopes=("t.q[4]",))
    ir = replace(ir, coverage=replace(ir.coverage, status=CoverageStatus.PARTIAL, gaps=(gap,)))
    r = mapped(SourceGraphConnectivityBackend(_entry(ir)), "t.q")
    assert any(p["target_path"] == "t.q[4]" and p["resolution"] == "unknown" for p in r["bit_provenance"])
    proved = [p for p in r["bit_provenance"] if p["resolution"] == "proved_no_driver"]
    assert {bit for p in proved for bit in p["target_bits"]} == {0, 2}
    assert not any(p["target_path"] == "t.q[4:0]" for p in proved)


def test_display_limit_retains_structural_findings_and_separate_evidence_gap():
    r = mapped(bank_backend(True), "bank.S", evidence_limit=2)
    assert r["resolved_bit_count"] == 32
    assert {bit for p in r["bit_provenance"] if p["resolution"] == "found" for bit in p["target_bits"]} == set(range(1,33))
    assert any("query_evidence_limit" in p["reason_codes"] for p in r["bit_provenance"])


def test_reused_definition_keeps_unsupported_control_gap_in_each_instance():
    r = mapped(bank_backend(), "bank.S[17:20]")
    assert all(p["resolution"] == "found" for p in r["bit_provenance"])
    assert all(not p["driver_set_complete"] for p in r["bit_provenance"])
    assert all("procedural_control_shape_partial" in p["reason_codes"] for p in r["bit_provenance"])


def test_overlap_segment_keeps_exact_ascending_source_mapping():
    r = mapped(bank_backend(), "bank.S")
    overlap = [p for p in r["bit_provenance"] if p["multiple_driver"]]
    assert {tuple(p["target_bits"]) for p in overlap} == {(9,)}
    assert {p["source_path"] for p in overlap} == {"bank.u1.dout[4]", "bank.u2.dout[1]"}
    assert {p["terminal_path"] for p in overlap} == {"bank.u1.dout[4]", "bank.u2.dout[1]"}
