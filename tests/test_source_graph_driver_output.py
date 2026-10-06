import json

import pytest

import server
from src.source_graph_backend import SourceGraphConnectivityBackend
from tests.test_dynamic_evidence import project
from tests.test_source_graph_backend import _entry
from tests.test_source_graph_driver_processes import bank_backend


def test_default_groups_keep_all_case_locations_and_query_instance():
    backend = bank_backend()
    result = backend.find_driver("bank.S", "", "", recursive=True)
    assert len(result["driver_chain"]) == 8
    assert all(len(row["statement_evidence"]) == 64 for row in result["driver_chain"])
    assert result["query_instance_path"] == "bank"
    assert result["resolved_instance_path"] == "bank.u0"
    assert result["traversal"]["returned_fact_count"] == 8
    assert result["traversal"]["retained_evidence_count"] == 512
    for row in result["driver_chain"]:
        assert row["source_line"] == row["statement_evidence"][0]["source_line"]
        assert [e["source_line"] for e in row["statement_evidence"]] == list(range(3, 67))
    from src.driver_evidence import expand_driver_evidence
    from src.schemas import ExplainDriverResult
    public = {k: v for k, v in result.items() if not k.startswith("_")}
    restored = ExplainDriverResult.model_validate(public).model_dump(mode="json", exclude_none=True)
    expanded = expand_driver_evidence(restored["driver_chain"])
    query = backend._entry.query_engine.query_driver("bank.S")
    assert len(expanded) == len(query.matches) == 512
    for row, match in zip(expanded, query.matches):
        assert row["source_line"] == match.evidence.location.line
        assert row["source_column"] == match.evidence.location.column
        assert row["statement_semantics"]["covered_signal"]["bits"] == list(match.covered_signal.bits)
        assert row["statement_semantics"]["target"]["bits"] == list(match.target.bits)
        assert row["resolved_instance_path"] == match.instance_path


def test_group_roundtrip_preserves_all_fields_order_and_different_semantics():
    from src.driver_evidence import group_driver_evidence, expand_driver_evidence
    base = dict(structural_driver_id="p", source_file="a.sv", source_line=10,
                source_column=2, guard="a", dependencies=["data"], confidence="exact",
                target_bits=[0,2], source_path="t.a[1:0]")
    rows = [base, {**base, "guard":"!a", "source_line":11},
            {**base, "source_line":12}, {**base, "source_column":30},
            {**base, "dependencies":["other"]}, {**base, "confidence":"partial"},
            {**base, "target_bits":[1,2]}, {**base, "source_path":"t.a[0:1]"},
            {**base, "structural_driver_id":"other-process"}]
    before = json.loads(json.dumps(rows))
    grouped = group_driver_evidence(rows)
    assert len(grouped) == 7
    assert expand_driver_evidence(grouped) == before
    assert rows == before


def test_guards_and_dependencies_stay_distinct_and_dynamic_branches_survive():
    source = "module t(input a,b,c, output logic q); always_comb if(a) q=b; else q=c; endmodule"
    b = SourceGraphConnectivityBackend(_entry(project(source)._entry.ir))
    r = b.find_driver("t.q", "", "", recursive=True)
    assert len(r["driver_chain"]) == 2
    semantics = [row["statement_semantics"] for row in r["driver_chain"]]
    assert {s["guard"] for s in semantics} == {"a", "!(a)"}
    assert len(b.get_dynamic_step("t.q")["branches"]) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("keyword", ["clk", ["clk", "missing"]])
async def test_vcd_null_direction_hint_for_single_and_batch(keyword):
    from pathlib import Path
    path = Path(__file__).parent / "fixtures/cycle_test.vcd"
    result = await server._dispatch("search_signals", {"wave_path":str(path), "keyword":keyword})
    entries = result.batch if isinstance(keyword,list) else [result]
    for entry in entries:
        assert "VCD" in entry.hint and "direction" in entry.hint
        assert "unavailable" in entry.hint
        assert all(row.get("direction") is None for row in entry.results)
