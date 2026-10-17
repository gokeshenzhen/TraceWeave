"""Independent oracle for the explicit dual_trace_npi fixture, not a product API.

The HDL formula and full two-state truth table are the oracle. Do not import
TraceWeave's expression evaluator here. Unknown native expression forms fail.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path


def evaluate_binary(expr, inputs):
    """Small MSB-first evaluator for this fixture's reviewed native forms."""
    op, width, args = expr['op'], expr['width'], expr.get('args', [])
    if not 1 <= width <= 8 or expr.get('signed'):
        raise ValueError('oracle_unreviewed_type')
    if op == 'signal':
        value, declared = inputs[expr['signal']]
        if tuple(expr['declared_bits']) != tuple(declared) or len(expr['bits']) != width:
            raise ValueError('oracle_wrong_declaration')
        return ''.join(value[declared.index(bit)] for bit in expr['bits'])
    if op == 'const':
        value = expr['value']
    elif op == 'mux':
        condition = evaluate_binary(args[0], inputs)
        if len(condition) != 1:
            raise ValueError('oracle_non_scalar_condition')
        value = evaluate_binary(args[1 if condition == '1' else 2], inputs)
    elif op == '^':
        left, right = (evaluate_binary(a, inputs) for a in args)
        if len(left) != width or len(right) != width:
            raise ValueError('oracle_xor_width')
        value = ''.join(str(int(a) ^ int(b)) for a, b in zip(left, right))
    elif op == '~':
        value = ''.join('1' if b == '0' else '0' for b in evaluate_binary(args[0], inputs))
    elif op == 'concat':
        value = ''.join(evaluate_binary(a, inputs) for a in args)
    elif op == 'project':
        child = evaluate_binary(args[0], inputs)
        value = ''.join(child[position] for position in expr['bits'])
    else:
        raise ValueError('oracle_unreviewed_operator_' + op)
    if len(value) != width or set(value) - {'0', '1'}:
        raise ValueError('oracle_invalid_binary_value')
    return value


def validate_step(step, case, source, mask, require):
    top = case['top_hint']
    bits = list(range(7, -1, -1))
    require(step.get('backend') == 'verdi_npi' and step.get('complete') is True
            and step.get('gaps') == [] and step.get('boundary') == 'combinational'
            and step.get('clock') is None and step.get('width') == 8
            and step.get('bits') == bits and step.get('signal') == case['signal_path'],
            'unsupported_or_foreign_native_step')
    require(step.get('traversal', {}).get('search_exhaustive') is True,
            'native_step_not_exhaustive')
    require(step.get('sources') and all(
        row['cell'].startswith(top + '.') and source in str(row['location'])
        for row in step['sources']), 'native_step_source_mismatch')
    branches = step['branches']
    require(len(branches) == 1, 'unreviewed_writer_count')
    branch = branches[0]
    found = {}
    pending = [branch['guard'], branch['value']]
    while pending:
        node = pending.pop()
        if node['op'] == 'signal':
            found.setdefault(node['signal'], set()).update(node['bits'])
        pending.extend(node.get('args', []))
    require(found == {top + '.data': set(bits), top + '.mode': {0}},
            'native_reference_identity_or_bits')
    for mode in (0, 1):
        for data in range(256):
            inputs = {top + '.data': (f'{data:08b}', bits), top + '.mode': (str(mode), [0])}
            require(evaluate_binary(branch['guard'], inputs) == '1', 'unexpected_writer_guard')
            expected = data ^ mask if mode else data
            require(evaluate_binary(branch['value'], inputs) == f'{expected:08b}',
                    'independent_hdl_truth_table_mismatch')
    return dict(binary_rows_checked=512, mask=mask, declared_bits=bits)


async def trace_chain(args, report, cases, inventory, server, counts, handles,
                      identities, measured, public, require):
    require(bool(args.boundary_evidence), 'trace_requires_native_boundary')
    raw = Path(args.boundary_evidence).read_bytes()
    boundary = json.loads(raw)
    require(boundary.get('status') == 'boundary_observed' and not boundary.get('error')
            and not boundary.get('denied') and not boundary.get('cleanup_errors'), 'failed_native_boundary')
    require(boundary.get('inventory_sha256') == report['inventory_sha256']
            and boundary.get('mcp_evidence', {}).get('sha256') == report['mcp_evidence']['sha256'],
            'foreign_boundary_inputs')
    report['native_boundary_evidence'] = dict(path=str(Path(args.boundary_evidence).resolve()),
        sha256=hashlib.sha256(raw).hexdigest(), expected_head=boundary['expected_head'])
    report['independent_truth_tables'] = {}
    for label, mask in (('a', 0x0f), ('b', 0xf0)):
        case = cases[label]
        require(case['top_hint'] == f'p4_s2_{label}_tb'
                and case['signal_path'] == f'p4_s2_{label}_tb.result[7:0]', 'wrong_fixture_target')
        evidence = boundary['identities'][label]
        require(evidence['loaded_top'] == case['top_hint']
                and Path(evidence['loaded_kdb']).resolve() == Path(case['kdb_path']).resolve(),
                'foreign_boundary_loaded_design')
        report['independent_truth_tables'][label] = validate_step(
            boundary['results']['dynamic_' + label], case,
            inventory[label.upper()]['source']['path'], mask, require)

    request = dict(
        side_a=dict(design_handle=handles['a'], wave_path=cases['a']['wave_path'],
                    signal_path=cases['a']['signal_path']),
        side_b=dict(design_handle=handles['b'], wave_path=cases['b']['wave_path'],
                    signal_path=cases['b']['signal_path']),
        start_time_ps=5000, end_time_ps=35000, reference_side='a',
        comparison=dict(mode='event'), signal_pairs=[],
        scope_pairs=[dict(a=cases['a']['top_hint'], b=cases['b']['top_hint'])],
        limits=dict(max_depth=4, max_nodes=16, max_branches=8, timeout_sec=30))
    anchor_ids = {}

    def native_receipt(status, label):
        require(status.get('actual_backend') == status.get('selected_backend') == 'verdi_npi'
                and status.get('execution_mode') == 'local'
                and not status.get('fallback_reason'), 'non_native_trace_receipt_' + label)
        with server._design_context_store.pin(handles[label]) as entry:
            anchor = entry.binding.anchor
            require(anchor is not None and Path(anchor.canonical_path) == Path(cases[label]['kdb_path']).resolve(),
                    'foreign_trace_anchor_' + label)
            anchor_ids[label] = anchor.identity_sha256
            require(entry.leases == 1, 'leaked_public_request_pin_' + label)

    first_facts = None
    for iteration in ('first', 'repeat'):
        name = 'trace_' + iteration
        report.setdefault('requests', {})[name] = dict(request, cursor_name='p4_s2_' + iteration)
        before = counts.copy()
        result = await measured(name, lambda: public('trace_divergence', report['requests'][name]))
        server.schemas.TraceDivergenceResult.model_validate(result)
        require(result.get('status') == 'complete' and result['reference_side'] == 'a'
                and len(result['nodes']) == 1 and not result['edges'] and not result['frontier'],
                'incomplete_supported_local_trace')
        comp = result['comparison']
        require(comp['diverged'] is True and comp['first_divergence_time_ps'] == 20000
                and comp['earliest_difference_proven'] is True
                and comp['value_a'] == '00110011' and comp['value_b'] == '11001100',
                'independent_wave_oracle_mismatch')
        require(comp['coverage_status'] == 'partial'
                and {r['reason'] for r in comp['coverage_gaps']} == {'stopped_at_difference'},
                'comparison_tail_coverage_hidden')
        require(result['cursor']['time_ps'] == 20000 and not result['coverage']['gaps']
                and not result['coverage']['truncated'] and not result['coverage']['restart_reasons'],
                'trace_cursor_or_coverage_invalid')
        require(len(result['findings']) == 1
                and result['findings'][0]['kind'] == 'local_logic_candidate'
                and result['findings'][0]['checked_sides'] == ['a', 'b']
                and result['findings'][0]['competing_explanations']
                and result['findings'][0]['limitations'], 'overclaimed_local_logic_finding')
        native_steps = [row for row in report['dynamic_steps'] if row['phase'] == name]
        require([row['side'] for row in native_steps] == ['a', 'b'], 'trace_missing_actual_dual_steps')
        require(counts['get_dynamic_step'] - before['get_dynamic_step'] == 2
                and counts['sdk_load_design'] - before['sdk_load_design'] == 2,
                'trace_actual_native_work_mismatch')
        require([row['side'] for row in report['loads'] if row['phase'] == name] == ['a', 'b'],
                'trace_load_side_order_mismatch')
        for label, value, mask in (('a', '00110011', 0x0f), ('b', '11001100', 0xf0)):
            context = result['contexts'][label]
            dc = context['design_context']
            require(dc['design_handle'] == handles[label]
                    and dc['context_identity_sha256'] == identities[label], 'crossed_trace_context_' + label)
            status = context['backend_status']
            native_receipt(status, label)
            require(status['single_backend_provenance'] is True
                    and status['whole_trace_restart_count'] == 0, 'mixed_trace_provenance')
            require(status['kdb_load_status'] == 'loaded', 'trace_load_not_proven')
            require(dc['artifact']['source_association'] == 'unverified'
                    and context['waveform_compile_version_relation'] == 'not_independently_proven',
                    'upgraded_source_association')
            node = result['nodes'][0][label]
            structure, observation = node['structure'], node['observation']
            require(node['bits'] == list(range(7, -1, -1)) and node['value'] == value
                    and structure['artifact_sha256'] == status['artifact_sha256']
                    and structure['backend'] == 'verdi_npi' and structure['complete'] is True
                    and not structure['gaps'] and structure['boundary'] == 'combinational',
                    'trace_structure_mismatch')
            require(observation['value'] == value and observation['complete'] is True
                    and observation['sampling_time_ps'] == observation['observation_time_ps'] == 20000
                    and observation['sampling_phase'] == observation['observation_phase'] == 'after'
                    and observation['trigger_time_ps'] is None and not observation['gaps'],
                    'trace_sampling_or_value_mismatch')
            observed = {}
            top = cases[label]['top_hint']
            expected_inputs = {top + '.data': ('00111100', list(range(7, -1, -1))),
                               top + '.mode': ('1', [0])}
            for dep in observation['dependencies']:
                require(dep['signal'] in expected_inputs and dep['time_ps'] == 20000
                        and dep['phase'] == 'after' and not dep['gaps'], 'unexpected_active_dependency')
                expected, declared = expected_inputs[dep['signal']]
                require(dep['value'] == ''.join(expected[declared.index(b)] for b in dep['bits']),
                        'opposite_side_dependency_value_mismatch')
                observed.setdefault(dep['signal'], set()).update(dep['bits'])
            require(observed == {top + '.data': set(range(8)), top + '.mode': {0}},
                    'missing_opposite_side_dependency')
            row = next(row for row in native_steps if row['side'] == label)
            require(row['loaded_top'] == top
                    and Path(row['loaded_kdb']).resolve() == Path(cases[label]['kdb_path']).resolve(),
                    'public_native_step_wrong_design')
            validate_step(row['result'], cases[label], inventory[label.upper()]['source']['path'], mask, require)
        require(anchor_ids['a'] != anchor_ids['b']
                and result['contexts']['a']['design_identity_sha256'] != result['contexts']['b']['design_identity_sha256']
                and result['nodes'][0]['a']['structure']['artifact_sha256'] != result['nodes'][0]['b']['structure']['artifact_sha256'],
                'aliased_trace_designs')
        facts = {key: deepcopy(result[key]) for key in ('nodes', 'edges', 'findings', 'frontier')}
        if first_facts is None:
            first_facts = facts
            # Interleave a different NPI API and both designs before repeating.
            for label in ('b', 'a'):
                source = await measured('instance_' + label + '_between', lambda label=label: public(
                    'find_tb_instance', dict(design_handle=handles[label], path=cases[label]['top_hint'])))
                server.schemas.SourceInstanceResult.model_validate(source)
                require(source.get('facts_origin') == 'verdi_npi'
                        and source.get('context_identity_sha256') == identities[label]
                        and len(source['hits']) == 1
                        and source['hits'][0]['path'] == cases[label]['top_hint'], 'cross_tool_source_mismatch')
                native_receipt(source['backend_status'], label)
        else:
            require(facts == first_facts, 'repeat_trace_facts_changed')

    async def final_identities():
        rows = {}
        for label, case in cases.items():
            with server._design_context_store.pin(handles[label]) as entry:
                await server._validate_design_source(entry.context, timeout_sec=30)
                await server._validate_design_binding(entry.binding, timeout_sec=5)
                require(entry.leases == 1 and entry.context.identity_sha256 == identities[label],
                        'final_context_or_pin_mismatch')
                def token():
                    parser = server._get_parser(case['wave_path'])
                    return dict(parser_id=id(parser), parser_epoch_id=id(parser._scope_epoch),
                                waveform_stat=server._get_wave_signature(case['wave_path']))
                rows[label] = dict(context=identities[label], artifact=entry.binding.anchor.receipt(),
                    wave=await server._run_in_wave_thread(case['wave_path'], token))
        return rows
    await measured('final_identity_validation', final_identities)
    report['coverage'] = dict(real_dual_native_trace_first_repeat=True, combinational_local_scope_only=True,
        native_binary_truth_table_rows_per_step=512, source_kdb_wave_current=True,
        full_hierarchy='guarded_zero', actual_source_kdb_wave_byte_mutation='not_run',
        cancellation_timeout_native_integration='not_run', synchronous_trace='not_run',
        license_checkout_release='unverified', lsf='not_run')
