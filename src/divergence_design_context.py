"""Request ownership for independent designs in a dual-waveform trace.

All unique handles are pinned before the first await. Source/KDB validation is
outside waveform locks; parser observations stay inside the existing wave
workers. This module owns no semantic tree or native session.
"""
from __future__ import annotations

from contextlib import ExitStack
import time

import anyio

from .cancellation import check_cancelled
from .design_context_runtime import ContextUnavailable
from .divergence_compare import file_identity
from .kdb_anchor import KdbBindingChanged
from .prepared_trace_context import PreparedTraceContext
from .schemas import DesignQueryMetrics


class DivergenceDesigns:
    def __init__(self, services, request_model, sides, budget):
        self.base = services
        self.sides = sides
        self.budget = budget
        self.store = getattr(services, "_design_context_store", None)
        self.fields = {label: getattr(request_model, f"side_{label}").model_fields_set
                       for label in sides}
        self.pins = ExitStack()
        self.entries = {}
        self.owners = {}
        self.metrics = {}
        self.reused = {}
        self.previous_failure = {}
        self.prepared = set()
        self.prepare_ms = 0.0
        self.final_ms = 0.0
        self.parser_generations = {}
        self.parser_changes = set()
        self.wave_ids = {}
        self.services = _TraceServices(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self.pins.__exit__(*exc)

    def pin_all(self):
        # Do not await between A and B: either may otherwise be evicted while
        # the other is queued for source validation or first binding.
        for label, side in self.sides.items():
            handle = side.get("design_handle")
            if handle is not None and handle not in self.entries:
                self.entries[handle] = self.pins.enter_context(self.store.pin(handle))
                self.owners[handle] = label
                self.metrics[handle] = dict.fromkeys(DesignQueryMetrics.model_fields, 0)
                self.previous_failure[handle] = self.entries[handle].npi_failure
        for label, side in self.sides.items():
            handle = side.get("design_handle")
            if handle is None:
                continue
            context = self.entries[handle].context
            if ((side.get("compile_log") is not None and not self.base._same_realpath(
                    side["compile_log"], context.ordered_compile_logs[0]))
                    or side.get("simulator") not in (None, "auto", context.simulator)
                    or side.get("top_hint") not in (None, context.selected_top)):
                raise ContextUnavailable("design_context_arguments_mismatch")
            if "supplementary_compile_logs" in self.fields[label]:
                supplied = side["supplementary_compile_logs"]
                if (len(supplied) != len(context.ordered_compile_logs) - 1
                        or any(not self.base._same_realpath(path, expected)
                               for path, expected in zip(supplied, context.ordered_compile_logs[1:]))):
                    raise ContextUnavailable("design_context_arguments_mismatch")
            side.update(compile_log=context.ordered_compile_logs[0],
                        supplementary_compile_logs=list(context.ordered_compile_logs[1:]),
                        simulator=context.simulator, top_hint=context.selected_top)

    async def resolve(self, label):
        handle = self.sides[label]["design_handle"]
        entry = self.entries[handle]
        if handle not in self.prepared:
            phase = time.monotonic()
            try:
                _, self.reused[handle] = await self.budget.run(
                    lambda: self.base._prepare_pinned_design_entry(
                        self.store, handle, entry, metrics=self.metrics[handle],
                        remaining=lambda cap: min(cap, self.budget.remaining),
                        budget_binding=True,
                    ))
                self.prepared.add(handle)
            finally:
                self.prepare_ms += (time.monotonic() - phase) * 1000
        return PreparedTraceContext(entry, handle)

    def require_owner(self):
        check_cancelled()
        if self.entries:
            self.base._require_design_store_owner(self.store)
        for entry in self.entries.values():
            if entry.binding is not None:
                entry.binding.require_policy_current()

    def observe_parser(self, path, parser):
        token = (id(parser), getattr(parser, "_scope_epoch", None))
        if path in self.parser_generations and self.parser_generations[path] != token:
            if self.entries:
                raise ContextUnavailable("parser_generation_changed")
            # Legacy callers retain their cleared-comparison response shape.
            # Final validation discards any graph produced across generations.
            self.parser_changes.add(path)
        self.parser_generations[path] = token

    def require_wave_file(self, path):
        if self.entries:
            for label, side in self.sides.items():
                if side["wave_path"] == path and file_identity(path) != self.wave_ids[label]:
                    raise ContextUnavailable("waveform_changed")

    async def validate_final(self, bounds, routes, wave_ids):
        """Content-validate each design once, then cross-check final metadata.

        This still runs after the analysis deadline; its elapsed cost is public.
        It cannot make externally mutable source/KDB/wave files atomic. Final
        metadata catches changes to A while B is being hashed, within the
        existing bounded source/map observation contract.
        """
        started = time.monotonic()
        changed = []

        def metadata():
            for label, side in self.sides.items():
                if wave_ids[label] is None or file_identity(side["wave_path"]) != wave_ids[label]:
                    changed.append((label, "waveform_changed"))
                elif side["wave_path"] in self.parser_changes:
                    changed.append((label, "parser_generation_changed"))
            for entry in self.entries.values():
                if not entry.context.metadata_current():
                    raise ContextUnavailable("design_context_changed")
                if entry.binding is not None:
                    entry.binding.require_policy_current()
                    if entry.binding.anchor is not None and not entry.binding.anchor.metadata_current():
                        raise KdbBindingChanged()
            for label, bound in bounds.items():
                if bound is not None and getattr(bound, "prepared", None) is None and not bound.current():
                    changed.append((label, "compile_context_changed"))

        def contents():
            for handle, entry in self.entries.items():
                phase = time.monotonic()
                try:
                    if not entry.context.current():
                        raise ContextUnavailable("design_context_changed")
                    if entry.binding is not None:
                        entry.binding.require_current()
                finally:
                    # This is request-local accounting, never registry state.
                    self.metrics[handle]["final_validation_ms"] += (time.monotonic() - phase) * 1000
            for label, route in routes.items():
                current = (route.current(validate_binding=False)
                           if getattr(bounds[label], "prepared", None) is not None
                           else route.current())
                if not current:
                    changed.append((label, "compile_context_changed"))
            metadata()

        def parsers():
            for path in self.parser_generations:
                if file_identity(path) != next(
                    wave_ids[label] for label, side in self.sides.items() if side["wave_path"] == path
                ):
                    continue  # The metadata pass reports this without opening a replacement.
                self.observe_parser(path, self.base._get_parser(path))

        try:
            # Each raw validation already has its own finite capture timeout.
            # Include queue/lock wait too, even after the analysis budget ends.
            with anyio.fail_after(10 + 35 * max(1, len(self.entries))):
                await self.base._run_in_cancellable_thread(contents)
                if self.parser_generations:
                    await self.base._run_in_wave_thread(list(self.parser_generations), parsers)
                await self.base._run_in_cancellable_thread(metadata)
            await anyio.lowlevel.checkpoint()
            self.require_owner()
            return list(dict.fromkeys(changed))
        finally:
            self.final_ms += (time.monotonic() - started) * 1000

    def describe(self, contexts):
        for label, side in self.sides.items():
            handle = side.get("design_handle")
            if handle not in self.entries:
                continue
            entry = self.entries[handle]
            context = entry.context
            row = dict(
                design_handle=handle, context_identity_sha256=context.identity_sha256,
                backend_preference=context.backend_preference,
                source_snapshot_status="complete" if context.snapshot.complete else "partial",
                include_resolution_status="complete" if context.include_resolution_complete else "partial",
                previous_npi_failure=self.previous_failure[handle],
                metrics_owner_side=self.owners[handle],
            )
            if entry.binding is not None:
                # This is shared candidate evidence only. Actual load receipts
                # belong to each side's backend_status, including clean roots.
                row.update(entry.binding.receipt(), binding_reused=self.reused.get(handle, False))
            row["gaps"] = sorted(set(row.get("gaps", ())) | set(context.gaps))
            if self.owners[handle] == label:
                row["preparation_metrics"] = {key: value for key, value in self.metrics[handle].items()
                                              if key not in {"query_ms", "total_ms", "final_validation_ms"}}
                row["final_validation_ms"] = self.metrics[handle]["final_validation_ms"]
            contexts.setdefault(label, {})["design_context"] = row

    def recovery_context(self, label):
        side = self.sides[label]
        entry = self.entries.get(side.get("design_handle"))
        if entry is None:
            return {key: value for key, value in side.items() if key not in {"wave_path", "signal_path"}}
        context = PreparedTraceContext(entry, side["design_handle"]).context
        if entry.context.requested_kdb_path is not None:
            context["kdb_path"] = entry.context.requested_kdb_path
        return context

    def cost(self):
        return dict(design_context_count=len(self.entries),
                    design_context_preparation_ms=self.prepare_ms,
                    final_identity_validation_ms=self.final_ms)


class _TraceServices:
    def __init__(self, designs):
        self.designs = designs

    def __getattr__(self, name):
        return getattr(self.designs.base, name)

    def _get_parser(self, path):
        self.designs.require_wave_file(path)
        parser = self.designs.base._get_parser(path)
        self.designs.observe_parser(path, parser)
        return parser

    def _note_design_trace_failure(self, handle, reason):
        self.designs.require_owner()
        self.designs.store.note_npi_failure(handle, reason)
