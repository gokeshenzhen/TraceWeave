"""One-hop candidate inventory over existing typed backend steps, without values."""
from __future__ import annotations

import json
import time

from .cancellation import check_cancelled
from .dynamic_evidence import Expr, MAX_EXPR_WIDTH
from .scope_metadata import file_identity
from .wave_design_binding import bind_source_expression

LIMITS = dict(depth=1, selections=32, expression_nodes=256, output_bytes=32768, timeout_sec=10)


def unavailable(backend, reason):
    return dict(backend=backend, depth=1, complete=False, dependencies=[],
                gaps=[reason], truncated=False, active_branch='not_evaluated', limits=LIMITS)


def inventory(step, parser, *, binding=None, engine=None, deadline=None):
    """Candidate roles come from typed operators/pins, never signal names."""
    identity = file_identity(parser.file_path)
    deadline = deadline or time.monotonic() + LIMITS['timeout_sec']
    result = unavailable(step['backend'], 'dependency_inventory_incomplete')
    result.update(boundary=step.get('boundary', 'unsupported'), gaps=list(step.get('gaps', ())))
    refs = {}
    constants = []
    visited = 0
    root = step.get('state') or {}
    stopped = False

    def source_key(raw, active):
        return (raw['signal'], tuple(raw.get('bits', ())),
                tuple(raw.get('declared_bits', ())),
                tuple(tuple(d) for d in raw.get('dimensions', ())),
                tuple(raw.get('array_indices', ())), active)

    def walk(raw, role, active=None):
        nonlocal visited, stopped
        check_cancelled()
        if stopped:
            return
        visited += 1
        if visited > LIMITS['expression_nodes'] or time.monotonic() >= deadline:
            stopped = True
            result['truncated'] = True
            result['gaps'].append('dependency_budget_exhausted')
            return
        if raw.get('op') == 'signal':
            # Collect at most expression_nodes references before normalization.
            # Feedback belongs only to the exact intersecting bits.
            feedback = set(root.get('bits', ())) if raw['signal'] == root.get('signal') else set()
            for is_feedback in (False, True):
                bits = [b for b in raw.get('bits', ()) if (b in feedback) == is_feedback]
                if not bits:
                    continue
                selected = {**raw, 'bits': bits, 'width': len(bits)}
                key = source_key(selected, active)
                if key not in refs:
                    refs[key] = [selected, set(), active]
                refs[key][1].add(role)
                if is_feedback:
                    refs[key][1].add('feedback')
            return
        if raw.get('op') == 'const':
            fact = dict(width=raw['width'], value=raw['value'])
            if len(constants) < 8 and fact not in constants:
                constants.append(fact)
            return
        if raw.get('op') in {'unsupported', 'array', 'array_ref', 'array_select'}:
            result['gaps'].append(raw.get('reason') or 'dependency_expression_unmodeled')
        for i, child in enumerate(raw.get('args', ())):
            walk(child, 'control' if raw.get('op') == 'mux' and i == 0 else role)

    # Clock and explicit async pins are useful even when their assigned value
    # is unmodeled. Their roles/polarity come from the backend's typed facts.
    if step.get('clock'):
        walk(step['clock']['expression'], 'clock')
        result['edge'] = step['clock']['edge']
    for control in step.get('async_controls', ()):
        walk(control['expression'], control['kind'], control.get('active_value'))
    for expr in step.get('data_inputs', ()):
        walk(expr, 'data')
    for branch in step.get('branches', ()):
        if not (branch['guard'].get('op') == 'const' and branch['guard'].get('value') == '1'):
            walk(branch['guard'], 'control')
        walk(branch['value'], 'data')
    if not step.get('branches') and not step.get('data_inputs'):
        result['gaps'].append('dynamic_evidence_unavailable')

    groups = {}
    for raw, roles, active in refs.values():
        check_cancelled()
        if time.monotonic() >= deadline:
            result['truncated'] = True
            result['gaps'].append('dependency_budget_exhausted')
            break
        expr = Expr.from_dict(raw)
        row = dict(roles=sorted(roles), source=dict(path=expr.signal, bits=list(expr.bits)))
        if active is not None:
            row['active_value'] = active
        try:
            bound = bind_source_expression(parser, expr, binding, engine=engine)
            row.update(binding='bound', wave=dict(path=bound.signal, bits=list(bound.bits)))
        except KeyError:
            row.update(binding='not_dumped')
            result['gaps'].append('dependency_not_dumped')
        except ValueError:
            row.update(binding='unresolved')
            result['gaps'].append('dependency_binding_unresolved')
        # Bind before grouping: different source aliases, dump declarations,
        # shapes, roles and polarities cannot consume each other's coverage.
        # Non-identity packed-member mappings stay separate, preserving their
        # ordered source-to-wave pairing without assuming a linear mapping.
        key = (expr.signal, expr.declared_bits, expr.dimensions, expr.array_indices,
               tuple(row['roles']), active, row['binding'])
        if row['binding'] == 'bound':
            key += (bound.signal, bound.declared_bits)
        if row['binding'] != 'bound' or expr.bits != bound.bits:
            key += (expr.bits,)
        existing = groups.get(key)
        added = [b for b in expr.bits if existing is None or b not in existing[1]]
        if existing is not None and len(existing[1]) + len(added) > MAX_EXPR_WIDTH:
            key += (expr.bits,)
            existing = groups.get(key)
            added = [b for b in expr.bits if existing is None or b not in existing[1]]
        if existing is None:
            if len(result['dependencies']) >= LIMITS['selections']:
                result['truncated'] = True
                result['gaps'].append('dependency_selection_limit')
                break
            result['dependencies'].append(row)
            groups[key] = (row, set(expr.bits))
        else:
            row, seen = existing
            previous_width = len(row['source']['bits'])
            row['source']['bits'].extend(added)
            row['wave']['bits'].extend(added)
            seen.update(added)
        if len(json.dumps(result).encode()) > LIMITS['output_bytes'] - 2048:
            if existing is None:
                result['dependencies'].pop()
            else:
                del row['source']['bits'][previous_width:]
                del row['wave']['bits'][previous_width:]
            result['truncated'] = True
            result['gaps'].append('dependency_output_limit')
            break
    for row in result['dependencies']:
        check_cancelled()
        if row.get('wave') == row['source']:
            del row['source']  # Same coordinates; one directly usable selection.
    if constants:
        # Literal operands are structural facts, not observed active values.
        result['literal_operands'] = constants
    result['gaps'] = sorted(set(result['gaps']))
    result['complete'] = bool(step.get('complete')) and not result['gaps'] and not result['truncated']
    if identity != file_identity(parser.file_path):
        raise ValueError('wave_design_binding_changed')
    if len(json.dumps(result).encode()) > LIMITS['output_bytes']:
        limited = unavailable(step['backend'], 'dependency_output_limit')
        limited['truncated'] = True
        return limited
    return result
