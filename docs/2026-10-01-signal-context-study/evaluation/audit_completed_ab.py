"""Offline final audit of a completed frozen experiment and human grading.

Does not launch models or change inputs. Workflow overlap is inferred only from
recorded start/completion events; source checks cover literal HDL paths in the
observed shell commands. These checks do not infer reasoning quality or causality.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import statistics

from collect_ab import payloads, records


HDL_PATH = re.compile(r"/[^\s'\"\\;|]+\.(?:svh|sv|vh|v)\b")
VALUE_TOOLS = {
    'get_signal_at_time', 'get_signal_transitions', 'get_signals_around_time',
    'get_signals_by_cycle', 'inspect_handshake', 'verify_window', 'sweep_handshakes',
}


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def audit(out):
    root = Path(__file__).resolve().parents[3]
    manifest = json.loads((out / 'manifest.json').read_text())
    rubric = json.loads((out / 'rubric.json').read_text())
    runs = records(out / 'runs.jsonl')
    metrics = records(out / 'metrics.jsonl')
    grades = records(out / 'grading.jsonl')
    expected = [f"{r['case']}-{r['repeat']}-{r['arm']}" for r in manifest['order']]
    assert [r['run'] for r in runs] == expected, 'Frozen runs incomplete/out of order'
    assert [r['run'] for r in metrics] == expected, 'Refresh collect_ab.py first'
    assert len(grades) == len(expected) == len({g['run'] for g in grades})
    assert {g['run'] for g in grades} == set(expected)
    grade_by_run = {g['run']: g for g in grades}
    workflow = []
    dependency_packets = []
    for row in runs:
        run = out / 'runs' / row['run']
        calls = records(run / 'calls.jsonl')
        events = records(run / 'transcript.jsonl')
        grade = grade_by_run[row['run']]
        assert grade['reviewed'] and len(grade['criteria']) == len(rubric[row['case']])
        if grade['task_success']:
            assert row['status'] == 'complete'
        for fact in grade['evidence']:
            assert (out / fact['path']).is_file(), fact
            if 'call_indices' in fact:
                assert set(fact['call_indices']) <= {c['index'] for c in calls}, fact

        verified = set()
        unverified = []
        command_checks = []
        bounds = {}
        event_errors = []
        for line, event in enumerate(events, 1):
            item = event.get('item', {})
            tool = item.get('tool')
            if tool in {'build_tb_hierarchy', 'scan_structural_risks'}:
                bounds.setdefault(tool, {})[event['type']] = line
            if event['type'] in {'error', 'turn.failed'}:
                event_errors.append(dict(line=line, event=event))
            if event['type'] == 'item.completed' and tool in {'lookup_tb_files', 'get_tb_file_detail'}:
                for block in (item.get('result') or {}).get('content', []):
                    if block['type'] == 'text':
                        verified.update(HDL_PATH.findall(block['text']))
            if event['type'] == 'item.started' and item.get('type') == 'command_execution':
                paths = sorted(set(HDL_PATH.findall(item['command'])))
                missing = sorted(set(paths) - verified)
                unverified.extend(dict(event_line=line, path=path) for path in missing)
                command_checks.append(dict(event_line=line, command=item['command'],
                                           hdl_paths=paths, missing_prior_lookup=missing))
        h = bounds.get('build_tb_hierarchy', {})
        s = bounds.get('scan_structural_risks', {})
        complete_bounds = all('item.started' in b and 'item.completed' in b for b in (h, s))
        overlap = (max(h['item.started'], s['item.started']) <
                   min(h['item.completed'], s['item.completed'])) if complete_bounds else None
        context_calls = [c for c in calls if c['tool'] in bounds]
        compile_logs = {c['arguments'].get('compile_log') for c in context_calls}
        workflow.append(dict(run=row['run'], hierarchy_scan_event_bounds=bounds,
            hierarchy_scan_overlap=overlap,
            hierarchy_scan_same_compile_log=complete_bounds and len(compile_logs) == 1,
            command_checks=command_checks, unverified_literal_source_paths=unverified,
            model_errors=event_errors,
            explicit_waveform_value_calls=[c['index'] for c in calls if c['tool'] in VALUE_TOOLS]))
        for call in calls:
            for data in payloads(call):
                context = data.get('dependency_context') if isinstance(data, dict) else None
                if context is not None:
                    dependency_packets.append(dict(run=row['run'], index=call['index'],
                        backend=context['backend'], rows=len(context['dependencies']),
                        bytes=len(json.dumps(context).encode()), gaps=context['gaps']))

    changed = [path for path, digest in manifest['product_hashes'].items()
               if sha256(root / path) != digest]
    script_changes = [path for path, digest in manifest['scripts'].items()
                      if sha256(Path(__file__).parent / path) != digest]
    artifacts = [dict(project=project, kind=kind, matches=sha256(Path(item['path'])) == item['sha256'])
                 for project, inputs in manifest['artifacts'].items() for kind, item in inputs.items()]
    (out / 'workflow-audit.json').write_text(json.dumps(dict(
        scope='Recorded event order and literal HDL source paths; not a semantic shell proof',
        runs=workflow, product_changes=changed, frozen_script_changes=script_changes,
        artifact_checks=artifacts), indent=2, ensure_ascii=False) + '\n')

    case_summary = []
    for case in dict.fromkeys(row['case'] for row in runs):
        for arm in 'AB':
            selected = [r for r in metrics if r['case'] == case and r['arm'] == arm]
            case_summary.append(dict(case=case, arm=arm, runs=len(selected),
                paired_eligible_runs=sum(grade_by_run[r['run']].get('valid_for_paired_acceptance', True)
                                         for r in selected),
                task_success=sum(grade_by_run[r['run']]['task_success'] for r in selected),
                strict_rubric_accepted=sum(grade_by_run[r['run']]['strict_accepted'] for r in selected),
                statuses=dict(Counter(r['status'] for r in selected)),
                dependency_requests=sum(r['dependency_requests'] for r in selected),
                known_usage_runs=sum(r['total_tokens'] is not None for r in selected),
                all_task_tokens=(sum(r['total_tokens'] for r in selected)
                                 if all(r['total_tokens'] is not None for r in selected) else None)))
    arm_summary = []
    for arm in 'AB':
        selected = [r for r in metrics if r['case'] != 'S1' and r['arm'] == arm]
        if not selected:
            continue
        arm_summary.append(dict(arm=arm, core_runs=len(selected),
            task_success=sum(grade_by_run[r['run']]['task_success'] for r in selected),
            statuses=dict(Counter(r['status'] for r in selected)),
            usage_missing=sum(r['total_tokens'] is None for r in selected),
            mcp_completed=sum(r['mcp_calls'] for r in selected),
            mcp_attempted=sum(r['mcp_attempted'] for r in selected),
            source_read_commands=sum(r['source_read_commands'] for r in selected),
            source_read_max=max(r['source_read_commands'] for r in selected),
            search_calls=sum(r['tool_counts'].get('search_signals', 0) for r in selected),
            top_level_tool_errors=sum(len(r['mcp_errors']) for r in selected),
            tool_response_bytes=sum(r['tool_response_bytes'] for r in selected),
            sum_tool_elapsed=sum(r['tool_elapsed_sum'] for r in selected),
            elapsed_sum=sum(r['elapsed'] for r in selected),
            elapsed_median=statistics.median(r['elapsed'] for r in selected),
            all_task_tokens=(sum(r['total_tokens'] for r in selected)
                             if all(r['total_tokens'] is not None for r in selected) else None)))
    summary = dict(total_runs=len(runs), cases=case_summary, core_arms=arm_summary,
        dependency_packets=dependency_packets,
        workflow_serial_runs=[r['run'] for r in workflow if r['hierarchy_scan_overlap'] is False],
        source_lookup_mismatches=[r['run'] for r in workflow if r['unverified_literal_source_paths']],
        product_hashes_match=not changed, frozen_scripts_match=not script_changes,
        artifacts_match=all(a['matches'] for a in artifacts),
        known_usage=[dict(run=r['run'], usage=r['usage'], total_tokens=r['total_tokens'],
                          elapsed=r['elapsed']) for r in metrics if r['total_tokens'] is not None],
        interpretation='Raw observations only. Missing task usage remains null; no benefit inferred.')
    (out / 'summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({k: summary[k] for k in ('total_runs', 'core_arms', 'source_lookup_mismatches',
                                            'product_hashes_match', 'frozen_scripts_match', 'artifacts_match')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('experiment', type=Path)
    audit(parser.parse_args().experiment.resolve())
