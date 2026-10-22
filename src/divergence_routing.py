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
from .compile_input_capture import CaptureAborted
from .design_backend_binding import ExecutionBindingChanged
from .design_context_runtime import ContextUnavailable
from .connectivity_backend import (
    DeferredConnectivityFallbackBackend,
    StaticConnectivityBackend,
    select_backend,
)
from .divergence_budget import TraceRestart, BudgetExceeded
from .kdb_identity import kdb_identity
from .kdb_anchor import KdbBindingChanged
from .dynamic_evidence import unsupported_step
from .source_graph_adapter import AdapterStatus
from .source_graph_runtime import PrepareStatus
from .source_graph_x_trace import (
    SourceGraphTraceArtifactGuard,
    SourceGraphTraceQueryLedger,
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
        self.config = getattr(bound, "source_execution", None) or get_source_graph_execution_config()
        self.targets = [side["signal_path"]]
        self.attempts = list(getattr(bound, "prior_attempts", ()))
        self.receipt = {}
        self.artifact = None
        self.kdb_identity = None
        self.fallback_reason = None
        self.execution = {}
        self.probe = {}
        self.source_receipt = None
        self.selected_backend = "verdi_npi" if self.attempts else "source_graph"
        self.retain_async_controls = False
        self.root_binding = None
        self.prepared = getattr(bound, "prepared", None)
        self.npi_backend = None
        self.trace_plan = None
        self.query_ledger = None
        self.native = getattr(self.prepared, "native", False)
        if self.prepared is not None and not self.native:
            from .prepared_source_trace import PreparedTracePlan
            self.trace_plan = PreparedTracePlan(self.prepared.context, self.config,
                                               budget.deadline, budget.limits['max_depth'])

    async def initialize(self):
        if self.native and str(self.side['signal_path']).startswith('TOP.'):
            self.root_binding = await self.budget.run(lambda: self.s._bind_wave_design_root(
                {**self.side, "_native_query": self.prepared}, self.bound.context["simulator"]))
            if self.root_binding:
                self.targets = [self.root_binding.to_design(p) for p in self.targets]
        if str(self.side['signal_path']).startswith('TOP.') and not self.native:
            from .wave_design_binding import bind_root
            self.root_binding = await self.budget.run(lambda: self.s._run_in_wave_thread(
                self.side['wave_path'], lambda: bind_root(
                    self.s._get_parser(self.side['wave_path']), self.side['signal_path'],
                    self.bound.compile_result, top_hint=self.side.get('top_hint'))))
            if self.root_binding:
                self.targets = [self.root_binding.to_design(p) for p in self.targets]
        selection_reason = None
        if self.prepared is not None:
            self.budget.check()
            self.backend, self.probe, selection_reason = self.s._select_prepared_backend(
                self.prepared, DeferredConnectivityFallbackBackend()
            )
        else:
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
            except (OperationCancelled, CaptureAborted, KdbBindingChanged, ExecutionBindingChanged,
                    ContextUnavailable, TimeoutError):
                raise
            except Exception:
                self.backend = None
        if getattr(self.backend, "name", None) == "verdi_npi":
            self.npi_backend = self.backend
            if self.prepared is None:
                self.backend.bind_compile_context(self.bound.compile_result)
            self.stage = "verdi_npi"
            self.selected_backend = "verdi_npi"
            kdb = self.probe.get("kdb_path")
            self.kdb_identity = (self.prepared.binding.anchor.identity_sha256 if self.prepared is not None
                                 else kdb_identity(kdb) if kdb else None)
            self.artifact = hashlib.sha256(
                json.dumps(
                    [self.bound.snapshot, self.side.get("top_hint"), self.kdb_identity]
                ).encode()
            ).hexdigest()
        else:
            reason = selection_reason or self.probe.get("_npi_selection_reason") or (
                "npi_skipped_by_policy"
                if self.probe.get("connectivity_route") == "source_graph"
                else "npi_kdb_unavailable"
            )
            if not self.attempts:
                self.attempts.append(
                    dict(backend="verdi_npi", status=("failed" if selection_reason else
                        "skipped" if self.prepared is not None and (
                            self.prepared.binding.disposition == "source_policy" or self.prepared.npi_failure
                        ) else "unavailable"), reason=reason)
                )
            self.fallback_reason = self.attempts[0].get("reason") or reason
            self.stage = "source_graph"

    async def _validate_source(self):
        if self.native:
            from .native_query import NativeFallback
            self.budget.check()
            raise NativeFallback(self.attempts, side=self.label, budget=self.budget)
        if self.prepared is None:
            return
        self.budget.check()
        if self.s.get_source_graph_execution_config() != self.config:
            raise ExecutionBindingChanged()
        await self.budget.run(lambda: self.s._validate_design_source(
            self.prepared.context, timeout_sec=min(30, self.budget.remaining)))
        await self.budget.run(lambda: self.s._validate_design_binding(
            self.prepared.binding, timeout_sec=min(5, self.budget.remaining)))

    async def _prepare_source(self):
        reason = None
        await self._validate_source()
        if not self.config.enabled or not self.config.valid:
            reason = 'source_graph_disabled' if not self.config.enabled else 'source_graph_config_invalid'
        else:
            try:
                plan = await self.budget.run(
                    lambda: self.s._run_in_cancellable_thread(
                        lambda: self.trace_plan.build(self.targets) if self.trace_plan is not None
                        else self.s.build_source_graph_trace_plan(
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
                await self._validate_source()
                if plan.status is AdapterStatus.BLOCKED:
                    reason = "source_graph_" + plan.receipt.blocker.code
                else:
                    runtime = self.s.get_source_graph_runtime(self.config)
                    outcome = await self.budget.run(
                        lambda: runtime.prepare(
                            plan.request,
                            timeout_seconds=min(
                                self.config.timeout_sec, self.budget.limits["timeout_sec"]
                            ),
                        )
                    )
                    if self.prepared is not None and outcome.status is PrepareStatus.TIMED_OUT:
                        raise TimeoutError("source trace preparation deadline")
                    await self._validate_source()
                    if self.trace_plan is not None:
                        self.trace_plan.observe(outcome)
                    if hasattr(self.budget, "record_preparation"):
                        self.budget.record_preparation(outcome.metrics)
                    self.source_receipt = self.s._source_graph_receipt_from_prepare(
                        plan, outcome, adapter_wall_ms=0
                    )
                    if self.trace_plan is not None:
                        self.trace_plan.receipt(self.source_receipt)
                    if outcome.status is PrepareStatus.CANCELLED:
                        raise asyncio.CancelledError
                    if outcome.status is PrepareStatus.READY:
                        self.backend = self.s._source_graph_backend_for_plan(
                            outcome.entry, plan
                        )
                        if self.trace_plan is not None:
                            from .prepared_source_trace import PreparedTraceArtifactGuard
                            self.backend.query_budget = self.trace_plan.budget
                            self.guard = PreparedTraceArtifactGuard(outcome.entry, plan)
                        else:
                            self.guard = SourceGraphTraceArtifactGuard(
                                artifact_scope=outcome.entry.artifact_scope_receipt.scope,
                                hierarchy_result=self.bound.hierarchy)
                        self.source_receipt.update(single_artifact_provenance=True,
                            final_artifact_scope_match=True,
                            final_artifact_fingerprint_sha256=outcome.entry.build_key.digest)
                        self.artifact = outcome.entry.build_key.digest
                        self.query_ledger = SourceGraphTraceQueryLedger(self.artifact)
                        return
                    reason = self.s._SOURCE_GRAPH_PREPARE_REASONS.get(
                        outcome.status, "source_graph_prepare_failed"
                    )
            except CaptureAborted as exc:
                if exc.code == 'source_scope_limit':
                    raise BudgetExceeded('source_scope_limit') from exc
                raise
            except (OperationCancelled, BudgetExceeded, KdbBindingChanged,
                    ExecutionBindingChanged, ContextUnavailable, TimeoutError):
                raise
            except Exception:
                reason = "source_graph_prepare_failed"
        if self.prepared is not None and self.source_receipt is None:
            self.source_receipt = self.s._blocked_source_graph_receipt(
                "disabled" if not self.config.enabled else "invalid" if not self.config.valid else "blocked",
                code=reason,
                stage="execution_config" if not self.config.enabled or not self.config.valid else "target_scope",
            )
            self.source_receipt["fallback_used"] = True
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
        if self.source_receipt is not None:
            self.source_receipt['fallback_used'] = True

    def _expand(self, expansion):
        paths = expansion.signal_paths
        changed = self.trace_plan.expand(expansion) if self.trace_plan is not None else False
        new = [p for p in paths if p not in self.targets]
        if not new and not changed:
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
        if self.prepared is not None:
            self.prepared.binding.require_policy_current()
        if self.stage is None:
            await self.initialize()
        wave_signal = signal
        if self.root_binding:
            signal = self.root_binding.to_design(signal)
        if self.stage == "source_graph":
            await self._validate_source()
            if self.guard is None:
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
                if self.prepared is not None:
                    self.prepared.binding.require_policy_current()
            except SourceGraphTraceScopeExpansion as exc:
                self._expand(exc)
            except (OperationCancelled, BudgetExceeded, CaptureAborted, KdbBindingChanged,
                    ExecutionBindingChanged, ContextUnavailable, TimeoutError):
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
                # Preserve positive native chains at an explicitly evidenced,
                # unmodeled initial-only root. Generic missing evidence still
                # restarts on Source Graph; this is neither a TB/input verdict
                # nor an evaluated initial value.
                sources = result.get("sources", ())
                initial_leaf = (
                    gaps == {"dynamic_evidence_unavailable"}
                    and result.get("complete") is False
                    and result.get("boundary") == "unsupported"
                    and not result.get("branches") and sources
                    and result.get("state", {}).get("signal")
                    and all(row.get("kind") == "initial" and row.get("cell")
                            and row.get("location")
                            and row.get("declaration") == result["state"]["signal"]
                            for row in sources)
                )
                if initial_leaf:
                    terminal.add("dynamic_evidence_unavailable")
                if (self.retain_async_controls and result.get('async_controls')
                    and result.get('clock') and result.get('boundary') == 'sequential'
                    and gaps <= {'temporal_context_unavailable', 'async_control_value_unmodeled', 'driver_set_incomplete'}):
                    terminal.add('async_control_value_unmodeled')
                from .native_query import native_budget_exhausted
                if self.native and native_budget_exhausted(result):
                    raise BudgetExceeded("native_analysis_limit")
                if not result.get("complete") and not (gaps & terminal):
                    reason = next(
                        iter(sorted(gaps)), "npi_dynamic_evidence_unavailable"
                    )
                    self.attempts.append(
                        dict(backend="verdi_npi", status="inconclusive", reason=reason)
                    )
                    note_failure = getattr(self.s, "_note_design_trace_failure", None)
                    if self.prepared is not None and note_failure is not None:
                        note_failure(self.bound.design_handle, reason)
                    self.fallback_reason = reason
                    if self.native:
                        from .native_query import NativeFallback
                        raise NativeFallback(self.attempts, side=self.label, budget=self.budget)
                    self.stage = "source_graph"
                    self.backend = None
                    self.guard = None
                    raise TraceRestart(self.label, "npi_dynamic_fallback")
            else:
                query_receipt = result.pop("_source_graph_query_receipt", {})
                if self.query_ledger is not None and query_receipt:
                    self.query_ledger.record(signal_path=signal, max_depth=16, recursive=False,
                                             query_receipt=query_receipt)
                    self.source_receipt.update(self.query_ledger.to_dict())
                if self.trace_plan is not None:
                    self.trace_plan.budget.account_query(query_receipt)
                    if self.trace_plan.budget.exhausted or query_receipt.get('query_truncated'):
                        raise BudgetExceeded('source_query_budget')
                frontiers = query_receipt.get("expansion_frontiers", ())
                try:
                    if (
                        frontiers
                        and not result.get("complete")
                        and not (self.trace_plan is not None and self.guard.input_frontier(frontiers)
                                 and (result.get("branches") or result.get("boundary") == "input"))
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
                    self._expand(exc)
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
                      reason='dynamic_evidence_unavailable')
                 if self.stage == 'verdi_npi' and initial_leaf else
                 dict(backend=self.stage, status='inconclusive', coverage_status='partial',
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
        if self.prepared is not None:
            finalized = self.s._finalize_public_connectivity_status(
                backend_status=self.probe,
                selected_backend=self.selected_backend,
                actual_backend=self.stage,
                attempts=self.receipt["attempted_backends"],
                fallback_reason=self.fallback_reason,
                npi_backend=self.npi_backend,
                npi_execution=self.execution or None,
                source_graph_receipt=self.source_receipt,
                npi_kdb_status=self.s._npi_kdb_status(self.npi_backend) if self.npi_backend is not None else None,
            )
            self.receipt = {**finalized, "artifact_sha256": self.artifact}

        if self.root_binding:
            from .x_history_observe import bind_step_wave
            entry = getattr(self.backend, '_entry', None)
            result = await self.budget.run(lambda: self.s._run_in_wave_thread(
                self.side['wave_path'], lambda: bind_step_wave(result,
                    self.s._get_parser(self.side['wave_path']), binding=self.root_binding,
                    engine=entry.query_engine if entry else None)))
            result['signal'] = wave_signal
            result['wave_design_binding'] = self.root_binding.receipt(wave_signal)
        return result

    def current(self, *, validate_binding=True):
        if self.prepared is not None and self.s.get_source_graph_execution_config() != self.config:
            raise ExecutionBindingChanged()
        if self.prepared is not None and validate_binding:
            self.prepared.binding.require_current()
        if self.root_binding:
            from .scope_metadata import file_identity
            if self.root_binding.wave_identity != file_identity(self.side['wave_path']):
                return False
        if self.prepared is None and self.stage == "verdi_npi" and self.kdb_identity is not None:
            return kdb_identity(self.probe["kdb_path"]) == self.kdb_identity
        return True
