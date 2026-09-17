"""Bounded dual-context divergence graph, independent of mutable session state."""

from __future__ import annotations

import asyncio
from collections import deque
import hashlib
import json

from .cancellation import OperationCancelled
from .divergence_budget import Budget, BudgetExceeded, TraceRestart
from .divergence_clock import compare_clock
from .divergence_compare import compare_signals, file_identity, known
from .divergence_context import resolve_context, driver_action
from .divergence_mapping import Mapping, selected_path, split_selection
from .divergence_routing import DynamicRoute
from .dynamic_observe import observe_step
from .schemas import TraceDivergenceInput, TraceDivergenceResult

_COMPETING = [
    "upstream_input_difference",
    "local_logic_or_control_difference",
    "reference_or_mapping_invalid",
]


def _context(side):
    return {k: v for k, v in side.items() if k not in {"wave_path", "signal_path"}}


def _design_id(bound):
    return hashlib.sha256(
        json.dumps(
            [
                bound.snapshot,
                bound.context.get("top_hint"),
                bound.hierarchy["_compile_session_snapshot"].content_fingerprint_sha256,
            ]
        ).encode()
    ).hexdigest()


def _dependencies(observation):
    facts = {}
    for dep in observation.get("dependencies", ()):
        key = (dep["signal"], tuple(dep["bits"]), dep["time_ps"], dep["phase"])
        if key not in facts:
            facts[key] = {**dep, "roles": [dep["role"]]}
        else:
            facts[key]["roles"] = list(
                dict.fromkeys([*facts[key]["roles"], dep["role"]])
            )
            facts[key]["gaps"] = list(
                dict.fromkeys([*facts[key].get("gaps", ()), *dep.get("gaps", ())])
            )
    return list(facts.values())


def _structure(step):
    keys = (
        "signal",
        "backend",
        "artifact_sha256",
        "width",
        "bits",
        "boundary",
        "complete",
        "clock",
        "gaps",
        "sources",
        "cross_check",
        "traversal",
        "claim_semantics",
        "structural",
    )
    result = {k: step[k] for k in keys if k in step}
    result["assignments"] = [
        {k: b[k] for k in ("id", "order", "source", "port_hops") if k in b}
        for b in step.get("branches", ())
    ]
    return result


async def trace_divergence(services, raw, *, route_factory=DynamicRoute):
    request = TraceDivergenceInput.model_validate(raw).model_dump(exclude_none=True)
    start = services._resolve_time(request["start_time_ps"])
    end = services._resolve_time(request["end_time_ps"], allow_sentinel=True)
    if start < 0 or end < -1 or end >= 0 and end < start:
        raise ValueError("comparison_window_invalid")
    policy = request["comparison"]
    if policy["mode"] == "clock" and not (
        policy.get("clock_a") and policy.get("clock_b")
    ):
        raise ValueError("clock_a_and_clock_b_required")
    if policy["mode"] == "event" and (
        policy.get("clock_a") or policy.get("clock_b") or policy["sample_offset_ps"]
    ):
        raise ValueError("clock_policy_requires_clock_mode")
    sides = {s: request[f"side_{s}"] for s in ("a", "b")}
    budget = Budget(request["limits"])
    result = dict(
        status="blocked",
        reference_side=request["reference_side"],
        comparison={},
        contexts={},
        nodes=[],
        edges=[],
        findings=[],
        frontier=[],
        coverage={
            "checks": [],
            "gaps": [],
            "limits": request["limits"],
            "truncated": False,
        },
        next_actions=[],
        cursor=None,
        operation_metrics={},
    )
    wave_ids = {s: file_identity(side["wave_path"]) for s, side in sides.items()}
    bounds = {}
    routes = {}
    comparison = None

    def frontier(node, reason, **facts):
        row = dict(node_id=node, reason=reason, **facts)
        if reason == "budget_exhausted":
            result["coverage"]["truncated"] = True
        if row not in result["frontier"]:
            result["frontier"].append(row)
        if reason not in {"input_boundary", "testbench_driven", "constant_source"}:
            if reason not in result["coverage"]["gaps"]:
                result["coverage"]["gaps"].append(reason)

    async def wave(fn):
        budget.cache_bytes = 0
        try:
            return await budget.run(
                lambda: services._run_in_wave_thread(
                    [s["wave_path"] for s in sides.values()], fn
                )
            )
        finally:
            budget.cache_bytes = 0

    try:
        # Mapping syntax is validated even before a root diff can early-return.
        Mapping(
            request["signal_pairs"],
            request["scope_pairs"],
            sides["a"]["signal_path"],
            sides["b"]["signal_path"],
            False,
        )
        for label, side in sides.items():
            bound, reason = await budget.run(
                lambda side=side: services._run_in_cancellable_thread(
                    lambda: resolve_context(_context(side), services._handle_store)
                )
            )
            bounds[label] = bound
            result["contexts"][label] = dict(
                compile_context=bound.context if bound else _context(side),
                wave_path=side["wave_path"],
                waveform_identity=wave_ids[label],
                status=reason,
                binding_basis="caller_supplied",
                waveform_compile_version_relation="not_independently_proven",
            )
            if bound is None:
                action_result = {
                    f"wave_path_{label}": side["wave_path"],
                    f"signal_{label}": side["signal_path"],
                    "first_divergence_time_ps": None,
                }
                result["next_actions"].append(
                    driver_action(
                        result=action_result,
                        side=label,
                        context=_context(side),
                        reason=reason,
                    )
                )
                frontier(None, reason, side=label)
        if any(bound is None for bound in bounds.values()):
            return _finish(result, budget)
        ids = {s: _design_id(bound) for s, bound in bounds.items()}
        for label in sides:
            result["contexts"][label]["design_identity_sha256"] = ids[label]
        mapping = Mapping(
            request["signal_pairs"],
            request["scope_pairs"],
            sides["a"]["signal_path"],
            sides["b"]["signal_path"],
            ids["a"] == ids["b"],
        )

        def compare():
            if policy["mode"] == "clock":
                return compare_clock(
                    get_parser=services._get_parser,
                    side_a=sides["a"],
                    side_b=sides["b"],
                    start=start,
                    end=end,
                    policy=policy,
                    consume=budget.consume,
                )
            return {
                **compare_signals(
                    get_parser=services._get_parser,
                    wave_path_a=sides["a"]["wave_path"],
                    signal_a=sides["a"]["signal_path"],
                    wave_path_b=sides["b"]["wave_path"],
                    signal_b=sides["b"]["signal_path"],
                    start_ps=start,
                    end_ps=end,
                    consume=budget.consume,
                ),
                "mode": "event",
            }

        comparison = await wave(compare)
        result["comparison"] = comparison
        result["coverage"]["checks"].append("root_comparison")
        if not comparison["diverged"]:
            result["status"] = (
                "no_difference"
                if comparison["comparison_status"] == "equal"
                else "inconclusive"
            )
        else:
            routes = {
                s: route_factory(services, s, side, bounds[s], budget)
                for s, side in sides.items()
            }
            while True:
                # No edge or finding from the discarded backend/artifact survives.
                (
                    result["nodes"],
                    result["edges"],
                    result["findings"],
                    result["frontier"],
                ) = [], [], [], []
                result["coverage"]["gaps"] = []
                try:
                    await _walk(
                        result,
                        sides,
                        routes,
                        mapping,
                        ids,
                        comparison,
                        start,
                        budget,
                        wave,
                        services,
                        frontier,
                    )
                    break
                except TraceRestart as restart:
                    (
                        result["nodes"],
                        result["edges"],
                        result["findings"],
                        result["frontier"],
                    ) = [], [], [], []
                    result["coverage"]["gaps"] = []
                    budget.restart(restart.reason)
            result["status"] = "partial" if result["coverage"]["gaps"] else "complete"
        for gap in comparison.get("coverage_gaps", ()):
            if gap["reason"] != "stopped_at_difference":
                frontier(None, gap["reason"], source="root_comparison")
                if comparison["diverged"]:
                    result["status"] = "partial"
    except OperationCancelled as exc:
        raise asyncio.CancelledError from exc
    except BudgetExceeded as exc:
        result["status"] = (
            "partial" if comparison and comparison.get("diverged") else "inconclusive"
        )
        result["coverage"]["truncated"] = True
        frontier(None, "budget_exhausted", budget=exc.limit)
        for node in result["nodes"]:
            if node["status"] == "pending":
                node["status"] = "frontier"
                frontier(node["id"], "budget_exhausted", budget=exc.limit)

    # Revalidation runs after the bounded work, even after a timeout. It performs
    # only exact local stat checks and never prepares/retries another backend.
    changed = []
    for label, side in sides.items():
        if wave_ids[label] is None or wave_ids[label] != file_identity(
            side["wave_path"]
        ):
            changed.append((label, "waveform_changed"))
        if bounds.get(
            label
        ) is not None and not await services._run_in_cancellable_thread(
            bounds[label].current
        ):
            changed.append((label, "compile_context_changed"))
        if label in routes and not routes[label].current():
            changed.append((label, "compile_context_changed"))
    if changed:
        result.update(status="inconclusive", nodes=[], edges=[], findings=[])
        if comparison:
            comparison.update(
                diverged=False,
                comparison_status="inconclusive",
                earliest_difference_proven=False,
                first_divergence_time_ps=None,
                value_a=None,
                value_b=None,
            )
        for label, reason in changed:
            frontier(None, reason, side=label)
            if reason == "compile_context_changed":
                result["next_actions"].append(
                    driver_action(
                        result={
                            f"wave_path_{label}": sides[label]["wave_path"],
                            f"signal_{label}": sides[label]["signal_path"],
                            "first_divergence_time_ps": None,
                        },
                        side=label,
                        context=_context(sides[label]),
                        reason=reason,
                    )
                )
        if any(reason == "waveform_changed" for _, reason in changed):
            result["next_actions"].append(
                dict(
                    side="both",
                    tool="trace_divergence",
                    arguments=request,
                    reason="rebind_waveform_after_change",
                    status="needs_context",
                    missing_fields=[
                        f"side_{label}.wave_path"
                        for label, reason in changed
                        if reason == "waveform_changed"
                    ],
                )
            )
    for label, route in routes.items():
        route.receipt.update(
            whole_trace_restart_count=budget.restarts,
            whole_trace_restart_reasons=budget.restart_reasons,
        )
        result["contexts"][label]["backend_status"] = route.receipt
    if comparison and comparison.get("diverged"):
        from .verify_condition import _attach_cursor

        _attach_cursor(
            comparison,
            services._cursor_store,
            comparison["first_divergence_time_ps"],
            comparison["value_a"],
            comparison["value_b"],
            sides["a"]["wave_path"],
            sides["a"]["signal_path"],
            sides["b"]["wave_path"],
            sides["b"]["signal_path"],
            request.get("cursor_name"),
            request.get("cursor_note"),
        )
        result["cursor"] = comparison["cursor"]
        for label in ("a", "b"):
            result["next_actions"].append(
                driver_action(
                    result=comparison,
                    side=label,
                    context=_context(sides[label]),
                    bound=bounds[label],
                )
            )
        if any(row["reason"] == "mapping_missing" for row in result["frontier"]):
            result["next_actions"].append(
                dict(
                    side="both",
                    tool="trace_divergence",
                    arguments=request,
                    reason="provide_explicit_dependency_mapping",
                    status="needs_context",
                    missing_fields=["signal_pairs"],
                    evidence={
                        "frontier_reason": "mapping_missing",
                        "accepted_mapping_fields": ["signal_pairs", "scope_pairs"],
                    },
                )
            )
    return _finish(result, budget)


def _finish(result, budget):
    for side in ("a", "b"):
        artifacts = {
            (
                node[side]["structure"]["backend"],
                node[side]["structure"]["artifact_sha256"],
            )
            for node in result["nodes"]
            if "structure" in node[side]
        }
        if len(artifacts) > 1:
            raise RuntimeError("divergence_trace_mixed_artifact_provenance")
    result["operation_metrics"] = budget.metrics()
    result["coverage"].update(
        used_budget=budget.metrics(),
        restart_reasons=budget.restart_reasons,
        complete_scope="supported mapped dependencies within the requested history window",
    )
    return TraceDivergenceResult.model_validate(result)


async def _walk(
    result,
    sides,
    routes,
    mapping,
    ids,
    comparison,
    start,
    budget,
    wave,
    services,
    frontier,
):
    queue = deque()
    seen = {}
    edge_keys = set()
    successors = {}

    def add(pair, depth, parent=None, relation="structural_candidate", roles=()):
        key = tuple(
            (
                ids[s],
                pair[s]["signal"],
                tuple(pair[s].get("bits", ())),
                pair[s]["time_ps"],
                pair[s]["phase"],
            )
            for s in ("a", "b")
        )
        if key in seen:
            node_id = seen[key]
            pending = [node_id]
            visited = set()
            while pending:
                budget.check()
                current = pending.pop()
                if current == parent:
                    frontier(parent, "combinational_cycle", dependency_node_id=node_id)
                    break
                if current not in visited:
                    visited.add(current)
                    pending.extend(successors.get(current, ()))
        else:
            budget.node()
            node_id = f"n{len(result['nodes'])}"
            seen[key] = node_id
            node = dict(
                id=node_id, depth=depth, a=pair["a"], b=pair["b"], status="pending"
            )
            result["nodes"].append(node)
            queue.append(node)
        if parent is not None:
            edge_key = (parent, node_id, relation, tuple(roles))
            if edge_key not in edge_keys:
                edge_keys.add(edge_key)
                successors.setdefault(parent, set()).add(node_id)
                result["edges"].append(
                    dict(
                        source=parent,
                        target=node_id,
                        relation=relation,
                        roles=list(roles),
                    )
                )
        return node_id

    root = {
        s: dict(
            signal=split_selection(side["signal_path"])[0],
            query_signal=side["signal_path"],
            bits=split_selection(side["signal_path"])[1] or (),
            value=comparison[f"value_{s}"],
            time_ps=comparison["first_divergence_time_ps"],
            phase="after",
        )
        for s, side in sides.items()
    }
    add(root, 0)
    while queue:
        budget.check()
        node = queue.popleft()
        node_id = node["id"]
        if node["depth"] >= budget.limits["max_depth"]:
            node["status"] = "frontier"
            frontier(node_id, "budget_exhausted", budget="max_depth")
            continue
        steps = {}
        for label in ("a", "b"):
            fact = node[label]
            steps[label] = await routes[label].query(
                fact.get("query_signal") or selected_path(fact["signal"], fact["bits"])
            )
            fact["structure"] = _structure(steps[label])
            if not fact["bits"]:
                fact["bits"] = steps[label].get("bits", [])
        seen[
            tuple(
                (
                    ids[s],
                    node[s]["signal"],
                    tuple(node[s]["bits"]),
                    node[s]["time_ps"],
                    node[s]["phase"],
                )
                for s in ("a", "b")
            )
        ] = node_id
        if "both_sides_driver_evidence" not in result["coverage"]["checks"]:
            result["coverage"]["checks"].append("both_sides_driver_evidence")
        terminals = []
        for label, step in steps.items():
            for reason in step.get("gaps", ()):
                if reason != "testbench_driven":
                    frontier(node_id, reason, side=label)
            reason = (
                "input_boundary"
                if step["boundary"] == "input"
                else "constant_source"
                if step["boundary"] == "constant"
                else None
            )
            if "testbench_driven" in step.get("gaps", ()):
                reason = "testbench_driven"
            if reason:
                terminals.append(label)
                frontier(node_id, reason, side=label)
        if terminals:
            node["status"] = "boundary"
            result["findings"].append(
                dict(
                    kind="boundary_difference",
                    node_id=node_id,
                    checked_sides=terminals,
                    competing_explanations=_COMPETING,
                    limitations=["boundary_observation_is_not_a_root_cause_verdict"],
                )
            )
            if len(terminals) < 2:
                frontier(node_id, "boundary_pair_mismatch")
            continue

        def observe():
            return {
                label: observe_step(
                    steps[label],
                    get_parser=services._get_parser,
                    wave=sides[label]["wave_path"],
                    time=node[label]["time_ps"],
                    history_start=start,
                    phase=node[label]["phase"],
                    consume=budget.consume,
                )
                for label in ("a", "b")
            }

        observations = await wave(observe)
        for check in ("selected_data_and_control", "temporal_sampling"):
            if check not in result["coverage"]["checks"] and (
                check != "temporal_sampling"
                or any(o["trigger_time_ps"] is not None for o in observations.values())
            ):
                result["coverage"]["checks"].append(check)
        deps = {s: _dependencies(o) for s, o in observations.items()}
        for label, obs in observations.items():
            node[label]["observation"] = obs
            for reason in obs["gaps"]:
                frontier(node_id, reason, side=label)
        aligned = (
            observations["a"]["sampling_time_ps"],
            observations["a"]["sampling_phase"],
        ) == (
            observations["b"]["sampling_time_ps"],
            observations["b"]["sampling_phase"],
        )
        if not aligned:
            frontier(node_id, "clock_alignment_unproven")
            node["status"] = "frontier"
            continue
        matches = []
        used_b = set()
        mapping_complete = True
        for a in deps["a"]:
            budget.check()
            mapped = mapping.target(a["signal"], a["bits"])
            candidates = [
                (i, b)
                for i, b in enumerate(deps["b"])
                if mapped is not None and (b["signal"], tuple(b["bits"])) == mapped
            ]
            if len(candidates) != 1:
                frontier(
                    node_id,
                    "mapping_missing" if not candidates else "mapping_ambiguous",
                    side="a",
                    dependency=a,
                )
                mapping_complete = False
                continue
            i, b = candidates[0]
            if i in used_b:
                frontier(node_id, "mapping_ambiguous", side="b", dependency=b)
                mapping_complete = False
                continue
            used_b.add(i)
            matches.append((a, b))
        for i, b in enumerate(deps["b"]):
            if i not in used_b:
                frontier(node_id, "mapping_missing", side="b", dependency=b)
                mapping_complete = False
        matches.sort(
            key=lambda pair: (
                0
                if "control" in pair[0]["roles"] or "control" in pair[1]["roles"]
                else 1
            )
        )
        all_equal = mapping_complete
        expanded = 0
        for pair_index, (a, b) in enumerate(matches):
            budget.check()
            if pair_index >= budget.limits["max_branches"]:
                frontier(
                    node_id,
                    "budget_exhausted",
                    budget="max_branches",
                    remaining_dependency_count=len(matches) - pair_index,
                )
                result["coverage"]["truncated"] = True
                all_equal = False
                break
            if len(a["bits"]) != len(b["bits"]):
                frontier(node_id, "width_mismatch")
                all_equal = False
                continue
            if (
                not known(a["value"])
                or not known(b["value"])
                or a.get("gaps")
                or b.get("gaps")
            ):
                frontier(node_id, "dependency_evidence_incomplete", a=a, b=b)
                all_equal = False
                continue
            if a["value"] == b["value"]:
                continue
            all_equal = False
            roles = tuple(sorted(set(a["roles"] + b["roles"])))
            relation = "structural_candidate"
            if roles == ("data",) and all(
                observations[s]["complete"]
                and observations[s]["value"] == node[s]["value"]
                and dep["value"] == node[s]["value"]
                for s, dep in (("a", a), ("b", b))
            ):
                relation = "value_supported_propagation"
            child_id = add(
                {"a": a, "b": b}, node["depth"] + 1, node_id, relation, roles
            )
            if "control" in roles:
                result["findings"].append(
                    dict(
                        kind="control_difference",
                        node_id=child_id,
                        observed_at_node_id=node_id,
                        checked_sides=["a", "b"],
                        competing_explanations=_COMPETING,
                        limitations=[
                            "a_control_difference_is_not_by_itself_a_unique_root_cause"
                        ],
                    )
                )
            expanded += 1
        if all_equal and all(o["complete"] for o in observations.values()):
            result["findings"].append(
                dict(
                    kind="local_logic_candidate",
                    node_id=node_id,
                    checked_sides=["a", "b"],
                    evidence="mapped active data, controls and required predecessor state agree",
                    competing_explanations=_COMPETING,
                    limitations=[
                        "caller_supplied_waveform_compile_binding",
                        "mapping_does_not_prove_semantic_equivalence",
                    ],
                )
            )
        node["status"] = (
            "expanded" if expanded else "candidate" if all_equal else "frontier"
        )
