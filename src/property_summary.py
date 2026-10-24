"""Bounded source-semantic summaries; no waveform evaluation or optional imports."""
from __future__ import annotations


def _label(text, limit=200):
    text = ' '.join(text.split())
    return text[:limit] + '...' if len(text) > limit else text


def _sampled_calls(value):
    """Walk only call arguments, retaining a bounded number of concrete rules."""
    pending = [value]
    visited = 0
    while pending and visited < 128:
        current = pending.pop()
        visited += 1
        if not isinstance(current, dict):
            continue
        for call in current.get('calls', []):
            visited += 1
            if visited > 128:
                return
            if call.get('purity') == 'sampled_value_function':
                yield call
            pending.extend(reversed(call.get('arguments', [])))


def _history_text(call):
    name = call['name']
    rules = {
        '$rose': 'LSB is 1 now and was 0, X or Z at the prior sampling event',
        '$fell': 'LSB is 0 now and was 1, X or Z at the prior sampling event',
        '$stable': 'all bits equal the prior sampled value, including X/Z',
        '$changed': 'some bit differs from the prior sampled value, including X/Z',
        '$sampled': 'the current sampled value; no prior history is required',
        '$past': 'the value at the recorded prior qualifying sampling event; use the recorded tick count and gate',
    }
    text = f"{name}: {rules[name]}."
    default = call.get('history_default')
    if default:
        literal = default.get('value')
        arg = call['arguments'][0]
        if literal is not None:
            text += (f" Before available history use {literal}, the default sampled value of input `{_label(arg['text'],80)}` "
                 f"of type {arg['type']['name']}, using declaration initializers and component defaults; "
                 'the function return type does not determine this history value.')
        else:
            text += (f" The default sampled value of input `{_label(arg['text'],80)}` is unresolved: "
                'use declaration initializers and recursive component defaults, not an assumed type-default literal.')
    first=call.get('first_sample')
    if first:
        prior=first['prior_bit']
        rule={
            '$rose':('false because the prior LSB is 1' if prior=='1' else 'true exactly when the current LSB is 1'),
            '$fell':('false because the prior LSB is 0' if prior=='0' else 'true exactly when the current LSB is 0'),
            '$stable':f'true exactly when ALL current bits are {prior}',
            '$changed':f'true exactly when NOT ALL current bits are {prior}',
        }[name]
        text+=f' At the FIRST sampling event without prior history, {name} is {rule}.'
        if name in {'$stable','$changed'}:
            text+=' Compare X/Z as distinct symbols: known 0 versus prior X is different, not an unknown comparison.'
        text+=' This history rule does not restart at each assertion attempt or waveform window.'
    return text


def summarize_property(result, max_lines=32):
    """Select semantic operations, including repetitions and sampled functions.

    An AST `sample` node can carry an entire repeated sequence. It must not be
    discarded as value boilerplate. Likewise, activation and sampled history
    determine interpretation even though they are outside the operator tree.
    """
    context = []
    activation = result.get('directive', {}).get('activation', {})
    kind = activation.get('kind')
    if kind == 'procedural':
        schedule=activation.get('schedule', {})
        if schedule.get('policy')=='at_most_one_initial_encounter':
            context.append('[activation] Initial assertion site: AT MOST ONE procedural encounter per instance '
                '(no enclosing runtime repetition). If that initial attempt has finished, later clock ticks do not restart it. '
                'Reachability, runtime enablement and the actual start time are not observed.')
        else:
            context.append('[activation] ' + activation['procedure'] +
            ': activate when procedural control reaches this statement. Later clock ticks within an attempt '
            'do not themselves start fresh procedural activations; enclosing controls and runtime execution remain separate.')
        if activation.get('blocking'):
            context.append('[activation] The procedure waits for this expect attempt; it has no implicit repeated attempts.')
    elif kind == 'concurrent_declaration':
        context.append('[activation] Concurrent declaration: a new attempt at each leading clock event, '
                       'subject to disable/control conditions; runtime enablement is not observed.')
    elif kind == 'none_definition_or_fragment':
        context.append('[activation] Definition or fragment only: it launches no runtime attempts by itself. '
                       'Matching and obligation rules below are scoped to one supplied start.')
    elif kind:
        context.append('[activation] Source activation is unresolved; do not assume a new attempt at every clock tick.')
    completion=result.get('directive',{}).get('completion_policy',{})
    strength=completion.get('default_sequence_strength')
    if strength=='weak':
        context.append('[completion] Required sequence mismatches still fail under WEAK: eliminate each failed candidate; '
            'when none can match, that sequence property is false. Only a still-extendible attempt may remain pending. '
            'Assert/assume defaults to weak for unqualified sequences; explicit strength and enclosing operators still apply.')
    elif strength=='strong':
        context.append('[completion] This calling statement uses STRONG for unqualified sequence properties; '
                       'explicit strength overrides the default. Rules below describe source semantics, not observed callbacks.')
    elif strength=='caller_dependent':
        context.append('[completion] Unqualified sequence strength needs the calling statement: '
            'assert/assume property defaults to weak, other property statements to strong. Explicit strength is retained below.')
    if completion.get('whole_property_outcomes'):
        context.append('[check] After applying ALL enclosing operators, whole-property false means check failure; '
            'whole-property true means satisfied. A disabled attempt has neither result; pending is not a completed result.')
    clocks = result.get('clocks', [])
    for clock in clocks[:4]:
        context.append(f"[clock {clock['id']}] {_label(clock['event'])}; inherited delays use this clock's sampling ticks. "
                       'Waveform pre-edge values are not automatically equivalent to assertion samples.')
    if len(clocks) > 4:
        context.append('[clocks] Additional clock domains are retained in clocks[].')
    disable = result.get('directive', {}).get('default_disable')
    if disable and disable.get('applied'):
        text = disable.get('condition', {}).get('text') or disable.get('text', '')
        context.append(f'[disable] Inherited default disable `{_label(text)}` aborts when true; '
                       'its asynchronous evaluation is separate from sampled functions.')

    semantic = []
    atomic = []
    for node in result['nodes']:
        facts = node['facts']
        values = [facts.get(key, {}) for key in ('expression', 'condition')]
        calls = []
        for value in values:
            calls.extend(_sampled_calls(value))
        important = (node['op'] not in {'sample', 'clock', 'reference'} or
                     'repetition' in facts or bool(calls) or facts.get('back_to') is not None)
        if facts.get('sequence_property') and node['domain']=='sequence':
            important=True
        explanation = node['explanation']
        if facts.get('match_items'):
            effects = facts['match_items']
            explanation += ' Match-item effects on matched branches, in their recorded phases: ' + '; '.join(
                _label(effect['source'].get('text', ''), 120) for effect in effects[:4]) + '.'
        if calls:
            rules = [_history_text(call) for call in calls[:4]]
            if len(calls) > 4:
                rules.append('Additional sampled-function rules are retained in the full node facts.')
            explanation = ' '.join(([explanation] if important and node['op'] != 'sample' else []) + rules)
        sequence=facts.get('sequence_property',{})
        strength=sequence.get('effective_strength')
        if sequence.get('match_class')=='required_single_sample':
            explanation += (' At its required sampling tick, a true Boolean result matches; a false/X/Z Boolean result makes this sequence property FALSE '
                'immediately, even if WEAK. A later true value cannot repair this start. Enclosing property operators still apply.')
        elif strength=='weak':
            explanation += (' WEAK still becomes FALSE when every legal matching candidate is eliminated by required mismatches. '
                'Only if a legal continuation remains can this attempt stay pending; missing completion alone at actual '
                'simulation end does not fail THAT still-extendible weak attempt. A requested window cutoff is not actual simulation end.')
        elif strength=='strong':
            explanation += (' This sequence is STRONG: a nonempty completing match is required. '
                'An unfinished sequence is not satisfied at actual simulation end; a requested window cutoff alone is not that event.')
        elif strength=='caller_dependent':
            explanation += ' This unqualified sequence needs the calling statement to select weak or strong completion.'
        if explanation:
            line = f"[{node['id']}] {_label(node['source'].get('text', ''))}: {explanation}"
            if important:
                semantic.append(line)
            elif node['op'] == 'sample':
                atomic.append(line)
    rows = context + (semantic or atomic)
    if len(rows) > max_lines:
        return rows[:max_lines-1] + ['[summary] Additional semantic entries omitted; inspect detail=full for the remaining node rules.']
    return rows
