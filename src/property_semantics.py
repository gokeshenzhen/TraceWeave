"""Bounded projection of bound Slang assertions into a symbolic temporal graph.

Imported only in the optional frontend worker. This is not a waveform evaluator:
endpoints, alternatives and universal obligations remain symbolic.
"""
from __future__ import annotations

import time
from .property_records import gap


class ProjectionLimit(Exception):
    pass


def kind(node):
    return getattr(getattr(node, 'kind', None), 'name', '')


def spelling(node):
    syntax = getattr(node, 'syntax', None)
    if syntax is None and type(node).__module__.endswith('.syntax'):
        syntax = node
    return str(syntax).strip() if syntax is not None else ''


def type_info(typ):
    return dict(name=str(typ), width=int(typ.bitWidth), signed=bool(typ.isSigned),
                four_state=bool(typ.isFourState))


class Projector:
    def __init__(self, manager, result, limits, *, deadline=None):
        from pyslang import ast
        self.ast = ast
        self.manager, self.result, self.limits = manager, result, limits
        self.deadline = deadline or time.monotonic() + 20
        self.visits = 0
        self.active_refs = {}
        self.active_calls = set()
        self.variables = {}
        self.clock_ids = {}
        self.native_clocks = {}
        self.empty_repetition_subtrees = set()
        self.execution_requested = False
        self.history_sequence = 0

    def charge(self, depth=0):
        self.visits += 1
        if time.monotonic() >= self.deadline:
            raise ProjectionLimit('item_timeout')
        if self.visits > max(1024, self.limits['max_nodes'] * 16):
            raise ProjectionLimit('ast_visit_limit')
        if depth > self.limits['max_depth']:
            raise ProjectionLimit('depth_limit')

    def source(self, node):
        syntax = getattr(node, 'syntax', None)
        sr = getattr(syntax, 'sourceRange', None) or getattr(node, 'sourceRange', None)
        if sr is None:
            loc = getattr(node, 'location', None)
            if loc:
                loc = self.manager.getFullyOriginalLoc(loc)
                return dict(file=str(self.manager.getFileName(loc)), line=int(self.manager.getLineNumber(loc)),
                            column=int(self.manager.getColumnNumber(loc)), offset=int(loc.offset), text=spelling(node))
            return {'text': spelling(node)}
        m = self.manager
        loc = m.getFullyOriginalLoc(sr.start)
        end = m.getFullyOriginalLoc(sr.end)
        return dict(file=str(m.getFileName(loc)), line=int(m.getLineNumber(loc)),
                    column=int(m.getColumnNumber(loc)), offset=int(loc.offset),
                    end_offset=int(end.offset), offset_basis='frontend_buffer', text=spelling(node))

    def missing(self, code, category, message, node, recovery=None):
        row = gap(code, category, message, recovery or
                  'Inspect this source fragment and its definitions; label any additional AI reasoning as inference.',
                  self.source(node) if node is not None else {})
        if row not in self.result['gaps']:
            self.result['gaps'].append(row)

    def add(self, node, op, domain, clock, facts=None, explanation=''):
        self.charge()
        if len(self.result['nodes']) >= self.limits['max_nodes']:
            raise ProjectionLimit('node_limit')
        out = dict(id=f'n{len(self.result["nodes"])}', domain=domain, op=op,
                   source=self.source(node), children=[], clock=clock, facts=facts or {},
                   explanation=explanation)
        self.result['nodes'].append(out)
        return out

    def operator_domain(self, expr, context):
        """Preserve the frontend's sequence/property distinction through wrappers.

        A sequence used as a property consequent is still a sequence. Slang
        attaches its outer property-spec / parenthesized syntax to the bound
        node, so inspect those wrappers before falling back to call context.
        """
        from pyslang import syntax
        node = expr.syntax
        depth = 0
        while node is not None and kind(node) in {
                'PropertySpec', 'SimplePropertyExpr', 'ParenthesizedPropertyExpr'}:
            self.charge(depth)
            node = node.expr
            depth += 1
        if isinstance(node, syntax.SequenceExprSyntax):
            return 'sequence', 'frontend_sequence_syntax'
        if isinstance(node, syntax.PropertyExprSyntax):
            return 'property', 'frontend_property_syntax'
        return context, 'enclosing_projection_context'

    @staticmethod
    def operand_label(expr):
        text = ' '.join(spelling(expr).split())
        return '`' + (text[:160] + '...' if len(text) > 160 else text) + '`'

    def summary(self):
        from .property_summary import summarize_property
        return summarize_property(self.result)

    @staticmethod
    def operand_label_text(text):
        text = ' '.join(text.split())
        return text[:200] + '...' if len(text) > 200 else text

    def clock(self, timing, inherited_from=None):
        text = spelling(timing)
        if not text:
            text = kind(timing)
        key = text + '|' + str(self.source(timing).get('file'))
        if key in self.clock_ids:
            return self.clock_ids[key]
        name = f'c{len(self.result["clocks"])}'
        row = dict(id=name, event=text, source=self.source(timing), inherited_from=inherited_from,
                   sampling='preponed_sampled_values; evaluation_in_observed_region',
                   physical_wave_pre_edge_equivalence='not_assumed')
        if kind(timing) == 'SignalEvent':
            row.update(edge=timing.edge.name, expression=self.value(timing.expr, name),
                       iff=self.value(timing.iffCondition, name) if timing.iffCondition else None)
            # Canonical resolved event identity, independent of spelling / call site.
            key = str((timing.edge.name, row['expression']['reads'], row['expression']['text'],
                       row['iff']))
            if key in self.clock_ids:
                return self.clock_ids[key]
        else:
            self.missing('clock_event_projection', 'unsupported',
                         'Compound or implicit event is retained; event alignment is unresolved.', timing)
        self.clock_ids[key] = name
        self.native_clocks[name] = timing
        self.result['clocks'].append(row)
        return name

    def qualified_event(self, clock):
        timing = self.native_clocks[clock]
        if kind(timing) != 'SignalEvent':
            raise ValueError('definition_clock_context_required')
        def qualified(expr):
            if expr is None: return ''
            sr=expr.sourceRange
            raw=self.manager.getSourceText(sr.start.buffer).encode()
            start,end=int(sr.start.offset),int(sr.end.offset)
            text=raw[start:end]; replacements=[]
            def visit(n):
                self.charge()
                if kind(n) in {'NamedValue','HierarchicalValue'}:
                    r=n.sourceRange
                    if r.start.buffer!=sr.start.buffer:
                        raise ValueError('definition_clock_context_required')
                    replacements.append((int(r.start.offset)-start,int(r.end.offset)-start,str(n.symbol.hierarchicalPath).encode()))
            expr.visit(visit)
            for a,b,value in sorted(set(replacements),reverse=True): text=text[:a]+value+text[b:]
            return text.decode()
        edge={'PosEdge':'posedge ', 'NegEdge':'negedge ', 'BothEdges':'edge ', 'None_':''}.get(timing.edge.name,'')
        return edge+qualified(timing.expr)+(' iff '+qualified(timing.iffCondition) if timing.iffCondition else '')

    def symbols(self, expr):
        names = set()
        if expr is None:
            return []
        def visit(n):
            self.charge()
            if kind(n) in {'NamedValue', 'HierarchicalValue'}:
                names.add(str(n.symbol.hierarchicalPath or n.symbol.name))
            if kind(n) == 'AssertionInstance':
                return self.ast.VisitAction.Skip
        expr.visit(visit)
        return sorted(names)

    def lvalue(self, expr):
        """Separate assigned objects from index/address dependencies."""
        self.charge()
        k = kind(expr)
        if k in {'NamedValue', 'HierarchicalValue'}:
            return self.symbols(expr), []
        if k in {'ElementSelect', 'RangeSelect', 'MemberAccess'}:
            writes, reads = self.lvalue(expr.value)
            selectors = ([expr.selector] if k == 'ElementSelect' else
                         [expr.left, expr.right] if k == 'RangeSelect' else [])
            return writes, sorted(set(reads) | {s for e in selectors for s in self.symbols(e)})
        if k == 'Concatenation':
            parts = [self.lvalue(e) for e in expr.operands]
            return sorted({s for w, r in parts for s in w}), sorted({s for w, r in parts for s in r})
        self.missing('lvalue_effects_incomplete', 'unsupported', 'Assigned object could not be resolved statically.', expr)
        return [], self.symbols(expr)

    def value(self, expr, clock=None, depth=0):
        if expr is None:
            return None
        self.charge(depth)
        out = dict(text=spelling(expr), kind=kind(expr), type=type_info(expr.type),
                   reads=self.symbols(expr), clock=clock)
        calls = []
        def visit(n):
            self.charge(depth)
            if kind(n) == 'Call':
                calls.append(self.call(n, clock, depth+1))
                return self.ast.VisitAction.Skip
            if kind(n) == 'AssertionInstance':
                return self.ast.VisitAction.Skip
        expr.visit(visit)
        if calls:
            out['calls'] = calls
        if kind(expr) == 'Invalid':
            self.missing('unbound_value', 'context', 'Value expression could not be bound.', expr,
                         'Supply the original declarations, macros, parameter values and clock in a shared unit or compile context.')
        if self.execution_requested and expr.type.isIntegral:
            from .property_value_projection import project_value
            out['execution']=project_value(self,expr,clock)
        return out

    def call(self, expr, clock, depth):
        self.charge(depth)
        name = str(expr.subroutineName)
        args = list(expr.arguments)
        row = dict(name=name, arguments=[self.value(e, clock, depth+1) for e in args],
                   source=self.source(expr), clock=clock, return_type=type_info(expr.type))
        sampled = {
            '$rose': dict(rule='lsb_previous_in_0_X_Z_and_current_1', history_ticks=1, vector_rule='least_significant_bit'),
            '$fell': dict(rule='lsb_previous_in_1_X_Z_and_current_0', history_ticks=1, vector_rule='least_significant_bit'),
            '$stable': dict(rule='all_bits_equal_including_X_Z', history_ticks=1, vector_rule='entire_expression'),
            '$changed': dict(rule='any_bit_differs_including_X_Z', history_ticks=1, vector_rule='entire_expression'),
            '$sampled': dict(rule='current_sampled_value', history_ticks=0),
            '$past': dict(rule='value_at_prior_qualifying_sampling_event', default_ticks=1,
                          gate='third_argument_if_present; count_only_events_where_gate_is_true'),
        }
        if name in sampled:
            row['semantics'] = sampled[name]
            row['history_initialization'] = ('Current sampled value; no prior history is required.' if name == '$sampled' else
                'Before available history, use the INPUT EXPRESSION default sampled value from declaration initializers and component defaults, not the function return type; no runtime history was read.')
            if name != '$sampled' and args:
                typ = args[0].type
                from .property_history import sampled_default,first_sample_rules
                default=sampled_default(self,args[0],clock)
                default['type']=type_info(typ)
                row['history_default'] = default
                first=first_sample_rules(name,default)
                if first:
                    row['first_sample']=first
            row['explicit_clock'] = spelling(args[-1]) if args and kind(args[-1]) == 'ClockingEvent' else None
            row['purity'] = 'sampled_value_function'
        elif expr.isSystemCall:
            row['purity'] = 'system_function_or_task'
            if name in {'$bits', '$isunknown', '$onehot', '$onehot0', '$countones', '$countbits', '$clog2', '$isunbounded'}:
                row['semantics'] = {'rule': 'SystemVerilog_' + name[1:], 'arguments_and_types_preserved': True}
            elif name in {'$realtime', '$time', '$stime'}:
                row['semantics'] = {'rule': 'simulation_time_in_enclosing_timeunit; not_cycle_count'}
                row['formal_model_support'] = 'requires_external_tool_configuration_evidence'
            elif name.startswith(('$assert', '$display', '$error', '$warning', '$info', '$fatal')):
                row['semantics'] = {'rule': 'procedural_control_or_reporting; existence_does_not_prove_execution'}
            else:
                self.missing('system_call_semantics', 'unsupported', 'System call effects are not modeled.', expr)
        else:
            sub = expr.subroutine
            path = str(sub.hierarchicalPath)
            row['definition'] = self.source(sub)
            row['bindings'] = [{'formal': p.name, 'actual': row['arguments'][i] if i < len(args) else None}
                               for i, p in enumerate(sub.arguments)]
            if any(p.direction.name != 'In' for p in sub.arguments):
                self.missing('function_argument_effects', 'unsupported',
                             'Output, inout or ref function argument effects require additional alias analysis.', expr)
            if path in self.active_calls:
                row['purity'] = 'unknown_recursive_call'
                self.missing('recursive_function_effects', 'unsupported', 'Recursive function body is not unfolded.', expr)
                return row
            self.active_calls.add(path)
            try:
                effects = self.effects(sub.body, 'function_body', 'when_call_executes', clock, depth+1, publish=False)
                row['effects'] = effects
                writes = {w for e in effects for w in e.get('writes', [])}
                reads = {r for e in effects for r in e.get('reads', [])}
                params = {str(p.hierarchicalPath) for p in sub.arguments}
                returns = [e for e in effects if e['kind'] == 'return']
                # Only a straight single-return automatic function with argument-only reads.
                pure = (str(sub.defaultLifetime.name) == 'Automatic' and not writes
                        and all(p.direction.name == 'In' for p in sub.arguments)
                        and reads <= params and len(effects) == len(returns) == 1
                        and not returns[0]['expression'].get('calls'))
                row['purity'] = 'pure_single_return' if pure else ('stateful' if writes else 'not_proven_pure')
                if pure:
                    row['return_expression'] = returns[0]['expression']
                elif any(e.get('incomplete') for e in effects) or not effects:
                    self.missing('function_effects_incomplete', 'unsupported', 'Function effects cannot be completely summarized.', expr)
            finally:
                self.active_calls.remove(path)
        return row

    def effects(self, obj, phase, condition, clock, depth=0, *, publish=True):
        self.charge(depth)
        if obj is None or kind(obj) == 'Empty':
            return []
        k = kind(obj)
        rows = []
        base = dict(phase=phase, condition=condition, source=self.source(obj), reads=[], writes=[])
        if k in {'List', 'Block'}:
            children = obj.list if k == 'List' else [obj.body]
            for child in children:
                rows.extend(self.effects(child, phase, condition, clock, depth+1, publish=False))
        elif k == 'ExpressionStatement':
            rows.extend(self.effects(obj.expr, phase, condition, clock, depth+1, publish=False))
        elif k == 'Assignment':
            value = self.value(obj.right, clock, depth+1)
            writes, address_reads = self.lvalue(obj.left)
            reads = sorted(set(value['reads']) | set(address_reads) | (set(writes) if obj.isCompound else set()))
            rows.append(dict(base, kind='assignment', writes=writes, reads=reads,
                             target=self.value(obj.left, clock, depth+1), expression=value,
                             nonblocking=bool(obj.isNonBlocking), compound=bool(obj.isCompound)))
        elif k == 'UnaryOp' and ('Increment' in obj.op.name or 'Decrement' in obj.op.name):
            writes, address_reads = self.lvalue(obj.operand)
            rows.append(dict(base, kind='update', writes=writes, reads=sorted(set(writes+address_reads)),
                             expression=self.value(obj, clock, depth+1)))
        elif k == 'Return':
            value = self.value(obj.expr, clock, depth+1)
            rows.append(dict(base, kind='return', expression=value, reads=value['reads'] if value else []))
        elif k == 'Call':
            call = self.call(obj, clock, depth+1)
            effects = call.get('effects', [])
            rows.append(dict(base, kind='call', call=call, reads=sorted(set(self.symbols(obj)) |
                             {r for e in effects for r in e.get('reads', [])}),
                             writes=sorted({w for e in effects for w in e.get('writes', [])})))
        elif k == 'Conditional':
            cond = ' && '.join(spelling(c.expr) for c in obj.conditions)
            rows.append(dict(base, kind='branch_condition', reads=sorted({s for c in obj.conditions for s in self.symbols(c.expr)}),
                             expressions=[self.value(c.expr, clock, depth+1) for c in obj.conditions]))
            rows.extend(self.effects(obj.ifTrue, phase, f'({condition}) && ({cond})', clock, depth+1, publish=False))
            rows.extend(self.effects(obj.ifFalse, phase, f'({condition}) && !({cond})', clock, depth+1, publish=False))
        else:
            rows.append(dict(base, kind=k or 'unknown', incomplete=True))
            self.missing('procedural_effects_incomplete', 'unsupported', 'Procedural construct retained without complete read/write/control analysis.', obj)
        if publish:
            self.result['effects'].extend(rows)
        return rows

    def project(self, expr, domain='property', clock=None, depth=0, conditions=()):
        self.charge(depth)
        k = kind(expr)
        out = self.add(expr, k, domain, clock)
        facts = out['facts']
        def child(e, d=domain, c=clock, cond=conditions):
            result = self.project(e, d, c, depth+1, cond)
            out['children'].append(result['id'])
            return result
        if k == 'Simple':
            value = expr.expr
            if kind(value) == 'AssertionInstance':
                path = str(value.symbol.hierarchicalPath)
                out['op'] = 'reference'
                out['domain'] = 'sequence' if kind(value.symbol) == 'Sequence' else 'property'
                bindings = [{'formal': p.name, 'formal_symbol': str(p.hierarchicalPath),
                             'local': bool(p.isLocalVar), 'direction': p.direction.name if p.direction is not None else None,
                             'actual': self.value(v, clock, depth+1)
                             if isinstance(v, self.ast.Expression) else {'text': spelling(v), 'kind':kind(v)}}
                            for p, v in value.arguments]
                ref = dict(node=out['id'], symbol=path, bindings=bindings, conditions=list(conditions),
                           definition=self.source(value.symbol), recursive=bool(value.isRecursiveProperty),
                           clock=clock)
                self.result['references'].append(ref)
                facts.update(symbol=path, bindings=bindings)
                for var in value.localVars:
                    key = f'{out["id"]}:{var.name}'
                    row = dict(id=key, name=var.name, type=type_info(var.type), source=self.source(var),
                               symbol=str(var.hierarchicalPath),
                               owner=out['id'], lifetime='one_attempt_and_its_matching_branches',
                               branch_flow='copies_on_alternatives; definite_assignment_checked_by_temporal_projection')
                    self.variables[key] = row
                if value.isRecursiveProperty or path in self.active_refs:
                    ref['recursive'] = True
                    ref['back_to'] = self.active_refs.get(path)
                    facts['back_to'] = ref['back_to']
                    facts['progress_conditions'] = list(conditions)
                    out['explanation'] = 'Recursive reference retains its arguments and guarded clock progression; no finite expansion proves termination or truth.'
                else:
                    self.active_refs[path] = out['id']
                    try:
                        child(value.body, out['domain'])
                    finally:
                        self.active_refs.pop(path, None)
                    out['explanation'] = 'Bind the named definition in this scope with these arguments; local state is private to each attempt.'
            else:
                out['op'] = 'sample'
                if clock is None:
                    self.missing('clock_unresolved','context',
                                 'This sequence value has no inherited sampling event; clocks do not flow out of parentheses or named instances.',expr,
                                 'Provide an enclosing/default clock or an explicit event on this segment.')
                facts['expression'] = self.value(value, clock, depth+1)
                facts.update(match='one_tick_when_expression_true', unknown='X_or_Z_boolean_result_is_not_a_match',
                             endpoints='end=start')
                out['explanation'] = 'Evaluate the typed value at this sampling event; a true result is a one-tick sequence match.'
            self.repetition(expr.repetition, out)
        elif k == 'Clocking':
            ck = self.clock(expr.clocking, clock)
            out.update(op='clock', clock=ck)
            facts.update(event=ck, inherited_clock=clock, scope='contained_expression')
            child(expr.expr, domain, ck)
            out['explanation'] = 'The explicit event supplies the clock for the contained expression and its inherited delays.'
        elif k == 'SequenceConcat':
            out.update(op='concat', domain='sequence')
            facts['joins'] = []
            prior = None
            for i, element in enumerate(expr.elements):
                part = child(element.sequence, 'sequence')
                delay = self.range(element.delay)
                if i or delay['min']:
                    facts['joins'].append(dict(left=prior, right=part['id'], delay=delay,
                        same_clock='start(right)=end(left)+delay ticks; initial delay is relative to attempt start',
                        cross_clock='##0: nearest destination event at_or_after left end; ##1: nearest strictly_after; other delays across clocks are illegal',
                        empty='empty ##0 sequence and sequence ##0 empty have no match; for N>0 an empty operand ends at start-1, so either side shifts by N-1',
                        right_clock=self.leading_clock(part)))
                prior = part['id']
            facts['match_quantifier'] = 'all_legal_combinations_of_component_matches_and_delay_choices'
            facts['empty_join_rules'] = dict(
                scope='same_clock; N_is_the_selected_delay',
                empty_endpoint='end=start-1',
                left_empty_positive_delay='right.start=attempt.start+N-1',
                right_empty_positive_delay='result.end=left.end+N-1',
                both_empty_positive_delay='duration_ticks=N-1; N=1_is_empty; N>=2_spans_ticks_without_a_boolean_sample',
                zero_delay_with_empty='no_match')
            out['explanation'] = 'Concatenation relates each next start to the preceding match end. Ranges preserve alternatives instead of selecting one delay.'
            if any(c in self.empty_repetition_subtrees for c in out['children']):
                out['explanation'] += (' An empty operand ends at start-1. For N>0, empty ##N empty spans N-1 ticks; '
                                       'empty ##2 empty is a one-tick match without requiring a true value. An empty operand joined by ##0 has no match.')
        elif k == 'Binary':
            op = expr.op.name
            out['op'] = op
            if op in {'OverlappedImplication', 'NonOverlappedImplication'}:
                out['domain'] = 'property'
                ant = child(expr.left, 'sequence')
                nonoverlap = op == 'NonOverlappedImplication'
                consequent = child(expr.right, 'property', cond=(*conditions, dict(
                    antecedent=ant['id'], match='every_match', advance_ticks=1 if nonoverlap else 0, clock=clock)))
                facts.update(antecedent=ant['id'], consequent=consequent['id'],
                    obligation_quantifier='for_every_antecedent_match',
                    consequent_sequence_choices='exists_a_successful_match_for_each_obligation',
                    vacuity='no_antecedent_match_is_vacuous_success',
                    when_no_antecedent_match='property_succeeds_vacuously; no_consequent_obligation',
                    start_relation='next_tick_after_antecedent_end' if nonoverlap else 'antecedent_end_tick',
                    destination_clock=self.leading_clock(consequent),
                    cross_clock_alignment='nearest_strictly_after' if nonoverlap else 'nearest_at_or_after')
                facts['obligation_scope'] = 'each_antecedent_match_within_one_attempt; activation_is_separate'
                facts['antecedent_start']='fixed_at_this_implication_start; no_implicit_search_for_later_true_ticks'
                out['explanation'] = ('Within ONE attempt, every antecedent match creates a separate consequent obligation. '
                    'If the antecedent has no match, the implication succeeds vacuously and creates no consequent obligation. '
                    'Each consequent may choose a successful sequence alternative. On the destination clock, start at the nearest event '
                    + ('strictly after' if nonoverlap else 'at or after') + ' the antecedent endpoint. Attempt activation is separate from these match choices.')
                ant_sample=self.single_sample(ant)
                con_sample=self.single_sample(consequent)
                if ant_sample:
                    facts['single_sample_antecedent']=dict(node=ant_sample['id'],tick='this_supplied_implication_start',
                        no_match='vacuous_completion_here; not_waiting_for_a_later_true_antecedent')
                    out['explanation'] += (' The single-sample antecedent is checked ONLY at this supplied implication start. '
                        'If its Boolean result is false/X/Z, this implication finishes vacuously here; it does not wait until a later true tick.')
                    if (not nonoverlap and con_sample and ant_sample['clock'] is not None
                            and ant_sample['clock']==con_sample['clock']):
                        facts['single_tick_implication']=dict(clock=ant_sample['clock'],sample_scope='this_supplied_start_only',
                            true_antecedent='consequent_required_now',false_X_Z_antecedent='vacuous_success_now',
                            future_ticks='do_not_recheck_this_completed_implication; another_start_requires_separate_activation')
                        out['explanation'] += (' Both operands are single samples on the SAME clock: if the antecedent is true, '
                            'check the consequent NOW and finish this implication. Do not scan later ticks for this start; '
                            'additional starts require separate activation or an enclosing property operator.')
            elif op in {'And', 'Or', 'Intersect', 'Throughout', 'Within'}:
                resolved, basis = self.operator_domain(expr, domain)
                seq = resolved == 'sequence' or op in {'Intersect', 'Throughout', 'Within'}
                out['domain'] = 'sequence' if seq else 'property'
                facts['domain_basis'] = basis
                left, right = child(expr.left, out['domain']), child(expr.right, out['domain'])
                facts.update(left=left['id'], right=right['id'])
                if op == 'And':
                    facts.update(start_relation='same_start', success='both',
                        requires_same_start=True, requires_same_end=False,
                        end_relation='max(left.end,right.end)' if seq else 'independent_property_completion',
                        local_flow='variables_assigned_in_both_parallel_threads_cannot_flow_out; each_thread_keeps_its_state')
                    out['explanation'] = ('AND requires both ' + self.operand_label(expr.left) + ' and '
                        + self.operand_label(expr.right) + ' to succeed from the same start. '
                        + 'Their end ticks MAY DIFFER (requires_same_end=false). '
                        + ('The combined sequence ends at max(left.end,right.end).' if seq else
                           'Each property has its own completion; combine their truth values.'))
                elif op == 'Or':
                    facts.update(start_relation='same_start', success='either',
                        matches='union_of_branch_matches' if seq else 'disjunction_of_property_truth',
                        local_flow='retain_branch_private_values; a_variable_used_after_or_must_be_assigned_on_each_possible_flow_path')
                    out['explanation'] = 'Either branch can succeed; preserve branch identity and local values, including ties at the same endpoint.'
                elif op == 'Intersect':
                    facts.update(start_relation='same_start', end_relation='same_end', success='both',
                                 requires_same_start=True, requires_same_end=True)
                    out['explanation'] = ('INTERSECT requires both ' + self.operand_label(expr.left)
                        + ' and ' + self.operand_label(expr.right)
                        + ' to match with the SAME start AND end ticks (requires_same_end=true).')
                elif op == 'Throughout':
                    facts.update(interval=right['id'], requirement='left_true_at_every_tick_of_right_match',
                                 right_sequence_match='required',
                                 vacuity='no_implication_style_vacuous_success_from_absent_right_match',
                                 equivalent='left[*0:$] intersect right')
                    out['explanation'] = ('The right sequence must match, and the left value must hold at every sampling point '
                        'of that match, including both endpoints. An absent right match does not give implication-style vacuous success.')
                else:
                    facts.update(start_relation='outer.start<=inner.start', end_relation='inner.end<=outer.end',
                                 inner=left['id'], outer=right['id'], result_interval=right['id'],
                                 match_preconditions='both_inner_and_outer_sequences_must_actually_match_their_sampled_values',
                                 equivalent='(1[*0:$] ##1 inner ##1 1[*0:$]) intersect outer')
                    out['explanation'] = ('Both inner and outer sequences must actually match their sampled values. '
                        'Choose an inner match contained inclusively in an outer match; the resulting interval is the outer interval.')
            elif op in {'Iff', 'Implies'}:
                child(expr.left, 'property'); child(expr.right, 'property')
                facts.update(truth_rule='equivalent_truth' if op == 'Iff' else 'not_left_or_right',
                             timing='both_properties_start_here; not_sequence_implication')
                out['explanation'] = ('Both properties start here; combine their property truth using '
                    + ('equivalence.' if op == 'Iff' else 'not-left or right.'))
            elif op in {'Until','SUntil','UntilWith','SUntilWith'}:
                child(expr.left, 'property'); child(expr.right, 'property')
                facts.update(left_required='through_right_success_tick' if op.endswith('With') else 'before_right_success_tick',
                             right_eventually_required=op.startswith('S'), start_relation='same_start')
                out['explanation'] = ('From this start, require the left property '
                    + ('through' if op.endswith('With') else 'before') + ' the right-success tick. '
                    + ('Right success must eventually occur.' if op.startswith('S') else 'The weak form does not require eventual right success.'))
            else:
                child(expr.left); child(expr.right)
                self.missing('binary_operator_semantics', 'unsupported', f'No semantic rule for {op}.', expr)
        elif k in {'SequenceWithMatch', 'FirstMatch'}:
            out.update(op='first_match' if k == 'FirstMatch' else 'match_items', domain='sequence')
            target = child(expr.seq if k == 'FirstMatch' else expr.expr, 'sequence')
            facts['operand'] = target['id']
            if k == 'FirstMatch':
                facts.update(selection='minimum_end_for_this_attempt_and_operand', ties='retain_all_matching_branches_at_earliest_end',
                             scope='only_the_enclosed_sequence; no_global_earliest_attempt')
                out['explanation'] = ('The enclosed sequence must match before an endpoint can be selected. '
                    'Keep its earliest endpoint for this attempt. Preserve all tied matching branches and their local values.')
            facts['match_items'] = []
            for item in expr.matchItems:
                phase = 'reactive_subroutine_call' if kind(item) == 'Call' else 'observed_local_assignment'
                facts['match_items'].extend(self.effects(item, phase, f'on_match({target["id"]})', clock, depth+1))
            if k == 'SequenceWithMatch':
                if facts['match_items']:
                    out['explanation'] = ('Apply the recorded match-item effects on successful operand matches, in their recorded phases; '
                        'each matching branch keeps its local values. Effects: '
                        + '; '.join(self.operand_label_text(e['source'].get('text', '')) for e in facts['match_items'][:4]))
                self.repetition(expr.repetition, out)
        elif k == 'DisableIff':
            out['op'] = 'disable_iff'
            facts.update(condition=self.value(expr.condition, clock, depth+1),
                sampling='asynchronous_unsampled_condition; sampled_functions_keep_their_own_sampling',
                outcome='disabled_attempt_neither_passes_nor_fails', affects='active_and_new_attempts')
            child(expr.expr)
            out['explanation'] = ('When the disable condition evaluates true (Boolean 1), abort the attempt asynchronously. '
                                  'A false condition permits checking; disabled is separate from failure, vacuity and unfinished checks.')
        elif k == 'Abort':
            out['op'] = ('sync_' if expr.isSync else '') + expr.action.name.lower() + '_on'
            facts.update(condition=self.value(expr.condition, clock, depth+1),
                         sampling='sampled_on_clock' if expr.isSync else 'asynchronous',
                         outcome='success' if expr.action.name == 'Accept' else 'failure')
            child(expr.expr)
            out['explanation'] = ('When the condition is true, '
                + ('synchronously ' if expr.isSync else 'asynchronously ')
                + ('accept' if expr.action.name == 'Accept' else 'reject') + ' the contained attempt; otherwise continue evaluating it.')
        elif k == 'StrongWeak':
            out['op'] = expr.strength.name.lower()
            child(expr.expr, 'sequence')
            facts.update(completion='nonempty_match_required' if out['op']=='strong' else 'no_finite_bad_prefix',
                unfinished='strong_sequence_not_satisfied_without_completion' if out['op']=='strong' else 'finite_end_without_match_is_not_a_counterexample_only_if_no_finite_bad_prefix',
                observation='tool_does_not_determine_runtime_pending_or_finish_callbacks')
            out['explanation'] = 'Strong requires a nonempty completing sequence match.' if out['op']=='strong' else 'Weak requires absence of a finite prefix that makes matching impossible; finite truncation is not a completed match.'
        elif k == 'Conditional':
            out['op'] = 'if'
            condition = self.value(expr.condition, clock, depth+1)
            facts.update(condition=condition, selection='true_selects_then; false_X_Z_select_else',
                         missing_else='vacuous_success')
            child(expr.ifExpr, 'property', cond=(*conditions, {'if':condition['text']}))
            if expr.elseExpr:
                child(expr.elseExpr, 'property', cond=(*conditions, {'else':condition['text']}))
            out['explanation'] = 'Select a property branch using the sampled condition at the start; an omitted else is true.'
        elif k == 'Unary':
            out['op'] = expr.op.name
            progression=conditions
            if expr.op.name in {'NextTime','SNextTime'}:
                progression=(*conditions,{'operator':expr.op.name,'advance_ticks':int(expr.range.min) if expr.range else 1,'clock':clock})
            child(expr.expr, 'property', cond=progression)
            facts.update(range=self.range(expr.range) if expr.range is not None else None,
                         rule={'Not':'negate_property_truth', 'NextTime':'weak_next_sampling_event',
                               'SNextTime':'strong_next_sampling_event', 'Always':'every_tick_in_range',
                               'SAlways':'strong_every_tick_in_finite_range', 'Eventually':'some_tick_in_finite_range',
                               'SEventually':'eventual_completion_required'}.get(expr.op.name))
            if facts['rule'] is None:
                self.missing('unary_operator_semantics','unsupported','Unary operator rule is unavailable.',expr)
            else:
                out['explanation'] = ({
                    'Not': 'Negate the contained property truth for THIS same start; preserve its temporal matching and completion rules.',
                    'NextTime': 'Evaluate the property at the recorded later sampling tick; this is weak nexttime.',
                    'SNextTime': 'Evaluate the property at the recorded later sampling tick; this is strong nexttime.',
                    'Always': 'Require the property at every tick in the recorded range.',
                    'SAlways': 'Require the property at every tick in the recorded finite range with strong completion.',
                    'Eventually': 'Require a property success at some tick in the recorded finite range.',
                    'SEventually': 'Require eventual property success with strong completion.',
                }[expr.op.name] + (' Range: ' + str(facts['range']) + '.' if facts['range'] else ''))
        else:
            out['domain'] = 'unresolved'
            self.missing('invalid_assertion' if k=='Invalid' else 'assertion_projection_unsupported',
                         'context' if k=='Invalid' else 'unsupported', f'Cannot project assertion node {k}.', expr)
        # Strength belongs at a sequence-as-property boundary, not to every
        # sequence component or to an implication antecedent.
        if k=='StrongWeak' or (domain=='property' and (out['domain']=='sequence' or out['op']=='sample')):
            from .property_completion import sequence_property_rules
            match_node=(self.result['nodes'][int(out['children'][0][1:])] if k=='StrongWeak' else out)
            match_node=self.single_sample(match_node) or match_node
            facts['sequence_property']=sequence_property_rules(self.result.get('directive',{}),
                explicit=out['op'] if k=='StrongWeak' else None,match_node=match_node)
        if (facts.get('repetition', {}).get('empty_match_allowed') or
                any(c in self.empty_repetition_subtrees for c in out['children'])):
            self.empty_repetition_subtrees.add(out['id'])
        return out

    def single_sample(self, node):
        """Resolve only transparent clock/reference wrappers with a bounded walk."""
        for _ in range(self.limits['max_depth']+1):
            self.charge()
            if node['facts'].get('repetition') or node['facts'].get('back_to') is not None:
                return None
            if node['op']=='sample':
                return node
            if node['op'] not in {'clock','reference'} or len(node['children'])!=1:
                return None
            node=self.result['nodes'][int(node['children'][0][1:])]
        return None

    def leading_clock(self, node):
        if node['op'] == 'clock':
            return node['clock']
        if node['children']:
            return self.leading_clock(self.result['nodes'][int(node['children'][0][1:])])
        return node['clock']

    @staticmethod
    def range(value):
        return dict(min=int(value.min), max=int(value.max) if value.max is not None else None,
                    unbounded=value.max is None)

    def repetition(self, rep, out):
        if rep is None:
            return
        r = self.range(rep.range)
        mode = rep.kind.name
        # These describe one Boolean base occurrence, not the interval of the
        # whole repeated sequence. Preserve them under an explicit scope.
        facts = out['facts']
        base = {key: facts.pop(key) for key in ('match', 'unknown', 'endpoints') if key in facts}
        if base:
            facts['base_match'] = dict(scope='one_base_occurrence_before_repetition', **base)
        out['domain'] = 'sequence'
        facts.update(match='repetition_of_operand_matches',
                     endpoints='see_repetition_endpoint_and_zero_count',
                     match_scope='entire_repeated_sequence')
        out['facts']['repetition'] = dict(kind=mode, count=r,
            empty_match_allowed=r['min']==0,
            empty_interval='zero_ticks; end=start-1; contextual_legality_checked_by_frontend',
            continuity={'Consecutive':'adjacent_matches_joined_by_##1',
                        'GoTo':'boolean_occurrences_separated_by_zero_or_more_false_ticks',
                        'Nonconsecutive':'boolean_occurrences_separated_by_zero_or_more_false_ticks'}[mode],
            endpoint={'Consecutive':'end_of_last_repetition', 'GoTo':'last_true_occurrence',
                      'Nonconsecutive':'last_true_occurrence_or_a_later_false_tick_before_next_true'}[mode],
            zero_count=('empty_only' if mode != 'Nonconsecutive' else 'zero_or_more_false_ticks; may_be_empty'))
        count = str(r['min']) if r['min'] == r['max'] else f"{r['min']}:{r['max'] if r['max'] is not None else '$'}"
        out['explanation'] = (f'{mode} repetition applies count {count} to operand matches. '
                              'The repetition endpoint and zero-count rules describe the whole sequence; '
                              'base_match describes a single base occurrence.')
        if mode in {'GoTo', 'Nonconsecutive'}:
            out['facts']['repetition'].update(
                occurrence_selection='count_true_occurrences_in_order_from_this_repetition_start; do_not_skip_earlier_true_occurrences',
                waiting_bound='unbounded_between_occurrences_even_with_finite_count',
                finite_window_without_required_occurrences='can_remain_pending; absence_of_match_is_not_automatically_a_failure')
            out['explanation'] += (' The count bounds occurrences, not elapsed clock ticks. '
                'Count true occurrences in order from this repetition start; do not skip earlier true occurrences. '
                + ('The match ends at the selected count\'s last true occurrence.' if mode == 'GoTo' else
                   'The match may end at that last true occurrence or a later false tick before the next true occurrence.')
                + ' Waiting for occurrences can remain pending at a finite simulation end.')

    def check_local_flow(self, root):
        """Must-defined local values across sequence alternatives, not live signals.

        A single abstract pass suffices for definite assignment: repeated bodies
        cannot initialize a read that was illegal on the first iteration. Zero
        repetition does not guarantee assignments from the skipped body.
        """
        nodes={n['id']:n for n in self.result['nodes']}
        locals_={v['symbol'] for v in self.variables.values()}
        owners={}
        for v in self.variables.values(): owners.setdefault(v['owner'],set()).add(v['symbol'])
        def reads(names,assigned,node):
            for name in sorted((set(names)&locals_)-assigned):
                self.result['gaps'].append(gap('local_variable_does_not_flow','syntax',
                    f'Local {name} is read on a branch where no value flows to this use.',
                    'Assign the local on every incoming matching branch, or move the use inside its assigning branch.',node['source']))
        def walk(node_id,assigned,depth=0):
            self.charge(depth)
            n=nodes[node_id]; f=n['facts']; incoming=set(assigned); assigned=set(assigned); written=set()
            reads(f.get('expression',{}).get('reads',[]),assigned,n)
            condition=f.get('condition')
            if isinstance(condition,dict): reads(condition.get('reads',[]),assigned,n)
            children=n['children']
            if n['op']=='reference':
                assigned-=owners.get(node_id,set())
                for binding in f.get('bindings',[]):
                    if binding['local'] and binding['direction'] in {'In','InOut'}:
                        dependencies=binding['actual'].get('reads',[])
                        reads(dependencies,incoming,n)
                        if set(dependencies)&locals_ <= incoming:
                            assigned.add(binding['formal_symbol'])
            if n['op'] in {'And','Or','Intersect','Within','if'} and len(children)>=2:
                branches=[walk(c,assigned,depth+1) for c in children]
                if n['op'] in {'Or','if'}:
                    assigned=set.intersection(*(s for s,w in branches))
                else:
                    assigned=set.union(*(s for s,w in branches))
                    conflict=set.intersection(*(w for s,w in branches))
                    assigned-=conflict
                    f['locals_blocked_from_flow']=sorted(conflict)
                written=set.union(*(w for s,w in branches))
            else:
                for c in children:
                    assigned,w=walk(c,assigned,depth+1);written|=w
            for effect in f.get('match_items',[]):
                reads(effect.get('reads',[]),assigned,n)
                writes=set(effect.get('writes',[]))&locals_
                assigned|=writes;written|=writes
            if f.get('repetition',{}).get('empty_match_allowed'):
                assigned&=incoming
            if n['op']=='reference':
                transfers=[]
                for binding in f.get('bindings',[]):
                    if binding['local'] and binding['direction'] in {'Out','InOut'}:
                        actual=binding['actual']
                        targets=actual.get('reads',[]) if actual.get('kind') in {'NamedValue','HierarchicalValue'} else []
                        if not targets:
                            self.missing('local_argument_alias_unresolved','unsupported',
                                'Local output argument is not a whole statically bound variable.',None)
                        elif binding['formal_symbol'] in assigned:
                            assigned.update(targets);written.update(targets)
                        transfers.append({'formal':binding['formal_symbol'],'actual':targets,
                                          'copy':'on_successful_sequence_match',
                                          'definitely_assigned':binding['formal_symbol'] in assigned})
                f['local_output_transfers']=transfers
                assigned-=owners.get(node_id,set())
                written-=owners.get(node_id,set())
            f['local_flow']={'definitely_assigned_out':sorted(assigned&locals_), 'written':sorted(written)}
            return assigned,written
        walk(root,set())
