"""Bounded default sampled expressions and symbolic history; never wave values."""
from __future__ import annotations

import re


_PURE={'IntegerLiteral','UnbasedUnsizedIntegerLiteral','NamedValue','HierarchicalValue',
    'UnaryOp','BinaryOp','ConditionalOp','Concatenation','Replication','ElementSelect',
    'RangeSelect','MemberAccess','Conversion'}


def sampled_default(projector, expr, clock):
    """IEEE 16.5.1: declaration initialization, then recursive component defaults.

    Fold only bounded integral expressions with no calls or effects. Initializers
    that read mutable objects remain symbolic rather than using their type default.
    """
    ast=projector.ast
    row=dict(basis='input_expression_default_sampled_value',status='symbolic',
        observation='static_source_default; not_a_runtime_value_or_wave_window_initial_value')
    def inspect(value, constants_only=False):
        names={};safe=True;count=0
        def visit(n):
            nonlocal safe,count
            projector.charge();count+=1
            if count>256 or n.kind.name not in _PURE or not n.type.isIntegral or n.type.bitWidth>4096:
                safe=False
                return ast.VisitAction.Skip
            if n.kind.name=='UnaryOp' and n.op.name in {'Preincrement','Predecrement','Postincrement','Postdecrement'}:
                safe=False
                return ast.VisitAction.Skip
            if n.kind.name in {'NamedValue','HierarchicalValue'}:
                sym=n.symbol
                if isinstance(sym,(ast.VariableSymbol,ast.NetSymbol)):
                    if constants_only:safe=False
                    names[str(sym.hierarchicalPath)]=sym
        value.visit(visit)
        return safe,names
    safe,names=inspect(expr)
    if not safe:
        row['unresolved_reason']='outside_bounded_pure_integral_subset'
        return row
    # EvalContext requires a scoped symbol, not a built-in type. A named clock
    # supplies the context even for a literal-only argument. No clock is evaluated.
    anchor=next(iter(names.values()),None)
    timing=projector.native_clocks.get(clock)
    if anchor is None and timing is not None and timing.kind.name=='SignalEvent':
        if timing.expr.kind.name in {'NamedValue','HierarchicalValue'}:
            anchor=timing.expr.symbol
    if anchor is None:
        row['unresolved_reason']='no_scoped_evaluation_context'
        return row
    context=ast.EvalContext(anchor)
    components=[]
    for path,sym in names.items():
        initializer=(sym.initializer if isinstance(sym,ast.VariableSymbol)
            and sym.lifetime==ast.VariableLifetime.Static else None)
        basis='static_declaration_initializer' if initializer is not None else 'variable_or_net_type_default'
        if initializer is not None:
            if not inspect(initializer,constants_only=True)[0]:
                row['unresolved_reason']='static_initializer_not_a_bounded_constant'
                return row
            value=initializer.eval(ast.EvalContext(sym))
        else:
            value=sym.type.defaultValue
        if not value:
            row['unresolved_reason']='frontend_default_evaluation_unavailable'
            return row
        context.createLocal(sym,value)
        components.append(dict(symbol=path,basis=basis,value=str(value)))
    value=expr.eval(context)
    if value:
        row.update(status='resolved',value=str(value),components=components)
    else:
        row['unresolved_reason']='frontend_default_evaluation_unavailable'
    return row


def first_sample_rules(name, default):
    if name not in {'$rose','$fell','$stable','$changed'}:
        return None
    # Restrict specialization to uniform zero/one/X defaults. Mixed packed defaults
    # and unrepresented types retain the ordinary sampled-function rules.
    literal=default.get('value','').lower()
    match=re.fullmatch(r"(\d+)'s?([bhd])([01x]+)",literal)
    if not match or len(set(match[3]))!=1:
        return None
    prior=match[3][0].upper()
    if prior=='1' and (match[2]!='b' or int(match[1])!=len(match[3])):
        return None
    predicate={
        '$rose':'always_false_with_prior_1' if prior=='1' else 'current_LSB_is_1',
        '$fell':'always_false_with_prior_0' if prior=='0' else 'current_LSB_is_0',
        '$stable':'all_current_bits_are_'+prior,
        '$changed':'not_all_current_bits_are_'+prior,
    }[name]
    return dict(scope='before_available_prior_sampling_event_on_this_call_clock; not_each_attempt_or_wave_window_start',
        basis='uniform_resolved_default_sampled_value',prior_bit=prior,true_iff=predicate,
        comparison='compare_0_1_X_Z_as_distinct_symbols; 0_vs_X_is_different_not_unknown',
        runtime_value='not_evaluated')
