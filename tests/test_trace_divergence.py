"""Public divergence traces checked against hand-authored RTL and wave oracles."""

import asyncio
import importlib.util
from pathlib import Path
import sys

import pytest

import server
from src.cursor_store import CursorStore
from src.divergence_mapping import Mapping
from src.hierarchy_handles import HandleStore

requires_source_graph = pytest.mark.skipif(
    importlib.util.find_spec("pyslang") is None,
    reason="requires the optional source-graph extra (pyslang)",
)
RTL = """module top(input logic clk, ena, enb, input logic [7:0] a,b,
                  output logic [7:0] qa,qb);
 always_ff @(posedge clk) if(ena) qa<=a;
 always @(posedge clk) if(enb) qb<=b;
endmodule
"""


def write_wave(path, *, b="01101010", enb="1", change=3):
    path.write_text(f"""$timescale 1ps $end
$scope module top $end
$var wire 1 ! clk $end
$var wire 1 @ ena $end
$var wire 1 # enb $end
$var wire 8 $ a $end
$var wire 8 % b $end
$var wire 8 & qa $end
$var wire 8 * qb $end
$upscope $end
$enddefinitions $end
#0
0!
1@
{enb}#
b01010101 $
b01010101 %
b00000000 &
b00000000 *
#{change}
b{b} %
#5
1!
b01010101 &
b{b if enb == "1" else "00000000"} *
#10
0!
#15
1!
#20
0!
""")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(server, "_handle_store", HandleStore())
    monkeypatch.setattr(server, "_cursor_store", CursorStore())
    monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "source_graph")
    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH_PYTHON", sys.executable)
    monkeypatch.setenv("TRACEWEAVE_AUTO_KDB", "0")


async def setup_case(tmp_path, source=RTL):
    src = tmp_path / "design.sv"
    src.write_text(source)
    log = tmp_path / "compile.log"
    log.write_text(
        f"Chronologic VCS simulator\nCommand: vcs -sverilog {src} -top top\nParsing design file '{src}'\nTop Level Modules:\n       top\n"
    )
    # Real public hierarchy, including its source capture, not a fabricated handle.
    built = await server._dispatch(
        "build_tb_hierarchy", {"compile_log": str(log), "simulator": "vcs"}
    )
    assert built.build_status == "completed", built
    wave = tmp_path / "wave.vcd"
    write_wave(wave)
    common = {"compile_log": str(log), "simulator": "vcs", "wave_path": str(wave)}
    return {
        "side_a": {**common, "signal_path": "top.qa"},
        "side_b": {**common, "signal_path": "top.qb"},
        "signal_pairs": [
            {"a": "top.a", "b": "top.b"},
            {"a": "top.ena", "b": "top.enb"},
        ],
        "start_time_ps": 0,
        "end_time_ps": 20,
    }


@pytest.mark.anyio
@requires_source_graph
async def test_real_source_graph_seq_trace_reaches_changed_input(tmp_path):
    args = await setup_case(tmp_path)
    r = await server._dispatch("trace_divergence", args)
    # Bounded Source Graph projection retains its global coverage restriction.
    assert r.status == "partial", r.model_dump()
    assert r.comparison["first_divergence_time_ps"] == 5
    assert len(r.nodes) == 2 and len(r.edges) == 1
    assert r.nodes[0]["a"]["observation"]["trigger_time_ps"] == 5
    assert r.nodes[1]["a"]["signal"] == "top.a" and r.nodes[1]["b"]["signal"] == "top.b"
    assert r.nodes[1]["a"]["phase"] == "before"
    assert r.contexts["a"]["backend_status"]["actual_backend"] == "source_graph"
    assert len(server._cursor_store) == 1
    assert not any(f["kind"] == "local_logic_candidate" for f in r.findings)


@pytest.mark.anyio
@requires_source_graph
async def test_same_timestamp_tb_change_is_an_explicit_sampling_gap(tmp_path):
    args = await setup_case(tmp_path)
    write_wave(Path(args["side_a"]["wave_path"]), change=5)
    r = await server._dispatch("trace_divergence", args)
    assert r.status == "partial"
    assert "sampling_order_unresolved" in r.coverage["gaps"]
    assert not any(f["kind"] == "local_logic_candidate" for f in r.findings)


@pytest.mark.anyio
async def test_clock_mode_and_no_difference_do_not_trace_or_create_cursor(tmp_path):
    args = await setup_case(tmp_path)
    write_wave(Path(args["side_a"]["wave_path"]), b="01010101")
    args["comparison"] = {
        "mode": "clock",
        "clock_a": "top.clk",
        "clock_b": "top.clk",
        "sample_offset_ps": 1,
    }
    r = await server._dispatch("trace_divergence", args)
    assert r.status == "no_difference" and not r.nodes and r.cursor is None
    assert r.operation_metrics["backend_query_count"] == 0
    assert r.comparison["samples_compared"] == 2


@pytest.mark.anyio
async def test_missing_exact_context_reports_both_prerequisites(tmp_path):
    args = await setup_case(tmp_path)
    server._handle_store.invalidate()
    r = await server._dispatch("trace_divergence", args)
    assert r.status == "blocked" and len(r.next_actions) == 2
    assert all(a.status == "needs_prerequisite" for a in r.next_actions)
    assert r.operation_metrics["backend_query_count"] == 0


@pytest.mark.anyio
@requires_source_graph
async def test_total_node_budget_and_depth_are_honest_frontiers(tmp_path):
    args = await setup_case(tmp_path)
    args["limits"] = {"max_nodes": 1}
    r = await server._dispatch("trace_divergence", args)
    assert r.status == "partial" and len(r.nodes) == 1
    assert any(f.get("budget") == "max_nodes" for f in r.frontier)
    assert r.cursor is not None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "limit,value",
    [
        ("max_depth", 0),
        pytest.param("max_branches", 1, marks=requires_source_graph),
    ],
)
async def test_depth_and_branch_limits_report_truncation(tmp_path, limit, value):
    args = await setup_case(tmp_path)
    args["limits"] = {limit: value}
    r = await server._dispatch("trace_divergence", args)
    assert r.status == "partial" and r.coverage["truncated"]
    assert any(f.get("budget") == limit for f in r.frontier)
    if limit == "max_depth":
        assert r.operation_metrics["backend_query_count"] == 0


def test_mapping_rejects_non_bijective_and_overlapping_scopes():
    with pytest.raises(ValueError, match="ambiguous"):
        Mapping(
            [{"a": "x.a", "b": "y.b"}, {"a": "x.c", "b": "y.b"}],
            [],
            "x.q",
            "y.q",
            False,
        )
    with pytest.raises(ValueError, match="ambiguous"):
        Mapping(
            [], [{"a": "x", "b": "y"}, {"a": "x.sub", "b": "z"}], "x.q", "y.q", False
        )
    m = Mapping([], [{"a": "x.u", "b": "y.v"}], "x.q", "y.q", False)
    assert m.target("x.unrelated.q", [0]) is None
    assert m.target("x.u.d", [3, 2]) == ("y.v.d", (3, 2))


def fake_npi(*, fail_upstream=False, cancel=False, constants=False):
    from dataclasses import asdict
    from src.dynamic_evidence import Expr, unsupported_step
    from tests.test_dynamic_evidence import signal

    class Npi:
        name = "verdi_npi"
        execution_mode = "local"
        uses_external_worker = False

        def bind_compile_context(self, result):
            pass

        def get_dynamic_step(self, signal_path, **kwargs):
            if cancel:
                from src.cancellation import OperationCancelled

                raise OperationCancelled("cancel")
            bare = signal_path.split("[")[0]
            if fail_upstream and bare == "top.a":
                return unsupported_step(signal_path, self.name, "npi_load_failed")
            root = bare in {"top.qa", "top.qb"}
            side = "a" if bare == "top.qa" else "b"
            rhs = (
                Expr("const", 8, value="01010101" if side == "a" else "01101010")
                if constants
                else signal("top." + side, 8)
            )
            return {
                "version": "1.0",
                "backend": self.name,
                "signal": signal_path,
                "width": 8,
                "bits": list(range(7, -1, -1)),
                "boundary": "combinational"
                if constants
                else "sequential"
                if root
                else "input",
                "complete": True,
                "gaps": [],
                "clock": {"expression": asdict(signal("top.clk")), "edge": "posedge"},
                "branches": [
                    {
                        "id": "root",
                        "order": 0,
                        "guard": asdict(signal("top.en" + side)),
                        "value": asdict(rhs),
                    }
                ]
                if root
                else [],
            }

    return Npi()


@pytest.mark.anyio
@requires_source_graph
async def test_npi_failure_restarts_entire_graph_then_real_source_graph(
    tmp_path, monkeypatch
):
    import src.divergence_routing as routing

    args = await setup_case(tmp_path)
    monkeypatch.setattr(
        routing, "select_backend", lambda *a, **k: fake_npi(fail_upstream=True)
    )
    r = await server._dispatch("trace_divergence", args)
    assert r.operation_metrics["restart_count"] == 1
    assert len(r.nodes) == 2
    assert all(n["a"]["structure"]["backend"] == "source_graph" for n in r.nodes)
    assert all(n["b"]["structure"]["backend"] == "verdi_npi" for n in r.nodes)
    assert len(server._cursor_store) == 1


@pytest.mark.anyio
async def test_cancellation_never_changes_backend_or_registers_cursor(
    tmp_path, monkeypatch
):
    import src.divergence_routing as routing

    args = await setup_case(tmp_path)
    monkeypatch.setattr(
        routing, "select_backend", lambda *a, **k: fake_npi(cancel=True)
    )

    def unexpected(**kwargs):
        raise AssertionError("cancel must not prepare fallback")

    monkeypatch.setattr(server, "build_source_graph_trace_plan", unexpected)
    with pytest.raises(asyncio.CancelledError):
        await server._dispatch("trace_divergence", args)
    assert len(server._cursor_store) == 0


@pytest.mark.anyio
async def test_complete_same_controls_can_produce_local_candidate(
    tmp_path, monkeypatch
):
    import src.divergence_routing as routing

    args = await setup_case(tmp_path)
    monkeypatch.setattr(
        routing, "select_backend", lambda *a, **k: fake_npi(constants=True)
    )
    r = await server._dispatch("trace_divergence", args)
    assert r.status == "complete", r.model_dump()
    assert len(r.findings) == 1 and r.findings[0]["kind"] == "local_logic_candidate"
    assert r.findings[0]["checked_sides"] == ["a", "b"]
    assert "reference_or_mapping_invalid" in r.findings[0]["competing_explanations"]


@pytest.mark.anyio
async def test_missing_mapping_cannot_become_a_local_candidate(tmp_path, monkeypatch):
    import src.divergence_routing as routing

    args = await setup_case(tmp_path)
    args["signal_pairs"] = []
    monkeypatch.setattr(routing, "select_backend", lambda *a, **k: fake_npi())
    r = await server._dispatch("trace_divergence", args)
    assert r.status == "partial" and "mapping_missing" in r.coverage["gaps"]
    assert not r.findings
    action = next(a for a in r.next_actions if a.tool == "trace_divergence")
    assert action.status == "needs_context" and action.missing_fields == [
        "signal_pairs"
    ]


@pytest.mark.anyio
@requires_source_graph
async def test_hold_feedback_has_a_distinct_before_phase_node(tmp_path, monkeypatch):
    args = await setup_case(tmp_path)
    wave = Path(args["side_a"]["wave_path"])
    text = wave.read_text().replace("1@", "0@").replace("1#", "0#")
    text = text.replace("b00000000 &", "b01010101 &").replace(
        "b00000000 *", "b01101010 *"
    )
    wave.write_text(text)
    args["comparison"] = {"mode": "clock", "clock_a": "top.clk", "clock_b": "top.clk"}
    r = await server._dispatch("trace_divergence", args)
    assert len(r.nodes) == 2, r.model_dump()
    assert r.nodes[0]["a"]["observation"]["action"] == "hold"
    assert (
        r.nodes[1]["a"]["signal"] == "top.qa" and r.nodes[1]["a"]["phase"] == "before"
    )
    assert "history_window_exhausted" in r.coverage["gaps"]
    assert "combinational_cycle" not in r.coverage["gaps"]


@pytest.mark.anyio
async def test_source_rebuild_cannot_reuse_old_ready_action_tokens(tmp_path):
    from src.divergence_context import resolve_context

    args = await setup_case(tmp_path)
    ctx = {
        k: v for k, v in args["side_a"].items() if k not in {"wave_path", "signal_path"}
    }
    bound, _ = resolve_context(ctx, server._handle_store)
    source = tmp_path / "design.sv"
    source.write_text(RTL.replace("qa<=a", "qa<=b"))
    await server._dispatch("build_tb_hierarchy", ctx)
    assert (
        resolve_context(bound.context, server._handle_store)[1]
        == "compile_context_changed"
    )


@pytest.mark.anyio
async def test_wave_lock_only_covers_wave_reads(tmp_path, monkeypatch):
    import src.divergence_routing as routing

    args = await setup_case(tmp_path)
    wave = args["side_a"]["wave_path"]
    backend = fake_npi()
    query = backend.get_dynamic_step

    def observed(*a, **kw):
        assert not server._wave_locks_for([wave])[0].locked()
        return query(*a, **kw)

    backend.get_dynamic_step = observed
    monkeypatch.setattr(routing, "select_backend", lambda *a, **k: backend)
    parser = server._get_parser(wave)
    read = parser.get_transitions

    def locked(*a, **kw):
        assert server._wave_locks_for([wave])[0].locked()
        return read(*a, **kw)

    monkeypatch.setattr(parser, "get_transitions", locked)
    r = await server._dispatch("trace_divergence", args)
    assert r.status == "complete"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "limit", ["DIVERGENCE_MAX_TRANSITIONS", "DIVERGENCE_MAX_WAVE_CACHE_BYTES"]
)
async def test_internal_scan_budget_stops_before_connectivity(
    tmp_path, monkeypatch, limit
):
    import src.divergence_budget as budgets

    args = await setup_case(tmp_path)
    monkeypatch.setattr(budgets, limit, 1)
    r = await server._dispatch("trace_divergence", args)
    assert r.status == "inconclusive" and r.coverage["truncated"]
    assert r.operation_metrics["backend_query_count"] == 0 and r.cursor is None


@pytest.mark.anyio
async def test_native_deadline_is_observed_on_return(tmp_path, monkeypatch):
    import time
    import src.divergence_routing as routing

    args = await setup_case(tmp_path)
    args["limits"] = {"timeout_sec": 0.04}
    backend = fake_npi()
    query = backend.get_dynamic_step

    def slow(*a, **kw):
        time.sleep(0.08)
        return query(*a, **kw)

    backend.get_dynamic_step = slow
    monkeypatch.setattr(routing, "select_backend", lambda *a, **k: backend)
    r = await server._dispatch("trace_divergence", args)
    assert r.status == "partial" and r.operation_metrics["elapsed_ms"] >= 80
    assert any(f.get("budget") == "timeout_sec" for f in r.frontier)
    assert r.operation_metrics["restart_count"] == 0


@pytest.mark.anyio
async def test_wave_replacement_discards_graph_and_cursor(tmp_path, monkeypatch):
    import os
    import src.divergence_routing as routing

    args = await setup_case(tmp_path)
    backend = fake_npi()
    query = backend.get_dynamic_step
    changed = False

    def replace_wave(*a, **kw):
        nonlocal changed
        if not changed:
            wave = Path(args["side_a"]["wave_path"])
            replacement = wave.with_suffix(".new")
            replacement.write_text(wave.read_text())
            os.replace(replacement, wave)
            changed = True
        return query(*a, **kw)

    backend.get_dynamic_step = replace_wave
    monkeypatch.setattr(routing, "select_backend", lambda *a, **k: backend)
    r = await server._dispatch("trace_divergence", args)
    assert r.status == "inconclusive" and not r.nodes and not r.edges
    assert r.cursor is None and "waveform_changed" in r.coverage["gaps"]


@pytest.mark.anyio
async def test_clock_mismatch_does_not_fall_back_to_event_mode(tmp_path):
    args = await setup_case(tmp_path)
    second = tmp_path / "second.vcd"
    second.write_text(
        Path(args["side_b"]["wave_path"]).read_text().replace("#5\n", "#6\n")
    )
    args["side_b"]["wave_path"] = str(second)
    args["comparison"] = {"mode": "clock", "clock_a": "top.clk", "clock_b": "top.clk"}
    r = await server._dispatch("trace_divergence", args)
    assert r.status == "inconclusive" and not r.nodes
    assert any(
        g["reason"] == "clock_alignment_unproven" for g in r.comparison["coverage_gaps"]
    )
    assert r.operation_metrics["backend_query_count"] == 0


@pytest.mark.anyio
@requires_source_graph
async def test_real_scope_expansion_restarts_without_mixing_artifacts(tmp_path):
    source = """module leaf(input wire [7:0] d, output wire [7:0] q); assign q=d; endmodule
    module top(input wire [7:0] a,b,output wire [7:0] qa,qb);
      leaf left(.d(a),.q(qa)); leaf right(.d(b),.q(qb));
    endmodule"""
    args = await setup_case(tmp_path, source)
    wave = Path(args["side_a"]["wave_path"])
    wave.write_text("""$timescale 1ps $end
$scope module top $end
$var wire 8 ! a $end
$var wire 8 @ b $end
$var wire 8 ! qa $end
$var wire 8 @ qb $end
$scope module left $end
$var wire 8 ! d $end
$var wire 8 ! q $end
$upscope $end
$scope module right $end
$var wire 8 @ d $end
$var wire 8 @ q $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
b01010101 !
b01101010 @
#20
b01010101 !
""")
    args["scope_pairs"] = [{"a": "top.left", "b": "top.right"}]
    r = await server._dispatch("trace_divergence", args)
    assert r.operation_metrics["restart_count"] >= 1, r.model_dump()
    assert len(r.nodes) == 3, r.model_dump()
    assert r.nodes[-1]["a"]["signal"] == "top.a"
    for side in ("a", "b"):
        assert {n[side]["structure"]["backend"] for n in r.nodes} == {"source_graph"}
        assert len({n[side]["structure"]["artifact_sha256"] for n in r.nodes}) == 1
    assert len(server._cursor_store) == 1
