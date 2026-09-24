"""Typed public expression failures with bounded, actionable recovery advice."""
from __future__ import annotations


_GROUPS = {
    "expression_type_unresolved": {
        "expression_type_unresolved", "expression_array_type_unresolved", "expression_member_type_unresolved",
    },
    "expression_shape_invalid": {
        "expression_dimensions_invalid", "expression_packed_shape_invalid", "expression_member_shape_invalid",
        "expression_member_duplicate", "expression_dump_type_shape_mismatch", "expression_array_mapping_invalid",
    },
    "expression_limit_exceeded": {
        "expression_text_limit", "dynamic_expression_limit", "dynamic_expression_width_limit", "expression_width_limit", "expression_type_limit",
        "expression_dependency_limit", "expression_count_limit", "expression_sample_limit", "expression_dependency_sample_limit",
    },
    "expression_syntax_invalid": {
        "expression_invalid_token", "expression_syntax_invalid", "expression_unexpected_end",
        "expression_operand_expected", "expression_member_expected", "expression_empty_set",
        "expression_empty_arguments", "expression_trailing_input", "expression_literal_invalid",
    },
    "expression_constant_required": {
        "expression_constant_required", "expression_known_constant_required", "expression_constant_size_invalid",
        "expression_part_direction_invalid", "expression_part_width_invalid",
    },
    "expression_unsupported": {
        "expression_array_cast_unsupported", "expression_unpacked_slice_unsupported",
        "expression_syntax_unsupported", "expression_function_unsupported",
    },
    "expression_operand_invalid": {
        "expression_array_requires_selection", "expression_concat_operand_invalid",
        "expression_index_not_integral", "expression_function_arity_invalid",
    },
    "expression_sampling_unavailable": {
        "expression_dependency_group_unavailable", "legacy_sub_ps_order_unavailable",
        "expression_declaration_changed",
    },
}
_CODES = {reason: code for code, reasons in _GROUPS.items() for reason in reasons}
_RECOVERY = {
    "expression_type_unresolved": ("provide_types", "Supply the operand's declared type in types, including array/member layout. Use typing=wave_bits only when an unsigned dump-vector interpretation is intended."),
    "expression_shape_invalid": ("correct_type_mapping", "Match types to the exact dump declaration and declared dimensions. For unpacked arrays, use unique in-range elements indices; do not infer missing storage."),
    "expression_limit_exceeded": ("reduce_request", "Split expressions or selections and reduce width/dependencies. For sampling limits, narrow the time window or number of cycles."),
    "expression_syntax_invalid": ("correct_expression", "Use a read-only SV value expression. Check tokens, parentheses and operands; assignments, temporal syntax and side effects are unsupported."),
    "expression_constant_required": ("provide_constant", "Use known integral constants for slice widths, replication counts and streaming sizes. Supply constants explicitly; fixed part-select bounds must follow the declared direction."),
    "expression_unsupported": ("rewrite_expression", "Rewrite with supported integral operators or query already dumped intermediate signals. User functions, side effects and unpacked slices are not evaluated."),
    "expression_operand_invalid": ("correct_operand", "Select an integral array element before applying this operator and check the supported system function's argument count."),
    "expression_sampling_unavailable": ("check_waveform", "Check waveform/declaration identity and event-read capability. Use a new query on current artifacts; a point query can inspect a value but cannot prove missing event order."),
    "expression_signal_unresolved": ("resolve_binding", "Use search_signals and bind exact paths. For an unpacked array supply types.<name>.unpacked plus bindings.<name>.elements=[{indices:[i],signal:path}], or a 1D path_template with one {index} (at most 128 elements). scope does not discover elements. Do not assume undumped values."),
    "expression_control_width_invalid": ("correct_control_width", "Provide an expression with the role's required width; use an explicit comparison for Boolean controls, and a two-bit expression for valid_htrans."),
    "expression_window_invalid": ("correct_window", "Provide a nonnegative start and an end at or after start; -1 selects the waveform end where supported."),
    "expression_input_invalid": ("correct_input", "Correct the expression object's fields using WaveformExpression in tools/list. Check value types, required fields and declared limits."),
    "expression_binding_invalid": ("correct_binding", "Use an exact dump declaration and valid declared bit indices. Supply bits OR lsb+width, not both."),
}


class ExpressionError(ValueError):
    """A ValueError-compatible failure; only fixed codes enter telemetry."""

    def __init__(self, reason, *, operand=None, position=None, parameter=None, message=None, issues=None):
        self.reason = reason
        self.code = _CODES.get(reason, reason)
        if self.code not in _RECOVERY:
            raise ValueError(f"Unregistered expression error: {reason}")
        self.operand = operand
        self.position = position
        self.parameter = parameter
        self.issues = (issues or [])[:16]
        super().__init__(reason if message is None else message)

    def payload(self):
        action, message = _RECOVERY[self.code]
        if self.reason == "dynamic_expression_limit":
            from .dynamic_evidence import MAX_EXPR_DEPTH, MAX_EXPR_NODES
            message = (f"Expression complexity exceeds a bound: depth {MAX_EXPR_DEPTH}, "
                       f"nodes {MAX_EXPR_NODES}, or tokens {MAX_EXPR_NODES * 8}. "
                       "Shorten nested ternary chains, remove redundant parentheses, "
                       "or split into smaller expressions. Narrowing the time window "
                       "does not reduce expression complexity.")
        if self.reason == "expression_array_type_unresolved":
            message = ("Supply types.<operand> with the per-element width and declared unpacked "
                       "ranges, e.g. {width:8,unpacked:[[0,7]]}. Bounds must be JSON integers. "
                       "typing=wave_bits cannot infer unpacked dimensions. Bind elements explicitly or use a 1D path_template with one {index} (at most 128 elements).")
        if self.code == "expression_input_invalid":
            message = ("Fix the listed fields together. Ordinary unsigned [7:0] needs types.a={width:8}; "
                       "range bounds are JSON integers, e.g. unpacked:[[0,7]]. "
                       "Arrays need elements OR a 1D path_template with one {index}.")
        return {"error": str(self), "error_code": self.code, "reason": self.reason,
                "operand": self.operand, "position": self.position, "parameter": self.parameter,
                "recovery": {"action": action, "message": message}, "issues": self.issues}


def input_validation_issues(exc, raw, prefix=""):
    """Project existing Pydantic errors, excluding irrelevant union branches."""
    issues = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        loc = list(error['loc'])
        if len(loc) >= 3 and loc[0] == 'bindings':
            binding = raw.get('bindings', {}).get(loc[1]) if isinstance(raw.get('bindings'), dict) else None
            if isinstance(binding, dict):
                chosen = 'ExpressionArray' if 'elements' in binding or 'path_template' in binding else 'WaveformSelection'
                if chosen not in str(loc[2]):
                    continue
                del loc[2]
        parameter = '.'.join(str(p) for p in loc)
        issues.append({'parameter': '.'.join(p for p in (prefix, parameter) if p),
                       'message': error['msg'][:300]})
        if len(issues) == 16:
            break
    return issues
