"""Per-side NPI -> exact Source Graph -> Static route for a whole evidence graph.

Uses the same selection, runtime, artifact guard and execution helpers as the
public connectivity tools. A route/artifact change aborts the current graph.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from config import get_source_graph_execution_config
from .cancellation import OperationCancelled
from .connectivity_backend import (
    DeferredConnectivityFallbackBackend,
    StaticConnectivityBackend,
    select_backend,
)
from .divergence_budget import TraceRestart, BudgetExceeded
from .kdb_identity import kdb_identity
from .dynamic_evidence import unsupported_step
from .source_graph_adapter import AdapterStatus
from .source_graph_runtime import PrepareStatus
from .source_graph_x_trace import (
    SourceGraphTraceArtifactGuard,
    SourceGraphTraceScopeExpansion,
    SourceGraphTraceFallbackRequired,
)


class DynamicRoute:
    def __init__(self, services, label, side, bound, budget):
        self.s, self.label, self.side, self.bound, self.budget = (
            services,
            label,
            side,
            bound,
            budget,
        )
        self.backend = None
        self.stage = None
        self.guard = None
        self.config = get_source_graph_execution_config()
        self.targets = [side["signal_path"]]
        self.attempts = []
        self.receipt = {}
        self.artifact = None
        self.kdb_identity = None
        self.fallback_reason = None
        self.execution = {}
        self.probe = {}
        self.source_receipt = None
        self.selected_backend = "source_graph"
        self.retain_async_controls = False

    async def initialize(self):
        self.probe = await self.budget.run(
            lambda: self.s._run_in_cancellable_thread(
                lambda: self.s.probe_verdi_backend(
                    self.bound.compile_result,
                    compile_log_path=self.bound.context["compile_log"],
                )
            )
        )
        try:
            self.backend = select_backend(
                self.probe, fallback=DeferredConnectivityFallbackBackend()
            )
        except OperationCancelled:
            raise
        except Exception:
            self.backend = None
        if getattr(self.backend, "name", None) == "verdi_npi":
            self.backend.bind_compile_context(self.bound.compile_result)
            self.stage = "verdi_npi"
            self.selected_backend = "verdi_npi"
            kdb = self.probe.get("kdb_path")
            self.kdb_identity = kdb_identity(kdb) if kdb else None
            self.artifact = hashlib.sha256(
                json.dumps(
                    [self.bound.snapshot, self.side.get("top_hint"), self.kdb_identity]
                ).encode()
            ).hexdigest()
        else:
            reason = (
                "npi_skipped_by_policy"
                if self.probe.get("connectivity_route") == "source_graph"
                else "npi_kdb_unavailable"
            )
            self.attempts.append(
                dict(backend="verdi_npi", status="unavailable", reason=reason)
            )
            self.fallback_reason = reason
            self.stage = "source_graph"

    async def _prepare_source(self):
        reason = None
        if not self.config.enabled or not self.config.valid:
            reason = (
                "source_graph_disabled"
                if not self.config.enabled
                else "source_graph_config_invalid"
            )
        else:
            try:
                plan = await self.budget.run(
                    lambda: self.s._run_in_cancellable_thread(
                        lambda: self.s.build_source_graph_trace_plan(
                            compile_log=self.bound.context["compile_log"],
                            compile_result=self.bound.compile_result,
                            hierarchy_result=self.bound.hierarchy,
                            hierarchy_snapshot_sha256=self.bound.snapshot,
                            signal_paths=tuple(self.targets),
                            top_hint=self.side.get("top_hint"),
                            max_hops=self.budget.limits["max_depth"],
                            frontend_version=self.config.frontend_version,
                            runtime_plusarg_allowlist=self.config.runtime_plusarg_allowlist,
                        )
                    )
                )
                if plan.status is AdapterStatus.BLOCKED:
                    reason = "source_graph_" + plan.receipt.blocker.code
                else:
                    runtime = self.s.get_source_graph_runtime(self.config)
                    outcome = await self.budget.run(
                        lambda: runtime.prepare(
                            plan.request,
                            timeout_seconds=min(
                                self.config.timeout_sec, self.budget.remaining
                            ),
                        )
                    )
                    if hasattr(self.budget, "record_preparation"):
                        self.budget.record_preparation(outcome.metrics)
                    self.source_receipt = self.s._source_graph_receipt_from_prepare(
                        plan, outcome, adapter_wall_ms=0
                    )
                    if outcome.status is PrepareStatus.CANCELLED:
                        raise asyncio.CancelledError
                    if outcome.status is PrepareStatus.READY:
                        self.backend = self.s._source_graph_backend_for_plan(
                            outcome.entry, plan
                        )
                        self.guard = SourceGraphTraceArtifactGuard(
                            artifact_scope=outcome.entry.artifact_scope_receipt.scope,
                            hierarchy_result=self.bound.hierarchy,
                        )
                        self.artifact = outcome.entry.build_key.digest
                        return
                    reason = self.s._SOURCE_GRAPH_PREPARE_REASONS.get(
                        outcome.status, "source_graph_prepare_failed"
                    )
            except (OperationCancelled, BudgetExceeded):
                raise
            except Exception:
                reason = "source_graph_prepare_failed"
        self.attempts.append(
            dict(backend="source_graph", status="unavailable", reason=reason)
        )
        self._static(reason)

    def _static(self, reason):
        self.stage = "static"
        self.backend = StaticConnectivityBackend()
        self.backend.bind_compile_context(self.bound.compile_result)
        self.artifact = self.bound.snapshot
        self.fallback_reason = reason

    def _expand(self, paths):
        new = [p for p in paths if p not in self.targets]
        if not new:
            self._static("source_graph_scope_expansion_stalled")
        elif len(self.targets) + len(new) > self.config.frontier_max_instances:
            raise BudgetExceeded("source_graph_frontier_instances")
        else:
            self.targets.extend(new)
            self.backend = None
            self.guard = None
        raise TraceRestart(self.label, "source_graph_scope_expansion")

    async def query(self, signal):
        self.budget.check()
        if self.stage is None:
            await self.initialize()
        if self.stage == "source_graph" and self.guard is None:
            await self._prepare_source()
        self.budget.queries += 1
        if self.stage == "static":
            structural = await self.budget.run(
                lambda: self.s._call_public_connectivity_operation(
                    self.backend,
                    operation="driver",
                    args={**self.side, "signal_path": signal, "recursive": False},
                    simulator=self.bound.context["simulator"],
                )
            )
            result = unsupported_step(
                signal, "static", "dynamic_evidence_unavailable", structural=structural
            )
        else:
            try:
                if self.guard:
                    await self.budget.run(
                        lambda: self.s._run_in_cancellable_thread(
                            lambda: self.guard.require((signal,))
                        )
                    )

                def query():
                    return self.backend.get_dynamic_step(
                        signal_path=signal,
                        compile_log=self.bound.context["compile_log"],
                        top_hint=self.side.get("top_hint"),
                        simulator=self.bound.context["simulator"],
                    )

                if self.stage == "verdi_npi":
                    result = await self.budget.run(
                        lambda: self.s._call_connectivity_backend(self.backend, query)
                    )
                else:
                    result = await self.budget.run(
                        lambda: self.s._run_in_cancellable_thread(query)
                    )
            except SourceGraphTraceScopeExpansion as exc:
                self._expand(exc.signal_paths)
            except (OperationCancelled, BudgetExceeded):
                raise
            except Exception:
                result = unsupported_step(
                    signal, self.stage, "dynamic_backend_query_failed"
                )
            self.execution.update(result.pop("_npi_execution_status", {}) or {})
            if self.stage == "verdi_npi":
                gaps = set(result.get("gaps", ()))
                terminal = {
                    "testbench_driven",
                    "multiple_driver_candidates",
                    "dynamic_expression_limit",
                }
                if (self.retain_async_controls and result.get('async_controls')
                    and result.get('clock') and result.get('boundary') == 'sequential'
                    and gaps <= {'temporal_context_unavailable', 'async_control_value_unmodeled', 'driver_set_incomplete'}):
                    terminal.add('async_control_value_unmodeled')
                if not result.get("complete") and not (gaps & terminal):
                    reason = next(
                        iter(sorted(gaps)), "npi_dynamic_evidence_unavailable"
                    )
                    self.attempts.append(
                        dict(backend="verdi_npi", status="inconclusive", reason=reason)
                    )
                    self.fallback_reason = reason
                    self.stage = "source_graph"
                    self.backend = None
                    self.guard = None
                    raise TraceRestart(self.label, "npi_dynamic_fallback")
            else:
                query_receipt = result.pop("_source_graph_query_receipt", {})
                frontiers = query_receipt.get("expansion_frontiers", ())
                try:
                    if (
                        frontiers
                        and not result.get("complete")
                        and not self.s._query_gap_blocks_scope_expansion(
                            set(query_receipt.get("unresolved_boundary_codes", ()))
                        )
                    ):
                        await self.budget.run(
                            lambda: self.s._run_in_cancellable_thread(
                                lambda: self.guard.require_frontier_children(frontiers)
                            )
                        )
                except SourceGraphTraceScopeExpansion as exc:
                    self._expand(exc.signal_paths)
                except SourceGraphTraceFallbackRequired:
                    pass
                # Keep supported positive statements even with honest objective exclusions.
                if not result.get("branches") and result.get("boundary") not in {
                    "input",
                    "constant",
                }:
                    self.attempts.append(
                        dict(
                            backend="source_graph",
                            status="inconclusive",
                            reason="source_graph_dynamic_evidence_unavailable",
                        )
                    )
                    self._static("source_graph_dynamic_evidence_unavailable")
                    raise TraceRestart(self.label, "source_graph_dynamic_fallback")
        if result.get("backend") != self.stage:
            raise ValueError("dynamic_backend_provenance_invalid")
        result["artifact_sha256"] = self.artifact
        result.pop("_source_graph_query_receipt", None)
        self.receipt = {
            "actual_backend": self.stage,
            "selected_backend": self.selected_backend,
            "attempted_backends": [
                *self.attempts,
                (dict(backend=self.stage, status='inconclusive', coverage_status='partial',
                      reason='async_control_value_unmodeled')
                 if self.retain_async_controls and result.get('async_controls')
                 else dict(backend=self.stage, status="success")),
            ],
            "fallback_reason": self.fallback_reason,
            "single_backend_provenance": True,
            "artifact_sha256": self.artifact,
            "execution_mode": getattr(self.backend, "execution_mode", "local"),
            "connectivity_route": self.probe.get("connectivity_route", "auto"),
            "source_graph": self.source_receipt,
            **self.execution,
        }

        return result

    def current(self):
        if self.stage == "verdi_npi" and self.kdb_identity is not None:
            return kdb_identity(self.probe["kdb_path"]) == self.kdb_identity
        return True
