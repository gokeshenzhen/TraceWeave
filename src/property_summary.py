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
        literal = default.get('value', 'the input argument type default')
        arg = call['arguments'][0]
        text += (f" Before available history use {literal}, from input `{_label(arg['text'],80)}` "
                 f"of type {arg['type']['name']}; the function return type does not determine this history value.")
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
