"""Frozen two-register real dual-design oracle; no product evaluator as golden."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from dual_trace_oracle import evaluate_binary

BITS = list(range(7, -1, -1))
SUFFIXES = ('dut.result', 'dut.q1', 'dut.q0', 'dut.data', 'dut.mask',
            'result', 'data', 'mask')


def validate_step(step, case, source, leaf, require):
    top = case['top_hint']
    require(step['backend'] == 'verdi_npi' and step['complete'] and not step['gaps']
            and step['width'] == 8 and list(step['bits']) == BITS, 'sync_native_incomplete_' + leaf)
    require(step['signal'] == top + '.dut.' + leaf + '[7:0]', 'sync_native_target')
    require(step['sources'] and all(source in str(row['location']) for row in step['sources']),
            'sync_native_source')
    require(step['traversal']['search_exhaustive'], 'sync_native_not_exhaustive')
    require(len(step['branches']) == 1, 'sync_native_writer_count')
    branch = step['branches'][0]
    if leaf in ('q0', 'q1'):
        require(step['boundary'] == 'sequential' and step['clock']['edge'] == 'posedge',
                'sync_dff_boundary')
        clock = step['clock']['expression']
        require(clock['op'] == 'signal' and clock['width'] == 1
                and clock['signal'] in (top + '.clk', top + '.dut.clk')
                and list(clock['bits']) == [0], 'sync_dff_clock')
        expected = 'data' if leaf == 'q0' else 'q0'
        expr = branch['value']
        require(expr['op'] == 'signal' and expr['width'] == 8
                and expr['signal'] == top + '.dut.' + expected
                and list(expr['bits']) == list(expr['declared_bits']) == BITS,
                'sync_dff_data_bits')
        for value in range(256):
            values = {expr['signal']: (f'{value:08b}', BITS)}
            require(evaluate_binary(branch['guard'], values) == '1'
                    and evaluate_binary(expr, values) == f'{value:08b}', 'sync_dff_assignment_oracle')
    else:
        require(step['boundary'] == 'combinational' and step['clock'] is None, 'sync_xor_boundary')
        for value in range(256):
            values = {top + '.dut.q1': (f'{value:08b}', BITS),
                      top + '.dut.mask': ('00001111', BITS)}
            require(evaluate_binary(branch['guard'], values) == '1'
                    and evaluate_binary(branch['value'], values) == f'{value ^ 15:08b}',
                    'sync_xor_truth_oracle')


def native_ports(backend, case, leaf, require, record):
    """Independent SDK port records; these do not call the product projector."""
    nl = backend._npi_modules[1]
    name = case['top_hint'] + '.dut.' + leaf
    net = nl.get_net(name)
    record['sdk'] = dict(declaration=name, net_present=net is not None, drivers=[])
    require(net is not None, 'sync_sdk_declaration_missing')
    rows = record['sdk']['drivers']
    for driver in net.driver_list() or []:
        scope = driver.scope_inst()
        row = dict(driver=driver.full_name(), scope=scope.full_name() if scope else None)
        rows.append(row)
        if scope and scope.inst_type() == 'npiNlRTLInst':
            row.update(cell_type=scope.cell_type(), source=str(scope.src_info()), ports=[])
            for pin in scope.instport_list() or []:
                linked = pin.connected_net()
                row['ports'].append(dict(name=pin.full_name(), direction=pin.direction(),
                    port_type=pin.port_type(), state=pin.port_state(),
                    net=None if linked is None else dict(name=linked.full_name(), width=int(linked.size()),
                        left=int(linked.left()), right=int(linked.right()))))
    result = dict(declaration=name, width=int(net.size()), left=int(net.left()), right=int(net.right()), drivers=rows)
    record['sdk'] = result
    require(result['width'] == 8 and result['left'] == 7 and result['right'] == 0, 'sync_sdk_bit_order')
    if leaf in ('q0', 'q1'):
        ports = [p for row in rows for p in row.get('ports', []) if p['direction'] == 'npiNlInput']
        clock = [p for p in ports if p['port_type'] == 'npiNlClockPort']
        data = [p for p in ports if p['port_type'] == 'npiNlDataPort']
        expected_data = case['top_hint'] + '.dut.' + ('data' if leaf == 'q0' else 'q0')
        require(len(ports) == 2 and len(clock) == len(data) == 1
                and clock[0]['state'] == 'npiNlRisingActive'
                and clock[0]['net']['name'] in (case['top_hint'] + '.clk', case['top_hint'] + '.dut.clk')
                and data[0]['net']['name'] in (expected_data, expected_data + '[7:0]')
                and data[0]['net']['width'] == 8 and data[0]['net']['left'] == 7
                and data[0]['net']['right'] == 0, 'sync_sdk_dff_pin_oracle')
    return result


async def sync_chain(args, report, cases, inventory, server, counts, selected,
                     measured, public, require):
    from src import divergence_trace, npi_hierarchy_source, verdi_npi_backend
    from src.evidence_output import expand_compact_result
    handles, identities, entries, sessions = {}, {}, {}, []
    original_init = divergence_trace.ObservationSession.__init__

    def session_init(session, *a, **kw):
        original_init(session, *a, **kw)
        sessions.append(session)

    async def call(name, tool, arguments):
        report.setdefault('requests', {})[name] = dict(tool=tool, arguments=deepcopy(arguments))
        return await measured(name, lambda: public(tool, arguments))

    divergence_trace.ObservationSession.__init__ = session_init
    try:
        import asyncio
        for label, case in cases.items():
            async def prepare(case=case):
                return await asyncio.gather(
                    public('prepare_design_context', dict(compile_log=case['compile_log'], simulator='vcs',
                                                         top_hint=case['top_hint'])),
                    public('scan_structural_risks', dict(compile_log=case['compile_log'], simulator='vcs', analysis_mode='fast')))
            prepared, scan = await measured('prepare_scan_' + label, prepare)
            require(not prepared.get('error') and not scan.get('error')
                    and prepared['semantic_status'] == scan['semantic']['status'] == 'not_run'
                    and scan['lexical_coverage_status'] == 'complete', 'sync_prepare_scan')
            handles[label], identities[label] = prepared['design_handle'], prepared['context_identity_sha256']
            with server._design_context_store.pin(handles[label]) as entry:
                entries[label] = entry
        require(handles['a'] != handles['b'] and identities['a'] != identities['b'], 'sync_identity_alias')
        require(counts['sdk_init'] == counts['sdk_load_design'] == 0, 'sync_prepare_native')
        if args.mode == 'sync-boundary':
            report['sync_boundary'] = {}
            for label, case in cases.items():
                await call('driver_' + label, 'explain_signal_driver', dict(design_handle=handles[label],
                    wave_path=case['wave_path'], signal_path=case['signal_path'], recursive=False))
                backend = selected[identities[label]]
                for leaf in ('result', 'q1', 'q0'):
                    async def query(case=case, leaf=leaf, backend=backend):
                        return await server._call_connectivity_backend(backend, lambda: backend.get_dynamic_step(
                            signal_path=case['top_hint'] + '.dut.' + leaf + '[7:0]',
                            compile_log=case['compile_log'], top_hint=case['top_hint'], simulator='vcs'))
                    step = await measured('boundary_' + label + '_' + leaf, query)
                    # Save original facts before validation; failure retains both SDK and normalized facts.
                    record = report['sync_boundary'][label + '_' + leaf] = dict(step=deepcopy(step))
                    native_ports(backend, case, leaf, require, record)
                    validate_step(step, case, inventory[label.upper()]['source']['path'], leaf, require)
            report['status'] = 'dual_sync_boundary_verified'
            return

        boundary_bytes = Path(args.boundary_evidence).read_bytes()
        boundary = json.loads(boundary_bytes)
        require(boundary['status'] == 'dual_sync_boundary_verified' and not boundary.get('error')
                and not boundary['denied'] and not boundary.get('cleanup_errors')
                and boundary['inventory_sha256'] == report['inventory_sha256']
                and boundary['mcp_evidence']['sha256'] == report['mcp_evidence']['sha256'], 'sync_boundary_unqualified')
        report['native_boundary_evidence'] = dict(path=args.boundary_evidence,
            sha256=hashlib.sha256(boundary_bytes).hexdigest())
        pairs = [dict(a=cases['a']['top_hint'] + '.' + suffix + '[7:0]',
                      b=cases['b']['top_hint'] + '.' + suffix + '[7:0]') for suffix in SUFFIXES]
        pairs += [dict(a=cases['a']['top_hint'] + '.' + suffix,
                       b=cases['b']['top_hint'] + '.' + suffix) for suffix in ('clk', 'dut.clk')]
        request = dict(side_a=dict(design_handle=handles['a'], wave_path=cases['a']['wave_path'], signal_path=cases['a']['signal_path']),
            side_b=dict(design_handle=handles['b'], wave_path=cases['b']['wave_path'], signal_path=cases['b']['signal_path']),
            start_time_ps=11000, end_time_ps=36000, reference_side='a',
            comparison=dict(mode='clock', clock_a=cases['a']['top_hint']+'.clk',
                clock_b=cases['b']['top_hint']+'.clk', edge='posedge', sample_offset_ps=1000),
            signal_pairs=pairs, scope_pairs=[], limits=dict(max_depth=8, max_nodes=32, max_branches=8, timeout_sec=30))
        first_facts = None
        actions = None
        for iteration in ('first', 'repeat', 'recovery'):
            if iteration == 'recovery':
                for action in actions:
                    result = expand_compact_result(await call('action_'+action['side'], action['tool'], action['arguments']))
                    require(result['backend_status']['actual_backend'] == 'verdi_npi'
                            and result['driver_status'] == 'resolved', 'sync_action_recovery')
            name = 'sync_' + iteration
            raw = await call(name, 'trace_divergence', dict(request, cursor_name='p4_s9_' + iteration))
            server.schemas.TraceDivergenceResult.model_validate(raw)
            require(raw['status'] in {'complete', 'partial'}, 'sync_public_status')
            comp = raw['comparison']
            require(comp['mode'] == 'clock' and comp['diverged'] and comp['earliest_difference_proven']
                    and comp['first_divergence_time_ps'] == 26000 and comp['samples_compared'] == 2
                    and comp['value_a'] == '00110011' and comp['value_b'] == '11001100'
                    and comp['coverage_status'] == 'partial' and not comp['coverage_gaps'], 'sync_clock_oracle')
            require(len(raw['nodes']) >= 3 and len(raw['edges']) >= 2
                    and not raw['coverage']['truncated'] and not raw['coverage']['restart_reasons'], 'sync_multilevel_missing')
            chain = []
            for leaf, observed, phase, sampled, sample_phase, trigger in (
                    ('result',26000,'after',26000,'after',None),
                    ('q1',26000,'after',25000,'before',25000),
                    ('q0',25000,'before',15000,'before',15000)):
                matches = [node for node in raw['nodes'] if all(node[s]['signal'] == cases[s]['top_hint']+'.dut.'+leaf for s in cases)]
                require(len(matches) == 1, 'sync_exact_chain_node_'+leaf)
                node = matches[0]; chain.append(node['id'])
                for label, value in [('a','00111100'), ('b','11000011')]:
                    fact = node[label]; obs = fact['observation']; structure = fact['structure']
                    require(fact['time_ps'] == observed and fact['phase'] == phase and list(fact['bits']) == BITS,
                            'sync_node_time_or_bits')
                    expected_value = ('00110011' if label=='a' else '11001100') if leaf=='result' else value
                    require(fact['value'] == obs['value'] == expected_value and obs['complete'] and not obs['gaps']
                            and obs['observation_time_ps'] == observed and obs['observation_phase'] == phase
                            and obs['sampling_time_ps'] == sampled and obs['sampling_phase'] == sample_phase
                            and obs['trigger_time_ps'] == trigger, 'sync_temporal_oracle_'+leaf)
                    require(structure['backend'] == 'verdi_npi' and structure['complete'] and not structure['gaps'],
                            'sync_structure_gap')
                    expected_dep = 'q1' if leaf=='result' else 'q0' if leaf=='q1' else 'data'
                    deps=[d for d in obs['dependencies'] if d['signal'] == cases[label]['top_hint']+'.dut.'+expected_dep]
                    require(deps and {b for d in deps for b in d['bits']} == set(BITS), 'sync_dependency_bits')
                    for dep in deps:
                        require(dep['time_ps']==sampled and dep['phase']==sample_phase and not dep['gaps']
                                and dep['value']==''.join(value[BITS.index(bit)] for bit in dep['bits']), 'sync_dependency_value')
            edges={(e['source'],e['target']) for e in raw['edges']}
            require((chain[0],chain[1]) in edges and (chain[1],chain[2]) in edges, 'sync_edges_missing')
            require(all(row['reason'] in {'testbench_driven','input_boundary','dynamic_evidence_unavailable'}
                        and row['node_id'] not in chain for row in raw['frontier']), 'sync_unexpected_frontier')
            for label in cases:
                context=raw['contexts'][label]; receipt=context['backend_status']
                require(context['design_context']['design_handle']==handles[label]
                        and receipt['actual_backend']==receipt['selected_backend']=='verdi_npi'
                        and receipt['single_backend_provenance'] and receipt['whole_trace_restart_count']==0
                        and not receipt.get('fallback_reason')
                        and context['design_context']['artifact']['source_association']=='unverified', 'sync_context_source')
                require(entries[label].leases==0 and not entries[label].binding_lock.locked(), 'sync_lease_leak')
            require(raw['cursor']['time_ps']==26000 and len(raw['next_actions'])==2
                    and all(a['status']=='ready' for a in raw['next_actions']), 'sync_cursor_actions')
            actions=deepcopy(raw['next_actions'])
            facts={k:raw[k] for k in ('nodes','edges','findings','frontier')}
            if first_facts is None: first_facts=deepcopy(facts)
            else: require(facts==first_facts, 'sync_repeat_facts_changed')
        require(all(not session.entries and session.events==session.nbytes==session.budget.cache_bytes==0
                    for session in sessions), 'sync_observation_leak')
        require(not server._FSDB_WAVE_LOCK.locked() and server._FSDB_ACTIVE is None
                and not npi_hierarchy_source._OWNER_LOCK.locked()
                and not verdi_npi_backend._NPI_FAN_IN_CALLBACK_LOCK.locked(), 'sync_lock_leak')
        report['sync_final_state']=dict(pins={s:e.leases for s,e in entries.items()}, sessions=len(sessions),
            sessions_empty=True, locks_clear=True, original_handles_recovered=True)
        report['status']='dual_sync_trace_verified'
    finally:
        divergence_trace.ObservationSession.__init__=original_init
        report['sync_observers_restored']=True
