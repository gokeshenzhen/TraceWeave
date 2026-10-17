"""Copy packed field mappings from one exclusive NPI Language owner.

Typespec members establish membership; native bit identity establishes offsets.
No source spelling, member order or sum of widths is treated as offset proof.
Only copied JSON facts leave the owner. Unsupported types remain explicit and
may use the caller's separate Source Graph fallback.
"""
from __future__ import annotations

from .hierarchy_source_ir import HierarchyBudgetExceeded


class LayoutUnavailable(ValueError):
    pass


def validate_packed_query(arguments):
    if not isinstance(arguments, dict) or set(arguments) != {"source_signal", "fields"}:
        raise ValueError("packed layout requires source_signal and fields")
    signal, fields = arguments["source_signal"], arguments["fields"]
    if not isinstance(signal, str) or not 1 <= len(signal) <= 16384:
        raise ValueError("source_signal must be a nonempty exact path")
    if (not isinstance(fields, list) or not 1 <= len(fields) <= 128
            or any(not isinstance(field, str) or not 1 <= len(field) <= 1024 for field in fields)
            or len(set(fields)) != len(fields)):
        raise ValueError("request 1..128 unique field names")


def query_packed_layout(provider, *, source_signal, fields, budget):
    from .npi_hierarchy_source import _LanguageIterator
    validate_packed_query(dict(source_signal=source_signal, fields=fields))
    if budget is not provider.budget:
        raise ValueError("a native owner uses one query budget")
    result = dict(schema_version=2, facts_origin=provider.origin,
        artifact_identity=provider.artifact_identity, coverage_status="partial",
        coverage_domain="requested_packed_fields", coverage_gaps=[], analysis_limits=[],
        query_metrics=budget.metrics(), semantic_status="partial", source_signal=source_signal,
        status="mapping_required", field_bits={}, declared_range=None)

    def need(value, reason):
        if value is None:
            raise LayoutUnavailable(reason)
        return value

    def native(fn, *args):
        provider.check()
        budget.call()
        value = fn(*args)
        budget.checkpoint()
        return value

    def relation(name, obj):
        return provider.wrap(native(provider.low.handle, getattr(provider.low, name), obj.cps_obj))

    def sequence(name, obj, cap):
        stream = _LanguageIterator(provider, getattr(provider.low, name), obj)
        values = []
        try:
            while True:
                value = stream.read()
                if value is None:
                    return values
                if len(values) >= cap:
                    raise HierarchyBudgetExceeded("packed_type_member_limit")
                budget.reserve_bytes(128)
                values.append(value)
        finally:
            provider.close_iterator(stream)

    def integer(obj):
        value = provider.scalar(need(obj, "npi_packed_range_unavailable"), "value")
        if type(value) is not int:
            raise LayoutUnavailable("npi_packed_range_unresolved")
        return value

    def struct_type(spec):
        if (spec is None or provider.scalar(spec, "type") != "npiStructTypespec"
                or provider.scalar(spec, "packed") != 1):
            raise LayoutUnavailable("npi_packed_struct_type_unavailable")

    def bit_handle(obj, index):
        return need(provider.wrap(native(provider.low.handle_by_index, obj.cps_obj, index)),
                    "npi_packed_bit_handle_unavailable")

    try:
        if provider.degraded:
            raise LayoutUnavailable("npi_degraded_design")
        provider._require_top()
        aggregate = need(provider.lookup(source_signal), "npi_packed_signal_unavailable")
        if provider.scalar(aggregate, "full_name") != source_signal or not provider._under_top(aggregate):
            raise LayoutUnavailable("npi_packed_signal_binding_unavailable")
        if provider.scalar(aggregate, "type") not in {"npiStructVar", "npiStructNet"}:
            raise LayoutUnavailable("npi_packed_aggregate_kind_unmodeled")
        spec = relation("Typespec", aggregate)
        struct_type(spec)
        width = provider.scalar(aggregate, "size")
        if type(width) is not int or width < 1:
            raise LayoutUnavailable("npi_packed_size_unavailable")
        # This identity-matrix algorithm is deliberately small. Wider types
        # can use the existing semantic projector, without quadratic native
        # work or a guessed sequential offset.
        if width > 256:
            raise LayoutUnavailable("npi_packed_identity_width_unsupported")
        budget.reserve_bytes(width * 128)
        root_bits = [bit_handle(aggregate, index) for index in range(width)]
        selected = {}
        for field in fields:
            current_type, current_path, current_obj = spec, source_signal, aggregate
            parts = field.split(".")
            if len(parts) > 32 or any(not part or "[" in part or "\\" in part for part in parts):
                raise LayoutUnavailable("npi_packed_member_path_unmodeled")
            for part in parts:
                struct_type(current_type)
                members = sequence("TypespecMember", current_type, 256)
                matches = [member for member in members if provider.scalar(member, "name") == part]
                if len(matches) != 1:
                    raise LayoutUnavailable("npi_packed_member_binding_unavailable")
                current_type = need(relation("Typespec", matches[0]), "npi_packed_member_type_unavailable")
                current_path += "." + part
                current_obj = need(provider.lookup(current_path), "npi_packed_member_handle_unavailable")
                if provider.scalar(current_obj, "full_name") != current_path:
                    raise LayoutUnavailable("npi_packed_member_path_mismatch")
            kind = provider.scalar(current_type, "type")
            if kind == "npiStructTypespec":
                struct_type(current_type)
                size = provider.scalar(current_obj, "size")
                if type(size) is not int or not 1 <= size <= width:
                    raise LayoutUnavailable("npi_packed_member_size_unavailable")
                indices = range(size - 1, -1, -1)
            elif kind in {"npiLogicTypespec", "npiBitTypespec"}:
                ranges = sequence("Range", current_type, 8)
                if len(ranges) != 1:
                    raise LayoutUnavailable("npi_packed_member_dimensions_unmodeled")
                left, right = (integer(relation(name, ranges[0])) for name in ("LeftRange", "RightRange"))
                if abs(left - right) + 1 > width:
                    raise LayoutUnavailable("npi_packed_member_range_mismatch")
                step = -1 if left > right else 1
                indices = range(left, right + step, step)
            else:
                raise LayoutUnavailable("npi_packed_member_kind_unmodeled")
            mapped = []
            for index in indices:
                bit = bit_handle(current_obj, index)
                aliases = []
                for root_index, root_bit in enumerate(root_bits):
                    equal = native(provider.low.compare_objects, bit.cps_obj, root_bit.cps_obj)
                    if type(equal) is not int or equal not in (0, 1):
                        raise LayoutUnavailable("npi_packed_bit_identity_unavailable")
                    if equal == 1:
                        aliases.append(root_index)
                if len(aliases) != 1 or aliases[0] in mapped:
                    raise LayoutUnavailable("npi_packed_bit_identity_ambiguous")
                budget.reserve_bytes(32)
                mapped.append(aliases[0])
            selected[field] = mapped
        budget.admit_payload(selected)
        result.update(status="resolved", field_bits=selected, declared_range={"left": width - 1, "right": 0},
                      coverage_status="complete", semantic_status="complete")
    except LayoutUnavailable as exc:
        result["coverage_gaps"] = [str(exc)]
    except HierarchyBudgetExceeded as exc:
        result["analysis_limits"] = [exc.reason]
    result["query_metrics"] = budget.metrics()
    return result
