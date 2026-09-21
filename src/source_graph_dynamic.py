"""Single-step dynamic projection over one prepared Source Graph artifact."""

from dataclasses import asdict, replace

from .cancellation import check_cancelled
from .connectivity_ir import CoverageStatus, EdgeKind
from .dynamic_evidence import DYNAMIC_VERSION, Expr, TRUE, expression_gaps


def query_step(backend, signal):
    from .source_graph_backend import (
        _query_claim_semantics,
        _driver_traversal_receipt,
        _query_receipt,
    )

    entry = backend._entry
    engine = entry.query_engine
    query = engine.query_driver(
        signal,
        max_depth=16,
        unprojected_instance_candidates=backend._unprojected_instance_candidates,
    )
    claims = _query_claim_semantics(query)
    target_definition = engine.definition_index[engine.instance_index[query.signal.instance_path].definition_id]
    target_range = target_definition.direct_signal_range(query.signal.symbol)
    result = dict(
        version=DYNAMIC_VERSION,
        backend="source_graph",
        signal=signal,
        width=query.signal.width,
        bits=list(query.signal.bits),
        state=asdict(Expr("signal", query.signal.width, signal=query.signal.path(),
                         bits=query.signal.bits, declared_bits=target_range.indices if target_range else ())),
        boundary="combinational",
        complete=query.coverage_status is CoverageStatus.COMPLETE
        and not query.truncated,
        branches=[],
        clock=None,
        gaps=[],
        claim_semantics=claims,
        traversal=_driver_traversal_receipt(query, claim_semantics=claims),
        _source_graph_query_receipt=_query_receipt(query, claim_semantics=claims),
    )
    if not query.matches:
        definition = engine.definition_index[
            engine.instance_index[query.signal.instance_path].definition_id
        ]
        port = definition.port(query.signal.symbol)
        if (
            port
            and port.direction.value == "input"
            and query.signal.instance_path in entry.ir.top_instances
        ):
            result["boundary"] = "input"
            if not result["complete"]:
                result["gaps"].append("driver_set_incomplete")
            return result
        # A port-only walk can end at a different declared signal without an
        # assignment match. Keep the positive binding path as one alias step.
        if len(query.frontiers) == 1:
            endpoint = query.frontiers[0]
            if endpoint.signal != query.signal and set(
                endpoint.query_target.bits
            ) == set(query.signal.bits):
                path = engine.query_path(
                    endpoint.signal.path(include_bits=True), signal
                )
                allowed = {
                    EdgeKind.PORT_BIND_INPUT,
                    EdgeKind.PORT_BIND_OUTPUT,
                    EdgeKind.PORT_BIND_INOUT,
                }
                if (
                    path.status.value == "found"
                    and path.path
                    and all(
                        h.edge_kind in allowed and h.exact_bit_mapping
                        for h in path.path
                    )
                ):
                    target_map = dict(
                        zip(endpoint.query_target.bits, endpoint.signal.bits)
                    )
                    source_definition = engine.definition_index[
                        engine.instance_index[
                            endpoint.signal.instance_path
                        ].definition_id
                    ]
                    declared = source_definition.direct_signal_range(
                        endpoint.signal.symbol
                    )
                    expr = Expr(
                        "signal",
                        query.signal.width,
                        signal=endpoint.signal.path(),
                        bits=tuple(target_map[b] for b in query.signal.bits),
                        declared_bits=declared.indices if declared else (),
                    )
                    result["branches"] = [
                        dict(
                            id="port_alias",
                            order=0,
                            guard=asdict(TRUE),
                            value=asdict(expr),
                            port_hops=[
                                dict(
                                    source=h.source.path(include_bits=True),
                                    target=h.target.path(include_bits=True),
                                    binding_id=h.edge_id,
                                )
                                for h in path.path
                            ],
                        )
                    ]
                    if not result["complete"]:
                        result["gaps"].append("driver_set_incomplete")
                    return result
        result.update(
            complete=False,
            boundary="unsupported",
            gaps=["dynamic_evidence_unavailable"],
        )
        return result
    if query.unresolved_bits or not result["complete"]:
        result["gaps"].append("driver_set_incomplete")
    processes = set()
    clocks = set()
    for match in query.matches:
        check_cancelled()
        source = {
            "file": match.evidence.location.file,
            "line": match.evidence.location.line,
        }
        if match.kind is EdgeKind.CONSTANT_DRIVER and len(query.matches) == 1:
            value = Expr(
                "const", len(match.constant_bits), value="".join(match.constant_bits)
            )
            result["branches"].append(
                dict(
                    id=match.fact_id,
                    guard=asdict(TRUE),
                    value=asdict(value),
                    order=0,
                    source=source,
                )
            )
            result["boundary"] = "constant"
            continue
        definition = engine.definition_index[
            engine.instance_index[match.instance_path].definition_id
        ]
        fact = next(
            (a for a in definition.assignments if a.assignment_id == match.fact_id),
            None,
        )
        if fact is None or fact.dynamic is None:
            result["gaps"].append("dynamic_evidence_unavailable")
            continue
        dynamic = fact.dynamic

        def bind(expr):
            declared = (
                definition.direct_signal_range(expr.signal) if expr.signal else None
            )
            return replace(
                expr,
                signal=f"{match.instance_path}.{expr.signal}" if expr.signal else None,
                declared_bits=declared.indices if declared else (),
                args=tuple(bind(a) for a in expr.args),
            )

        processes.add((match.instance_path, dynamic.process))
        result["gaps"].extend(dynamic.gaps)
        result["gaps"].extend(
            expression_gaps(dynamic.guard) + expression_gaps(dynamic.value)
        )
        if (
            set(match.covered_signal.bits) != set(query.signal.bits)
            or match.target.bits != fact.target.bits
        ):
            result["gaps"].append("dynamic_bit_mapping_unavailable")
        if dynamic.edge:
            result["boundary"] = "sequential"
            clock = bind(dynamic.clock)
            clocks.add((clock.signal, clock.bits, dynamic.edge))
            result["clock"] = {"expression": asdict(clock), "edge": dynamic.edge}
        result["branches"].append(
            dict(
                id=match.fact_id,
                guard=asdict(bind(dynamic.guard)),
                value=asdict(bind(dynamic.value)),
                order=dynamic.order,
                nonblocking=dynamic.nonblocking,
                source=source,
                port_hops=[
                    {
                        "source": h.source.path(include_bits=True),
                        "target": h.target.path(include_bits=True),
                        "binding_id": h.binding_id,
                    }
                    for h in match.traversal
                ],
            )
        )
    if len(processes) > 1:
        result["gaps"].append("multiple_driver_candidates")
    if len(clocks) > 1:
        result["gaps"].append("temporal_context_unavailable")
    result["gaps"] = list(dict.fromkeys(result["gaps"]))
    result["complete"] = result["complete"] and not result["gaps"]
    return result
