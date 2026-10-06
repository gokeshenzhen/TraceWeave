"""Structural writer identity is independent of branch/statement evidence."""

from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_dynamic_evidence import project
from src.connectivity_ir import ConnectivityIR
from src.connectivity_query import ConnectivityQueryEngine
from src.source_graph_backend import SourceGraphConnectivityBackend
from tests.test_source_graph_backend import _entry


FIXTURE = Path(__file__).parent / "fixtures/source_graph_drivers/case_bank.sv"


def bank_backend(corrected=False):
    source = FIXTURE.read_text()
    backend = project(source.replace("S[6:9]", "S[5:8]") if corrected else source)
    return SourceGraphConnectivityBackend(_entry(backend._entry.ir))


@pytest.mark.parametrize("corrected,counts", [(False, (31, 1, 1)), (True, (32, 0, 0))])
def test_case_bank_enumerates_all_writers_and_preserves_512_statements(corrected, counts):
    backend = bank_backend(corrected)
    query = backend._entry.query_engine.query_driver("bank.S")
    assert (len(query.resolved_bits), len(query.unresolved_bits), len(query.multi_driver_bits)) == counts
    assert len(query.matches) == 512
    assert query.structural_driver_count == 8
    assert query.inspected_assignment_count == 512
    assert not query.truncated
    assert len({(m.instance_path, m.structural_driver_id) for m in query.matches}) == 8
    assert all(len({m.evidence.location.line for m in query.matches if m.instance_path == f"bank.u{i}"}) == 64 for i in range(8))
    if not corrected:
        assert query.multi_driver_bits == (9,)
        overlap = backend._entry.query_engine.query_driver("bank.S[9]")
        assert {m.instance_path for m in overlap.matches} == {"bank.u1", "bank.u2"}


@pytest.mark.parametrize("body,count", [
    ("always_comb if (a) q=b; else q=c;", 2),
    ("always_comb begin q=0; if (a) q=b; end", 2),
    ("always_comb begin q=0; if (a) q[0]=b[0]; end", 2),
    ("always_comb case(a) 0:q=b; 1:q=c; endcase", 2),
])
def test_one_process_has_one_writer_and_all_evidence(body, count):
    backend = project(f"module t(input logic a, input logic [1:0] b,c, output logic [1:0] q); {body} endmodule")
    q = backend._entry.query_engine.query_driver("t.q")
    assert q.multi_driver_bits == ()
    assert q.structural_driver_count == 1
    assert len(q.matches) == count
    if "case" not in body:
        dynamic = backend.get_dynamic_step("t.q")
        assert len(dynamic["branches"]) == count
        assert "multiple_driver_candidates" not in dynamic["gaps"]


@pytest.mark.parametrize("body", [
    "always_comb q=a; always_comb q=b;",
    "assign q=a; assign q=b;",
    "for(genvar i=0;i<2;i++) begin:g always_comb q=a; end",
])
def test_independent_writers_still_overlap_including_same_line_and_generate(body):
    backend = project(f"module t(input a,b, output logic q); {body} endmodule")
    q = backend._entry.query_engine.query_driver("t.q")
    assert q.multi_driver_bits == (0,)
    assert q.structural_driver_count == 2
    assert len(q.matches) == 2


def test_missing_process_identity_cannot_prove_overlap_or_exclusivity():
    backend = project("module t(input a,b,output logic q); always_comb if(a) q=b; else q=0; endmodule")
    ir = backend._entry.ir
    ir = replace(ir, definitions=tuple(replace(d, assignments=tuple(replace(a, structural_driver_id=None) for a in d.assignments)) for d in ir.definitions))
    q = ConnectivityQueryEngine(ir).query_driver("t.q")
    assert q.multi_driver_bits == ()
    assert "driver_identity_unavailable" in {g.code for g in q.unresolved_boundaries}
    assert len(q.matches) == 2


def test_assignment_and_evidence_budgets_are_separate():
    engine = bank_backend()._entry.query_engine
    work = engine.query_driver("bank.S", assignment_limit=80)
    assert work.assignment_truncated and work.inspected_assignment_count == 80
    assert len(work.matches) == 80
    evidence = engine.query_driver("bank.S", evidence_limit=80)
    assert evidence.evidence_truncated and not evidence.assignment_truncated
    assert evidence.inspected_assignment_count == 512
    assert evidence.structural_driver_count == 8
    assert len(evidence.matches) == 80
    assert len(evidence.resolved_bits) == 31


def test_old_ir_is_rejected_and_writer_identity_roundtrips():
    ir = bank_backend()._entry.ir
    restored = ConnectivityIR.from_json_bytes(ir.to_json_bytes())
    assert restored == ir
    assert all(a.structural_driver_id for d in restored.definitions for a in d.assignments)
    payload = ir.to_dict()
    payload["ir_version"] = "1.4"
    with pytest.raises(ValueError, match="unsupported connectivity IR version"):
        ConnectivityIR.from_dict(payload)


def test_more_than_256_dynamic_branches_keep_order_guards_and_dependencies():
    body = "q=0;\n" + "\n".join(f"if (sel == 9'd{i}) q=data ^ 9'd{i};" for i in range(300))
    backend = project(f"module t(input logic [8:0] sel,data,output logic [8:0] q); always_comb begin {body} end endmodule")
    query = backend._entry.query_engine.query_driver("t.q")
    assert len(query.matches) == 301 and query.structural_driver_count == 1
    assert {d.source.symbol for m in query.matches for d in m.dependencies} == {"sel", "data"}
    dynamic = backend.get_dynamic_step("t.q")
    assert dynamic["complete"]
    assert sorted(b["order"] for b in dynamic["branches"]) == list(range(301))
    assert len({b["id"] for b in dynamic["branches"]}) == 301
