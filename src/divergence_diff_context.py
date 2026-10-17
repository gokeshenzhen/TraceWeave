"""Keep diff actions on their explicit designs without preparing a backend.

Comparisons read waveforms only. Pins and validation protect the identities
relayed to later driver calls; neither discovery nor licensed work is needed.
Legacy compile contexts retain their exact historical recovery tokens.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack

import anyio

from .cancellation import OperationCancelled
from .compile_input_capture import CaptureAborted
from .design_backend_binding import ExecutionBindingChanged
from .design_context_runtime import ContextUnavailable
from .kdb_anchor import KdbBindingChanged
from .prepared_trace_context import PreparedTraceContext, PreparedTraceWave
from .schemas import DivergenceQueryContext, DiffFirstDivergenceResult, ToolErrorResult


async def diff_with_context(services, args):
    models = {side: DivergenceQueryContext.model_validate(args[f"context_{side}"])
              for side in ("a", "b") if args.get(f"context_{side}") is not None}
    contexts = {side: (models[side].model_dump(exclude_none=True) if side in models else None)
                for side in ("a", "b")}
    store, cursor_store = services._design_context_store, services._cursor_store
    entries, bounds, legacy_bounds, waves = {}, {}, {}, {}
    paths = [args["wave_path_a"], args["wave_path_b"]]

    def require_owner():
        if entries:
            services._require_design_store_owner(store)

    def get_parser(path):
        # Called only inside the normal dual-path waveform worker/locks.
        if path in waves:
            waves[path].require_current()
        parser = services._get_parser(path)
        if path in waves:
            waves[path].observe(parser)
        return parser

    def check_parsers():
        for path in waves:
            get_parser(path)

    def check_legacy_actions():
        # A mixed request may hash its prepared side after resolving the old
        # side. Revalidate retained legacy tokens before publishing a ready
        # action; never turn a late source change into a new current identity.
        for bound in legacy_bounds.values():
            if not bound.current():
                raise ContextUnavailable("compile_context_changed")

    async def validate():
        for entry in entries.values():
            await services._validate_design_source(entry.context)
            if entry.binding is not None:
                await services._validate_design_binding(entry.binding)
            require_owner()

    def metadata():
        # Recheck A after hashing B and after reacquiring parser locks. These
        # bounded observations do not make external file mutations atomic.
        for entry in entries.values():
            if not entry.context.metadata_current():
                raise ContextUnavailable("design_context_changed")
            if entry.binding is not None:
                entry.binding.require_policy_current()
                if entry.binding.anchor is not None and not entry.binding.anchor.metadata_current():
                    raise KdbBindingChanged()
        for wave in waves.values():
            wave.require_current()

    try:
        with ExitStack() as pins:
            # Pin both sides before the first await. A shared handle has one
            # pin and one validation, and neither side can evict the other.
            for ctx in contexts.values():
                handle = ctx.get("design_handle") if ctx else None
                if handle is not None and handle not in entries:
                    entries[handle] = pins.enter_context(store.pin(handle))
            for side, model in models.items():
                if model.design_handle is None:
                    continue
                entry = entries[model.design_handle]
                context = entry.context
                if ((model.compile_log is not None and not services._same_realpath(
                        model.compile_log, context.ordered_compile_logs[0]))
                        or model.simulator not in ("auto", context.simulator)
                        or model.top_hint not in (None, context.selected_top)):
                    raise ContextUnavailable("design_context_arguments_mismatch")
                if "supplementary_compile_logs" in model.model_fields_set:
                    supplied = model.supplementary_compile_logs
                    expected = context.ordered_compile_logs[1:]
                    if (len(supplied) != len(expected) or any(not services._same_realpath(a, b)
                            for a, b in zip(supplied, expected))):
                        raise ContextUnavailable("design_context_arguments_mismatch")
                bounds[side] = PreparedTraceContext(entry, model.design_handle)
                contexts[side] = bounds[side].context
            if entries:
                await validate()
                waves.update((path, PreparedTraceWave(path)) for path in dict.fromkeys(paths))

            def work():
                return services.diff_first_divergence(
                    get_parser=get_parser, wave_path_a=paths[0], signal_a=args["signal_a"],
                    wave_path_b=paths[1], signal_b=args["signal_b"],
                    start_ps=services._resolve_time(args.get("start_time_ps", 0)),
                    end_ps=services._resolve_time(args.get("end_time_ps", -1), allow_sentinel=True),
                    cursor_store=None, cursor_name=args.get("cursor_name"), cursor_note=args.get("cursor_note"))

            result = await services._run_in_wave_request(paths, work)
            if result["diverged"]:
                for side, ctx in contexts.items():
                    if side in bounds:
                        resolved, reason = bounds[side], "ready"
                    elif ctx is not None:
                        resolved, reason = await services._run_in_cancellable_thread(
                            lambda ctx=ctx: services.resolve_divergence_context(ctx, services._handle_store))
                        if resolved is not None:
                            legacy_bounds[side] = resolved
                    else:
                        resolved, reason = None, "context_missing"
                    expression = next((row for row in result.get("expressions", []) if row.get("side") == side), None)
                    targets = expression["dependencies"][:16] if expression else [result[f"signal_{side}"]]
                    for target in targets:
                        action = services.driver_action(result={**result, f"signal_{side}": target}, side=side,
                                                        context=ctx, bound=resolved, reason=reason)
                        if expression:
                            action["reason"] = "inspect_expression_dependency"
                            action["evidence"]["expression_key"] = expression["key"]
                        if side in bounds:
                            action["evidence"]["context_identity_sha256"] = bounds[side].snapshot
                        result["next_actions"].append(action)

            if entries:
                await validate()
                with anyio.fail_after(10):
                    await services._run_in_cancellable_thread(metadata)
                    await services._run_in_wave_request(paths, check_parsers)
                    if legacy_bounds:
                        await services._run_in_cancellable_thread(check_legacy_actions)
                    await services._run_in_cancellable_thread(metadata)
                await anyio.lowlevel.checkpoint()
                require_owner()
            # Validate the complete payload before the sole cursor publication.
            DiffFirstDivergenceResult.model_validate(result)
            if result["diverged"]:
                from .verify_condition import _attach_cursor
                _attach_cursor(result, cursor_store, result["first_divergence_time_ps"],
                               result["value_a"], result["value_b"], paths[0], result["signal_a"],
                               paths[1], result["signal_b"], args.get("cursor_name"), args.get("cursor_note"))
                for action in result["next_actions"]:
                    action["evidence"]["cursor_name"] = result["cursor"]["name"]
            return DiffFirstDivergenceResult.model_validate(result)
    except OperationCancelled as exc:
        raise asyncio.CancelledError from exc
    except (ContextUnavailable, KdbBindingChanged, CaptureAborted, ExecutionBindingChanged, TimeoutError) as exc:
        code = (exc.code if isinstance(exc, ContextUnavailable)
                else "kdb_identity_changed" if isinstance(exc, KdbBindingChanged)
                else "design_context_execution_changed" if isinstance(exc, ExecutionBindingChanged)
                else "design_context_changed" if isinstance(exc, CaptureAborted) and exc.code == "input_changed"
                else "design_context_" + exc.code if isinstance(exc, CaptureAborted)
                else "design_context_timeout")
        return ToolErrorResult(error="The comparison context changed or did not complete; no cursor or actions were published.",
                               error_code=code)
