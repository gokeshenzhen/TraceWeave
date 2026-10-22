"""Request-local trace view of an already pinned independent context.

The dispatcher owns validation, binding and the pin. This view neither resolves
legacy handles nor materializes a hierarchy. It cannot outlive that request.
"""
from __future__ import annotations

from dataclasses import dataclass

from .design_context_runtime import ContextUnavailable
from .divergence_compare import file_identity


@dataclass(frozen=True)
class PreparedTraceContext:
    prepared: object
    design_handle: str
    source_execution: object | None = None
    history_budget: object | None = None
    prior_attempts: tuple = ()
    native_input_contract: bool = False

    @property
    def context(self) -> dict:
        context = self.prepared.context
        return {
            "compile_log": context.ordered_compile_logs[0],
            "supplementary_compile_logs": list(context.ordered_compile_logs[1:]),
            "simulator": context.simulator,
            "top_hint": context.selected_top,
            "design_handle": self.design_handle,
            "context_identity_sha256": context.identity_sha256,
            "backend_preference": context.backend_preference,
            "source_snapshot_sha256": context.snapshot.content_fingerprint_sha256,
        }

    @property
    def compile_result(self) -> dict:
        return self.prepared.context.compile_result()

    @property
    def snapshot(self) -> str:
        return self.prepared.context.identity_sha256

    @property
    def hierarchy(self) -> None:
        # A metadata-only view must not masquerade as a lexical tree.
        return None

    def current(self) -> bool:
        return self.prepared.context.current()


class PreparedTraceWave:
    """One waveform generation across all snapshot backend attempts."""

    def __init__(self, path: str):
        self.path = path
        self.identity = file_identity(path)
        self.parser_generation = None

    def observe(self, parser) -> None:
        # The caller owns the waveform lock while observing parser state.
        token = (id(parser), getattr(parser, "_scope_epoch", None))
        if self.parser_generation is not None and token != self.parser_generation:
            raise ContextUnavailable("parser_generation_changed")
        self.parser_generation = token

    def require_current(self) -> None:
        if self.identity is None or file_identity(self.path) != self.identity:
            raise ContextUnavailable("waveform_changed")


@dataclass(frozen=True)
class NativeTraceContext:
    prepared: object
    design_handle: None = None
    source_execution: object | None = None

    @property
    def context(self):
        inputs = self.prepared.context
        return dict(compile_log=inputs.ordered_compile_logs[0] if inputs.ordered_compile_logs else None,
            supplementary_compile_logs=list(inputs.ordered_compile_logs[1:]),
            simulator=inputs.simulator, top_hint=inputs.selected_top,
            compile_metadata_identity_sha256=inputs.compile_metadata_identity_sha256)

    @property
    def compile_result(self):
        return self.prepared.context.compile_result()

    @property
    def snapshot(self):
        import hashlib
        anchor = self.prepared.binding.anchor
        return hashlib.sha256(((anchor.identity_sha256 if anchor else "unavailable") + "\0" +
            self.prepared.context.selected_top).encode()).hexdigest()

    @property
    def hierarchy(self):
        return None

    def current(self):
        return self.prepared.context.current()
