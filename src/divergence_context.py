"""Exact, read-only compile-context resolution and executable diff relays."""
from __future__ import annotations

from dataclasses import dataclass
import os

from .cancellation import check_cancelled
from .compile_session_snapshot import CompileSessionSnapshot
from .hierarchy_handles import compute_handle, compute_snapshot_fingerprint
from .schemas import DivergenceContext


@dataclass(frozen=True)
class BoundContext:
    context: dict
    hierarchy: dict
    snapshot: str

    @property
    def compile_result(self) -> dict:
        return self.hierarchy["compile_result"]

    def current(self) -> bool:
        check_cancelled()
        ctx = self.context
        if self.snapshot != compute_snapshot_fingerprint(
            ctx["compile_log"], ctx["simulator"], ctx["supplementary_compile_logs"]
        ):
            return False
        source_snapshot = self.hierarchy.get("_compile_session_snapshot")
        return isinstance(source_snapshot, CompileSessionSnapshot) and source_snapshot.current()


def resolve_context(raw: dict, store) -> tuple[BoundContext | None, str]:
    ctx = DivergenceContext.model_validate(raw).model_dump(exclude_none=True)
    ctx["compile_log"] = os.path.abspath(ctx["compile_log"])
    ctx["supplementary_compile_logs"] = [os.path.abspath(p) for p in ctx["supplementary_compile_logs"]]
    simulator = ctx["simulator"]
    candidates = ("vcs", "xcelium", "auto") if simulator == "auto" else (simulator,)
    matches = []
    reason = "hierarchy_unavailable"
    for sim in candidates:
        snapshot = compute_snapshot_fingerprint(ctx["compile_log"], sim, ctx["supplementary_compile_logs"])
        handle = compute_handle(ctx["compile_log"], sim, ctx["supplementary_compile_logs"])
        if ctx.get("snapshot_sha256") not in (None, snapshot) or ctx.get("hierarchy_handle") not in (None, handle):
            reason = "compile_context_changed"
            continue
        hierarchy = store.resolve(handle)
        if not isinstance(hierarchy, dict) or not isinstance(hierarchy.get("compile_result"), dict):
            continue
        if hierarchy.get("_hierarchy_snapshot_sha256") != snapshot:
            reason = "compile_context_changed"
            continue
        exact = {**ctx, "simulator": sim, "hierarchy_handle": handle, "snapshot_sha256": snapshot}
        bound = BoundContext(exact, hierarchy, snapshot)
        if not bound.current():
            reason = "compile_context_changed"
            continue
        matches.append(bound)
    if len(matches) == 1:
        return matches[0], "ready"
    return None, "compile_context_ambiguous" if matches else reason


def driver_action(*, result: dict, side: str, context: dict | None,
                  bound: BoundContext | None = None, reason: str = "hierarchy_unavailable") -> dict:
    arguments = {"wave_path": result[f"wave_path_{side}"],
                 "signal_path": result[f"signal_{side}"], "recursive": False}
    action = dict(side=side, tool="explain_signal_driver", arguments=arguments,
                  reason="inspect_divergent_signal_driver", status="needs_context",
                  missing_fields=[f"context_{side}.compile_log"], prerequisite_calls=[],
                  evidence={"time_ps": result["first_divergence_time_ps"], "side": side})
    if result.get("cursor"):
        action["evidence"]["cursor_name"] = result["cursor"]["name"]
    if context is None:
        return action
    ctx = DivergenceContext.model_validate(context).model_dump(exclude_none=True)
    arguments.update(compile_log=ctx["compile_log"], simulator=ctx["simulator"], compile_context=ctx)
    if ctx.get("top_hint"):
        arguments["top_hint"] = ctx["top_hint"]
    action["missing_fields"] = []
    if bound is not None:
        arguments.update(compile_log=bound.context["compile_log"],
                         simulator=bound.context["simulator"], compile_context=bound.context)
        action["status"] = "ready"
        return action
    action["status"] = "needs_prerequisite"
    action["evidence"]["context_status"] = reason
    # A rebuild establishes a new identity. Do not relay stale tokens to it.
    arguments["compile_context"].pop("snapshot_sha256", None)
    arguments["compile_context"].pop("hierarchy_handle", None)
    build = {k: ctx[k] for k in ("compile_log", "simulator", "supplementary_compile_logs")}
    calls = [{"id": f"hierarchy_{side}", "tool": "build_tb_hierarchy", "arguments": build,
              "depends_on": [], "parallel_group": f"compile_{side}"}]
    for index, path in enumerate([ctx["compile_log"], *ctx["supplementary_compile_logs"]]):
        calls.append({"id": f"scan_{side}_{index}", "tool": "scan_structural_risks",
                      "arguments": {"compile_log": path, "simulator": ctx["simulator"]},
                      "depends_on": [], "parallel_group": f"compile_{side}"})
    action["prerequisite_calls"] = calls
    return action
