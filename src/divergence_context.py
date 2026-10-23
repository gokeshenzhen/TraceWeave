"""Exact, read-only compile-context resolution and executable diff relays."""

from __future__ import annotations

from dataclasses import dataclass
import os
import hashlib
import json

from config import STRUCTURAL_SCAN_IN_WORKFLOW

from .cancellation import check_cancelled
from .compile_session_snapshot import CompileSessionSnapshot
from .hierarchy_handles import compute_handle, compute_snapshot_fingerprint
from .schemas import DivergenceContext
from .divergence_compare import file_identity


def log_identity(ctx: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            [
                file_identity(p)
                for p in [
                    ctx["compile_log"],
                    *ctx.get("supplementary_compile_logs", ()),
                ]
            ]
        ).encode()
    ).hexdigest()


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
        if ctx.get("compile_log_identity_sha256") != log_identity(ctx):
            return False
        if self.snapshot != compute_snapshot_fingerprint(
            ctx["compile_log"], ctx["simulator"], ctx["supplementary_compile_logs"]
        ):
            return False
        source_snapshot = self.hierarchy.get("_compile_session_snapshot")
        return (
            isinstance(source_snapshot, CompileSessionSnapshot)
            and source_snapshot.current()
        )


def resolve_context(raw: dict, store) -> tuple[BoundContext | None, str]:
    ctx = DivergenceContext.model_validate(raw).model_dump(exclude_none=True)
    ctx["compile_log"] = os.path.abspath(ctx["compile_log"])
    ctx["supplementary_compile_logs"] = [
        os.path.abspath(p) for p in ctx["supplementary_compile_logs"]
    ]
    simulator = ctx["simulator"]
    candidates = ("vcs", "xcelium", "verilator", "auto") if simulator == "auto" else (simulator,)
    matches = []
    reason = "hierarchy_unavailable"
    eligible = False
    logs_id = log_identity(ctx)
    if ctx.get("compile_log_identity_sha256") not in (None, logs_id):
        return None, "compile_context_changed"
    for sim in candidates:
        snapshot = compute_snapshot_fingerprint(
            ctx["compile_log"], sim, ctx["supplementary_compile_logs"]
        )
        handle = compute_handle(
            ctx["compile_log"], sim, ctx["supplementary_compile_logs"]
        )
        if ctx.get("snapshot_sha256") not in (None, snapshot) or ctx.get(
            "hierarchy_handle"
        ) not in (None, handle):
            continue
        eligible = True
        hierarchy = store.resolve(handle)
        if not isinstance(hierarchy, dict) or not isinstance(
            hierarchy.get("compile_result"), dict
        ):
            continue
        if hierarchy.get("_hierarchy_snapshot_sha256") != snapshot:
            reason = "compile_context_changed"
            continue
        source_snapshot = hierarchy.get("_compile_session_snapshot")
        if not isinstance(source_snapshot, CompileSessionSnapshot):
            reason = "compile_context_changed"
            continue
        source_id = source_snapshot.content_fingerprint_sha256
        actual_simulator = hierarchy["compile_result"].get("simulator")
        if actual_simulator in {"vcs", "xcelium", "verilator"} and sim not in {"auto", actual_simulator}:
            reason = "compile_context_changed"
            continue
        selected_top = (hierarchy.get("project", {}).get("top_module")
                        or hierarchy["compile_result"].get("primary_top"))
        if (ctx.get("top_hint") and ctx["top_hint"] != selected_top
                and ctx["top_hint"] not in hierarchy.get("component_tree", {})):
            reason = "compile_context_changed"
            continue
        captured_logs = hierarchy.get("_compile_log_identity_sha256")
        if (
            ctx.get("source_snapshot_sha256") not in (None, source_id)
            or ctx.get("compile_log_identity_sha256") not in (None, logs_id)
            or captured_logs not in (None, logs_id)
        ):
            reason = "compile_context_changed"
            continue
        exact = {
            **ctx,
            "simulator": sim,
            "hierarchy_handle": handle,
            "snapshot_sha256": snapshot,
            "source_snapshot_sha256": source_id,
            "compile_log_identity_sha256": logs_id,
        }
        bound = BoundContext(exact, hierarchy, snapshot)
        if not bound.current():
            reason = "compile_context_changed"
            continue
        matches.append(bound)
    if len(matches) == 1:
        return matches[0], "ready"
    return None, "compile_context_ambiguous" if matches else (reason if eligible else "compile_context_changed")


def driver_action(
    *,
    result: dict,
    side: str,
    context: dict | None,
    bound: BoundContext | None = None,
    reason: str = "hierarchy_unavailable",
    native: bool = False,
) -> dict:
    arguments = {
        "wave_path": result[f"wave_path_{side}"],
        "signal_path": result[f"signal_{side}"],
        "recursive": False,
    }
    action = dict(
        side=side,
        tool="explain_signal_driver",
        arguments=arguments,
        reason="inspect_divergent_signal_driver",
        status="needs_context",
        missing_fields=[f"context_{side}.compile_log"],
        prerequisite_calls=[],
        evidence={"time_ps": result["first_divergence_time_ps"], "side": side},
    )
    if result.get("cursor"):
        action["evidence"]["cursor_name"] = result["cursor"]["name"]
    if context is None:
        return action
    if context.get("design_handle") is not None:
        if bound is not None:
            arguments["design_handle"] = bound.design_handle
            action.update(status="ready", missing_fields=[])
            return action
        action["missing_fields"] = [f"side_{side}.design_handle"]
        action["evidence"]["context_status"] = reason
        # A new preparation establishes current inputs. Never relay a stale
        # random handle or a composite historical-source fingerprint as an
        # expected identity for the ordinary prepare API.
        if context.get("compile_log"):
            prepare = {key: context[key] for key in (
                "compile_log", "simulator", "supplementary_compile_logs",
                "top_hint", "backend_preference", "kdb_path",
            ) if context.get(key) is not None}
            action["status"] = "needs_prerequisite"
            action["prerequisite_calls"] = [{
                "id": f"design_{side}", "tool": "prepare_design_context",
                "arguments": prepare, "depends_on": [], "parallel_group": f"compile_{side}",
            }]
            action["evidence"]["preparation_identity"] = "current_inputs_only"
            action["prerequisite_calls"].extend(_scan_calls(context, side))
        else:
            action["missing_fields"].append(f"side_{side}.compile_log")
        return action
    if native:
        # A retry starts a new native observation; no stale bound context or
        # legacy compile_context identity is carried across a lifecycle error.
        arguments.update({key: context[key] for key in (
            "compile_log", "supplementary_compile_logs", "simulator", "top_hint",
            "kdb_path", "backend_preference", "retry_npi",
        ) if context.get(key) is not None})
        action.update(status="ready", missing_fields=[])
        action["evidence"]["context_status"] = reason
        return action
    if bound is not None and getattr(getattr(bound, "prepared", None), "native", False):
        inputs = bound.prepared.context
        arguments.update({key: value for key, value in {
            "compile_log": inputs.ordered_compile_logs[0] if inputs.ordered_compile_logs else None,
            "supplementary_compile_logs": list(inputs.ordered_compile_logs[1:]),
            "simulator": inputs.simulator, "top_hint": inputs.selected_top,
            "kdb_path": inputs.requested_kdb_path}.items() if value is not None})
        action.update(status="ready", missing_fields=[])
        return action
    ctx = DivergenceContext.model_validate(context).model_dump(exclude_none=True)
    arguments.update(
        compile_log=ctx["compile_log"], simulator=ctx["simulator"], compile_context=ctx
    )
    if ctx.get("top_hint"):
        arguments["top_hint"] = ctx["top_hint"]
    action["missing_fields"] = []
    if bound is not None:
        arguments.update(
            compile_log=bound.context["compile_log"],
            simulator=bound.context["simulator"],
            compile_context=bound.context,
        )
        action["status"] = "ready"
        return action
    action["evidence"]["context_status"] = reason
    # An unavailable in-process tree is recoverable by the existing exact
    # action entry point. Retain every historical token for its verification;
    # a changed identity must never become a token-free current-source action.
    if reason in {"hierarchy_unavailable", "handle_expired"}:
        action["status"] = "ready"
        action["evidence"]["context_recovery"] = "required_before_query"
    else:
        action["status"] = "needs_context"
        action["missing_fields"] = [f"context_{side}.verified_identity"]
    action["prerequisite_calls"] = _scan_calls(ctx, side)
    return action


def _scan_calls(ctx, side):
    """Independent default-workflow scans never require a full hierarchy."""
    if not STRUCTURAL_SCAN_IN_WORKFLOW:
        return []
    calls = []
    for index, path in enumerate(
        [ctx["compile_log"], *ctx.get("supplementary_compile_logs", [])]
    ):
        calls.append(
            {
                "id": f"scan_{side}_{index}",
                "tool": "scan_structural_risks",
                "arguments": {"compile_log": path, "simulator": ctx.get("simulator", "auto")},
                "depends_on": [],
                "parallel_group": f"compile_{side}",
            }
        )
    return calls
